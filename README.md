# Project Statusboard

A single-file, dependency-free portfolio statusboard. Track projects by status
(Active / Blocked / Shipped / Backlog), tap milestones to complete them, and
watch per-project and portfolio progress bars fill in. State is saved to the
browser via `localStorage` — no backend, no database.

- **One file for the UI.** Everything (HTML/CSS/JS + fonts link) lives in `index.html`.
- **Responsive.** Phone layout (stacked, swipeable filter rail) and a desktop
  layout (fixed sidebar + masonry card grid that flows into 2–3 columns).
- **Editable.** Your projects and milestones are a plain `SEED` array near the
  top of the `<script>` block — edit it to make the board yours.
- **Cross-device sync (optional).** When served by the included `server.py`,
  milestone/status changes save server-side and push to every open device in
  real time (Server-Sent Events). Opened without that backend, it silently
  falls back to per-browser `localStorage` — nothing breaks.

## Quick look

Open `index.html` in any browser. That's it. To customize, edit the `SEED`
array and reload.

## Deploy behind a reverse proxy

`deploy-statusboard.sh` sets up a hardened `systemd` service that runs
`server.py` (Python stdlib only — no dependencies) on a port of your choosing
(default `8182`) for a reverse proxy to sit in front of. `server.py` serves the
board **and** provides the cross-device sync API.

```bash
git clone https://github.com/<you>/project-statusboard.git
cd project-statusboard
chmod +x deploy-statusboard.sh
sudo ./deploy-statusboard.sh
```

Point your proxy at it. Example Caddy block (Caddy auto-provisions TLS):

```caddy
projects.example.com {
    reverse_proxy <backend-host>:8182
}
```

Override defaults via env vars if needed:

```bash
sudo WEBROOT=/var/www/board PORT=9090 ./deploy-statusboard.sh
```

### How sync works

- `GET /api/state` — returns the shared board `{done, statusOverride, rev}`.
- `POST /api/op` — applies one change (`setDone` / `setStatus` / `reset`); the
  server is authoritative, so concurrent edits from different devices merge
  cleanly instead of clobbering.
- `GET /api/events` — a Server-Sent Events stream; the server pushes the full
  board to every connected device on each change.

Shared state is persisted to `/var/lib/<service>/state.json` (systemd
`StateDirectory`). Your filter selection stays per-device (it is not synced).
Any reverse proxy that streams SSE works; Caddy does so out of the box.

### Static-only (no sync)

Don't need sync? Serve `index.html` with anything (even `file://`) and it runs
in `localStorage`-only mode — progress is then per-browser, not shared.

## Updating

```bash
git pull
sudo ./deploy-statusboard.sh    # re-installs index.html + server.py, restarts service, keeps state
```

## License

MIT — see [LICENSE](LICENSE).
