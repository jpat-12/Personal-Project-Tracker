#!/usr/bin/env bash
#
# deploy-statusboard.sh — serve the board + cross-device sync backend on a port
#
# Run this ON the backend host that your reverse proxy points at, e.g. a Caddy
# block like:
#     projects.example.com { reverse_proxy <backend-host>:8182 }
#
# It installs index.html + server.py to a web root and runs a hardened systemd
# service (Python stdlib only) that serves the board on $PORT and syncs state
# across devices via Server-Sent Events. Your reverse proxy terminates TLS.
#
# Usage:
#     sudo ./deploy-statusboard.sh [path-to-html]
# Defaults to ./index.html sitting next to this script.

set -euo pipefail

# ---------------- config (edit to taste, or override via env) ----------------
WEBROOT="${WEBROOT:-/srv/projects}"     # where the files live on disk
PORT="${PORT:-8182}"                    # must match your reverse_proxy port
BIND="${BIND:-0.0.0.0}"                 # listen addr; lock down with a firewall
SERVICE="${SERVICE:-statusboard}"       # systemd unit + StateDirectory name
SRC="${1:-index.html}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ---------------- preflight ----------------
if [[ $EUID -ne 0 ]]; then
  echo "Please run with sudo:  sudo $0 ${1:-}" >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 (3.7+) not found. Install it (e.g. 'apt install -y python3') and re-run." >&2
  exit 1
fi
if [[ ! -f "$SRC" ]]; then
  echo "Can't find HTML file '$SRC'. Run from the repo root or pass its path." >&2
  exit 1
fi
if [[ ! -f "$SCRIPT_DIR/server.py" ]]; then
  echo "Can't find server.py next to this script ($SCRIPT_DIR)." >&2
  exit 1
fi

PY="$(command -v python3)"

# ---------------- install files ----------------
echo ">> Installing app -> $WEBROOT"
install -d -m 0755 "$WEBROOT"
install -m 0644 "$SRC" "$WEBROOT/index.html"
install -m 0644 "$SCRIPT_DIR/server.py" "$WEBROOT/server.py"

# ---------------- systemd unit ----------------
echo ">> Writing /etc/systemd/system/${SERVICE}.service"
cat > "/etc/systemd/system/${SERVICE}.service" <<EOF
[Unit]
Description=Project Statusboard (static + cross-device sync)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=WEBROOT=${WEBROOT} PORT=${PORT} BIND=${BIND}
ExecStart=${PY} ${WEBROOT}/server.py
Restart=on-failure
RestartSec=2

# Writable per-service state dir -> /var/lib/${SERVICE}/state.json (\$STATE_DIRECTORY)
StateDirectory=${SERVICE}

# --- hardening: throwaway user, read-only filesystem except the state dir ---
DynamicUser=yes
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadOnlyPaths=${WEBROOT}

[Install]
WantedBy=multi-user.target
EOF

# ---------------- start it ----------------
echo ">> Enabling and starting ${SERVICE}"
systemctl daemon-reload
systemctl enable "${SERVICE}.service" >/dev/null 2>&1 || true
systemctl restart "${SERVICE}.service"   # start, and pick up new code on re-deploys

sleep 1
systemctl --no-pager --lines=0 status "${SERVICE}.service" || true

echo
echo "Done. Serving ${WEBROOT} on ${BIND}:${PORT} with cross-device sync."
echo "Shared state: /var/lib/${SERVICE}/state.json"
echo "Local check:  curl -sI http://127.0.0.1:${PORT}/ | head -n1        (expect 200)"
echo "Sync check:   curl -s  http://127.0.0.1:${PORT}/api/state          (expect JSON)"
echo
echo "To update: 'git pull' then re-run this script (state is preserved)."
