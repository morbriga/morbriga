#!/usr/bin/env bash
# tg-media-bot installer — run on the Proxmox HOST shell (as root).
# Required env: API_ID API_HASH SMB_HOST SMB_SHARE SMB_USER SMB_PASS
# Optional env: JELLYFIN_URL JELLYFIN_KEY CTID STORAGE BRIDGE TEMPLATE
set -euo pipefail

REPO_RAW="${REPO_RAW:-https://raw.githubusercontent.com/morbriga/morbriga/tg-media-bot}"
: "${API_ID:?missing API_ID}" "${API_HASH:?missing API_HASH}"
: "${SMB_HOST:?missing SMB_HOST}" "${SMB_SHARE:?missing SMB_SHARE}"
: "${SMB_USER:?missing SMB_USER}" "${SMB_PASS:?missing SMB_PASS}"
JELLYFIN_URL="${JELLYFIN_URL:-}"
JELLYFIN_KEY="${JELLYFIN_KEY:-}"
STORAGE="${STORAGE:-local-lvm}"
BRIDGE="${BRIDGE:-vmbr0}"
HOSTMNT="/mnt/tgmedia"
CREDS="/root/.smb-tgmedia"

say() { echo -e "\n\e[1;36m==> $*\e[0m"; }
die() { echo -e "\n\e[1;31m✖ $*\e[0m"; exit 1; }

command -v pct >/dev/null || die "Run this on the Proxmox host shell"

# ---------- 1. mount Synology share on the host
say "Mounting //${SMB_HOST}/${SMB_SHARE} on host at ${HOSTMNT}"
command -v mount.cifs >/dev/null || { apt-get update -qq; apt-get install -y -qq cifs-utils; }
mkdir -p "$HOSTMNT"
umask 077
printf 'username=%s\npassword=%s\n' "$SMB_USER" "$SMB_PASS" > "$CREDS"
umask 022
FSTAB_LINE="//${SMB_HOST}/${SMB_SHARE} ${HOSTMNT} cifs credentials=${CREDS},uid=100000,gid=100000,file_mode=0664,dir_mode=0775,iocharset=utf8,vers=3.0,_netdev,nofail 0 0"
sed -i "\#[[:space:]]${HOSTMNT}[[:space:]]#d" /etc/fstab
echo "$FSTAB_LINE" >> /etc/fstab
systemctl daemon-reload || true
mountpoint -q "$HOSTMNT" && umount "$HOSTMNT" || true
mount "$HOSTMNT" || die "Mounting the Synology share failed — check user/password and that SMB is enabled"
ls "$HOSTMNT"
touch "$HOSTMNT/.tgmb_write_test" && rm -f "$HOSTMNT/.tgmb_write_test" || die "No write permission on the share for ${SMB_USER}"

# ---------- 2. create container
TEMPLATE="${TEMPLATE:-$(pveam list local 2>/dev/null | awk '/debian-12-standard/ {print $1}' | sort -V | tail -1)}"
if [ -z "$TEMPLATE" ]; then
  say "Downloading Debian 12 template"
  pveam update >/dev/null
  T=$(pveam available --section system | awk '/debian-12-standard/ {print $2}' | sort -V | tail -1)
  pveam download local "$T"
  TEMPLATE="local:vztmpl/$T"
fi
EXIST=$(pct list | awk '$NF=="tg-media-bot" {print $1}')
if [ -n "$EXIST" ]; then
  CTID="$EXIST"
  say "Reusing existing container $CTID"
else
  CTID="${CTID:-$(pvesh get /cluster/nextid)}"
  say "Creating container $CTID from $TEMPLATE"
  pct create "$CTID" "$TEMPLATE" \
    --hostname tg-media-bot --cores 2 --memory 1024 --swap 512 \
    --rootfs "${STORAGE}:32" --net0 "name=eth0,bridge=${BRIDGE},ip=dhcp" \
    --unprivileged 1 --features nesting=1 --onboot 1 --ostype debian
fi
pct set "$CTID" -mp0 "${HOSTMNT},mp=/media/video"
pct status "$CTID" | grep -q running || pct start "$CTID"
say "Waiting for network"
for i in $(seq 1 30); do pct exec "$CTID" -- ping -c1 -W1 deb.debian.org >/dev/null 2>&1 && break; sleep 2; done

# ---------- 3. install app inside container
say "Installing packages"
pct exec "$CTID" -- bash -c "export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && apt-get install -y -qq python3-venv python3-pip curl ca-certificates locales >/dev/null"
pct exec "$CTID" -- bash -c "sed -i 's/^# *en_US.UTF-8/en_US.UTF-8/' /etc/locale.gen && locale-gen >/dev/null && update-locale LANG=en_US.UTF-8"
pct exec "$CTID" -- bash -c "mkdir -p /opt/tgmedia && cd /opt/tgmedia && \
  curl -fsSL '${REPO_RAW}/bot.py' -o bot.py && \
  curl -fsSL '${REPO_RAW}/requirements.txt' -o requirements.txt && \
  python3 -m venv venv && venv/bin/pip install -q --upgrade pip && venv/bin/pip install -q -r requirements.txt"

say "Writing config"
CFG_JSON=$(python3 - <<PY
import json
print(json.dumps({
  "api_id": int("${API_ID}"),
  "api_hash": "${API_HASH}",
  "media_root": "/media/video",
  "jellyfin_url": "${JELLYFIN_URL}",
  "jellyfin_key": "${JELLYFIN_KEY}",
  "workers": 2,
  "default_sources": ["searchgram_bbot"],
  "folders": {"movies": "סרטים", "israeli": "סרטים ישראלים", "kids": "ילדים", "series": "סדרות"}
}, ensure_ascii=False, indent=2))
PY
)
printf '%s\n' "$CFG_JSON" | pct exec "$CTID" -- bash -c "umask 077; cat > /opt/tgmedia/config.json"

pct exec "$CTID" -- bash -c "cat > /etc/systemd/system/tg-media-bot.service <<'EOF'
[Unit]
Description=Telegram media downloader
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/tgmedia
Environment=LANG=en_US.UTF-8 PYTHONIOENCODING=utf-8
ExecStart=/opt/tgmedia/venv/bin/python /opt/tgmedia/bot.py
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload"

# ---------- 4. Telegram login (interactive, once)
if ! pct exec "$CTID" -- test -f /opt/tgmedia/session.session; then
  say "Telegram login — enter your phone (+972...), then the code Telegram sends you"
  pct exec "$CTID" -- bash -c "cd /opt/tgmedia && LANG=en_US.UTF-8 venv/bin/python bot.py --login" </dev/tty
fi

pct exec "$CTID" -- systemctl enable --now tg-media-bot >/dev/null 2>&1
pct exec "$CTID" -- systemctl restart tg-media-bot
sleep 5
pct exec "$CTID" -- systemctl is-active tg-media-bot >/dev/null && \
  say "✅ Done! Container ${CTID}. Check 'Saved Messages' in Telegram — the bot posted a hello." || \
  die "Service failed: pct exec ${CTID} -- journalctl -u tg-media-bot -n 50"
