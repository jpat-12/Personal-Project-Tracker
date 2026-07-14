#!/usr/bin/env bash
#
# deploy-statusboard.sh — serve index.html as a static site on a chosen port
#
# Run this ON the backend host that your reverse proxy points at, e.g. a Caddy
# block like:
#     projects.example.com { reverse_proxy <backend-host>:8182 }
#
# It installs index.html to a web root and runs a hardened systemd service that
# serves it on $PORT. Your reverse proxy (Caddy/nginx/Traefik) terminates TLS
# out front; this box just serves static files on the LAN.
#
# Usage:
#     sudo ./deploy-statusboard.sh [path-to-html]
# Defaults to ./index.html sitting next to this script.

set -euo pipefail

# ---------------- config (edit to taste, or override via env) ----------------
WEBROOT="${WEBROOT:-/srv/projects}"     # where the file lives on disk
PORT="${PORT:-8182}"                    # must match your reverse_proxy port
BIND="${BIND:-0.0.0.0}"                 # listen addr; lock down with a firewall
SERVICE="${SERVICE:-statusboard}"       # systemd unit name
SRC="${1:-index.html}"

# ---------------- preflight ----------------
if [[ $EUID -ne 0 ]]; then
  echo "Please run with sudo:  sudo $0 ${1:-}" >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found. Install it (e.g. 'apt install -y python3') and re-run." >&2
  exit 1
fi
if [[ ! -f "$SRC" ]]; then
  echo "Can't find HTML file '$SRC'." >&2
  echo "Run this from the repo root, or pass the path to your HTML as an argument." >&2
  exit 1
fi

PY="$(command -v python3)"

# ---------------- install the file ----------------
echo ">> Installing $SRC -> $WEBROOT/index.html"
install -d -m 0755 "$WEBROOT"
install -m 0644 "$SRC" "$WEBROOT/index.html"

# ---------------- systemd unit ----------------
echo ">> Writing /etc/systemd/system/${SERVICE}.service"
cat > "/etc/systemd/system/${SERVICE}.service" <<EOF
[Unit]
Description=Project Statusboard (static file server)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=${PY} -m http.server ${PORT} --bind ${BIND} --directory ${WEBROOT}
Restart=on-failure
RestartSec=2

# --- hardening: runs as a throwaway user with read-only access ---
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
systemctl enable --now "${SERVICE}.service"

sleep 1
systemctl --no-pager --lines=0 status "${SERVICE}.service" || true

echo
echo "Done. Serving ${WEBROOT}/index.html on ${BIND}:${PORT}"
echo "Local check:  curl -sI http://127.0.0.1:${PORT}/ | head -n1"
echo
echo "To update: 'git pull' then re-run this script (or re-copy index.html to ${WEBROOT})."
