#!/usr/bin/env bash
# tg-media-bot updater — run on the Proxmox HOST shell.
# Pulls latest bot.py, gives the bot a limited Proxmox API token, restarts.
set -uo pipefail
trap 'echo -e "\n\e[1;31m✖ failed at line $LINENO (exit $?)\e[0m"; exit 1' ERR
set -E
REPO_RAW="${REPO_RAW:-https://raw.githubusercontent.com/morbriga/morbriga/tg-media-bot}"
say() { echo -e "\n\e[1;36m==> $*\e[0m"; }

CTID=$(pct list | awk '$NF=="tg-media-bot" {print $1}')
[ -n "$CTID" ] || { echo "container tg-media-bot not found — run install.sh first"; exit 1; }

say "Updating code in container $CTID"
pct exec "$CTID" -- curl -fSL --retry 3 -o /opt/tgmedia/bot.py.new "${REPO_RAW}/bot.py"
pct exec "$CTID" -- curl -fSL --retry 3 -o /opt/tgmedia/requirements.txt "${REPO_RAW}/requirements.txt"
pct exec "$CTID" -- /opt/tgmedia/venv/bin/python -m py_compile /opt/tgmedia/bot.py.new
pct exec "$CTID" -- /opt/tgmedia/venv/bin/pip install -r /opt/tgmedia/requirements.txt --disable-pip-version-check -q
pct exec "$CTID" -- mv /opt/tgmedia/bot.py.new /opt/tgmedia/bot.py
echo "code updated"

if ! pct exec "$CTID" -- bash -c "command -v ffmpeg && command -v arp-scan" >/dev/null 2>&1; then
  say "Installing ffmpeg (YouTube) and arp-scan (network scanner)"
  pct exec "$CTID" -- bash -c "DEBIAN_FRONTEND=noninteractive apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ffmpeg arp-scan >/dev/null"
fi
pct exec "$CTID" -- ln -sf /usr/share/zoneinfo/Asia/Jerusalem /etc/localtime
pct exec "$CTID" -- mkdir -p "/media/video/YouTube/ילדים"

if ! pct exec "$CTID" -- test -f /etc/systemd/system/qbittorrent.service; then
  say "Installing qBittorrent (localhost only, controlled by the bot)"
  pct exec "$CTID" -- bash -c "DEBIAN_FRONTEND=noninteractive apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq qbittorrent-nox >/dev/null"
  pct exec "$CTID" -- mkdir -p /opt/qbt/qBittorrent/config /media/video/.downloads
  pct exec "$CTID" -- bash -c "cat > /opt/qbt/qBittorrent/config/qBittorrent.conf <<'EOF'
[LegalNotice]
Accepted=true

[BitTorrent]
Session\DefaultSavePath=/media/video/.downloads
Session\QueueingSystemEnabled=true
Session\MaxActiveDownloads=3
Session\MaxActiveTorrents=5
Session\GlobalMaxRatio=0

[Preferences]
Downloads\SavePath=/media/video/.downloads/
WebUI\Address=127.0.0.1
WebUI\Port=8080
WebUI\LocalHostAuth=false
WebUI\HostHeaderValidation=false
WebUI\CSRFProtection=false
EOF
cat > /etc/systemd/system/qbittorrent.service <<'EOF'
[Unit]
Description=qBittorrent (headless)
After=network-online.target

[Service]
ExecStart=/usr/bin/qbittorrent-nox --profile=/opt/qbt
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload && systemctl enable --now qbittorrent"
  sleep 4
  pct exec "$CTID" -- curl -fsS http://127.0.0.1:8080/api/v2/app/version && echo "  ← qBittorrent OK"
fi

if ! pct exec "$CTID" -- grep -q '"pve_token"' /opt/tgmedia/config.json; then
  say "Creating limited Proxmox API user for the bot (view + start/stop only)"
  pveum role add TGBot -privs "VM.Audit,VM.PowerMgmt,Sys.Audit,Datastore.Audit" 2>/dev/null || \
    pveum role modify TGBot -privs "VM.Audit,VM.PowerMgmt,Sys.Audit,Datastore.Audit"
  pveum user add tgbot@pve --comment "Telegram control bot" 2>/dev/null || true
  pveum acl modify / -user tgbot@pve -role TGBot
  pveum user token remove tgbot@pve bot 2>/dev/null || true
  SECRET=$(pveum user token add tgbot@pve bot --privsep 0 --output-format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["value"])')
  HOSTIP=$(hostname -I | awk '{print $1}')
  CFG=$(pct exec "$CTID" -- cat /opt/tgmedia/config.json)
  NEW=$(printf '%s' "$CFG" | python3 -c "
import json,sys
c=json.load(sys.stdin)
c['pve_url']='https://${HOSTIP}:8006'
c['pve_token']='tgbot@pve!bot=${SECRET}'
print(json.dumps(c, ensure_ascii=False, indent=2))")
  printf '%s\n' "$NEW" | pct exec "$CTID" -- bash -c "umask 077; cat > /opt/tgmedia/config.json"
fi

pct exec "$CTID" -- systemctl restart tg-media-bot
sleep 6
pct exec "$CTID" -- systemctl is-active tg-media-bot >/dev/null && say "✅ Updated. Check Telegram." || \
  pct exec "$CTID" -- journalctl -u tg-media-bot -n 30 --no-pager
