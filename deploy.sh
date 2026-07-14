#!/usr/bin/env bash
#
# deploy.sh — install the Personal Project Tracker as a hardened systemd service.
#
# Run this ON the host that serves the board (e.g. ILWG-Server2), behind your
# reverse proxy:  projects.example.com { reverse_proxy <backend-host>:8182 }
#
# One Python process serves the board + JSON API + SSE, and (if a Telegram
# token is configured) polls Telegram and writes the same SQLite DB. Standard
# library only — no pip installs.
#
#   sudo ./deploy.sh
#
# Before first run: copy config.example.env -> config.env and fill in your
# Telegram token/chat id (or leave blank to disable Telegram).

set -euo pipefail

WEBROOT="${WEBROOT:-/srv/tracker}"     # where the app files live
PORT="${PORT:-8182}"                   # must match your reverse_proxy port
BIND="${BIND:-0.0.0.0}"
SERVICE="${SERVICE:-tracker}"          # systemd unit + StateDirectory name
CFG_DIR="/etc/${SERVICE}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ $EUID -ne 0 ]]; then
  echo "Please run with sudo:  sudo $0" >&2; exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 (3.7+) required. apt install -y python3" >&2; exit 1
fi
for f in index.html server.py db.py; do
  [[ -f "$SCRIPT_DIR/$f" ]] || { echo "Missing $f next to this script." >&2; exit 1; }
done
PY="$(command -v python3)"

echo ">> Installing app -> $WEBROOT"
install -d -m 0755 "$WEBROOT"
install -m 0644 "$SCRIPT_DIR/index.html" "$WEBROOT/index.html"
install -m 0644 "$SCRIPT_DIR/server.py"  "$WEBROOT/server.py"
install -m 0644 "$SCRIPT_DIR/db.py"      "$WEBROOT/db.py"

echo ">> Config -> $CFG_DIR/config.env"
install -d -m 0750 "$CFG_DIR"
if [[ -f "$SCRIPT_DIR/config.env" ]]; then
  install -m 0600 "$SCRIPT_DIR/config.env" "$CFG_DIR/config.env"
elif [[ ! -f "$CFG_DIR/config.env" ]]; then
  install -m 0600 "$SCRIPT_DIR/config.example.env" "$CFG_DIR/config.env"
  echo "   No config.env found — installed a blank template. Edit $CFG_DIR/config.env"
  echo "   to add TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID, then: sudo systemctl restart $SERVICE"
fi

echo ">> Writing /etc/systemd/system/${SERVICE}.service"
cat > "/etc/systemd/system/${SERVICE}.service" <<EOF
[Unit]
Description=Personal Project Tracker (board + Telegram, SQLite-backed)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=WEBROOT=${WEBROOT} PORT=${PORT} BIND=${BIND}
EnvironmentFile=-${CFG_DIR}/config.env
ExecStart=${PY} ${WEBROOT}/server.py
Restart=on-failure
RestartSec=2

# DB + WAL live here (writable even under ProtectSystem=strict): /var/lib/${SERVICE}/tracker.db
StateDirectory=${SERVICE}

# hardening
DynamicUser=yes
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadOnlyPaths=${WEBROOT}

[Install]
WantedBy=multi-user.target
EOF

echo ">> Enabling and (re)starting ${SERVICE}"
systemctl daemon-reload
systemctl enable "${SERVICE}.service" >/dev/null 2>&1 || true
systemctl restart "${SERVICE}.service"

sleep 1
systemctl --no-pager --lines=0 status "${SERVICE}.service" || true

echo
echo "Done. Board + API on ${BIND}:${PORT}.  DB: /var/lib/${SERVICE}/tracker.db"
echo "Checks:  curl -sI http://127.0.0.1:${PORT}/ | head -n1        (expect 200)"
echo "         curl -s  http://127.0.0.1:${PORT}/api/state          (expect JSON)"
echo "Logs:    journalctl -u ${SERVICE} -f"
echo
echo "Telegram: set token/chat in ${CFG_DIR}/config.env then 'sudo systemctl restart ${SERVICE}'."
echo "IMPORTANT: only ONE process may poll a given Telegram bot — stop the old"
echo "Windows claude_watcher.py Telegram polling (or use a separate bot token)."
