# Project Statusboard

A single-file, dependency-free portfolio statusboard. Track projects by status
(Active / Blocked / Shipped / Backlog), tap milestones to complete them, and
watch per-project and portfolio progress bars fill in. State is saved to the
browser via `localStorage` — no backend, no database.

- **One file.** Everything (HTML/CSS/JS + fonts link) lives in `index.html`.
- **Responsive.** Phone layout (stacked, swipeable filter rail) and a desktop
  layout (fixed sidebar + masonry card grid that flows into 2–3 columns).
- **Editable.** Your projects and milestones are a plain `SEED` array near the
  top of the `<script>` block — edit it to make the board yours.

## Quick look

Open `index.html` in any browser. That's it. To customize, edit the `SEED`
array and reload.

## Deploy behind a reverse proxy

The board is static, so any web server works. `deploy-statusboard.sh` sets up a
hardened `systemd` service that serves `index.html` on a port of your choosing
(default `8182`) for a reverse proxy to sit in front of.

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

### Serve it directly with Caddy (no separate backend)

If Caddy runs on the same box, skip the service entirely and let Caddy serve the
file:

```caddy
projects.example.com {
    root * /srv/projects
    file_server
}
```

## Updating

```bash
git pull
sudo ./deploy-statusboard.sh    # re-copies index.html into place
```

`localStorage` is per-origin, so progress is saved per-browser at the URL you
open — it is not shared across devices.

## License

MIT — see [LICENSE](LICENSE).
