#!/usr/bin/env bash
# tg-media-bot updater — run on the Proxmox HOST shell.
# Pulls latest bot.py, gives the bot a limited Proxmox API token, restarts.
set -euo pipefail
REPO_RAW="${REPO_RAW:-https://raw.githubusercontent.com/morbriga/morbriga/tg-media-bot}"
say() { echo -e "\n\e[1;36m==> $*\e[0m"; }

CTID=$(pct list | awk '$NF=="tg-media-bot" {print $1}')
[ -n "$CTID" ] || { echo "container tg-media-bot not found — run install.sh first"; exit 1; }

say "Updating code in container $CTID"
pct exec "$CTID" -- bash -c "cd /opt/tgmedia && curl -fsSL '${REPO_RAW}/bot.py?$(date +%s)' -o bot.py.new && \
  curl -fsSL '${REPO_RAW}/requirements.txt?$(date +%s)' -o requirements.txt && \
  venv/bin/pip install -q -r requirements.txt && mv bot.py.new bot.py"

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
