# Personal Project Tracker

A self-hosted project tracker with a live web board and a Telegram front-end,
backed by SQLite. One small Python process (standard library only — no pip
installs) serves the board **and** listens to Telegram; both write the same
database and every change streams to open browsers in real time.

**Hierarchy:** `Category → Project → (Tasks & Sub-Projects → Tasks)`

- **SQLite is the source of truth** (`db.py`). The board is a live view you can
  also edit.
- **Live everywhere.** Add/rename/complete something on your phone, on another
  laptop, or via Telegram — every open board updates within a second (SSE), no
  refresh.
- **Telegram-fed.** Text your bot to add projects, add tasks, search, and mark
  things complete.
- **Three-state tasks.** Tap a task to advance it **To Do → Working → Completed**.
- **Responsive.** Phone layout and a desktop layout (sticky category sidebar +
  masonry cards). List or Gallery view, filter by category.

## Layout

```
index.html   the board UI (single file)
server.py    web server (static + /api/state, /api/op, /api/events) + Telegram poller
db.py        SQLite schema + data access (source of truth)
deploy.sh    installs it as a hardened systemd service
config.example.env   template for secrets (copy to config.env)
```

## Deploy

On the server (e.g. ILWG-Server2), behind a reverse proxy that terminates TLS:

```bash
git clone https://github.com/<you>/Personal-Project-Tracker.git
cd Personal-Project-Tracker
cp config.example.env config.env      # then edit config.env (see below)
chmod +x deploy.sh
sudo ./deploy.sh
```

Point your proxy at it — e.g. Caddy (auto-TLS, and it streams SSE out of the box):

```caddy
projects.example.com {
    reverse_proxy <backend-host>:8182
}
```

The service serves on `:8182`, keeps its database at
`/var/lib/tracker/tracker.db`, and reads secrets from `/etc/tracker/config.env`.

### Config / secrets

`config.env` (gitignored — never committed):

```
TELEGRAM_BOT_TOKEN=123456:ABC...     # from @BotFather; blank = Telegram disabled
TELEGRAM_CHAT_ID=7665804382          # your numeric chat id
TRACKER_PASSWORD=                    # blank = board is open; set it to require a login
PORT=8182
BIND=0.0.0.0
```

## Password protection

Set `TRACKER_PASSWORD` in `config.env` and restart the service to require a
login. It's a single shared password (no per-user accounts) — enter it once
and the board remembers you for 30 days via an HttpOnly session cookie.
`index.html` itself is always served (it holds no data), but `/api/state`,
`/api/op`, and `/api/events` all require a valid session, so the underlying
data is never exposed to an unauthenticated request. A **Log out** button
appears in the sidebar whenever a password is set.

```bash
sudo nano /etc/tracker/config.env    # set TRACKER_PASSWORD=your-passphrase
sudo systemctl restart tracker
```

## Telegram

Message your bot:

```
#TAK-CAP !High Radio config tool | needs the new repeater list
```

`#Category` and `!Priority` are optional (default `#Other` / `!Medium`). Commands:

| Command | Does |
|---|---|
| `#Cat !Pri Title \| Description` | Add a project to a category |
| `/task <ProjectID> <text>` | Add a task to a project |
| `/done <ProjectID>` | Mark a project Complete |
| `/status` | List active projects |
| `/find <keyword>` | Search projects |
| `/categories` | List categories |
| `/help` | Usage |

**⚠️ One poller per bot.** Telegram delivers each message to only one
`getUpdates` consumer. If you were running the Windows `claude_watcher.py`, stop
its Telegram polling (or give this service a separate bot token) — otherwise the
two will steal each other's messages.

## How sync works

- `GET /api/state` → `{categories, projects, tasks, rev}`.
- `POST /api/op` → one change (`addProject`, `updateProject`, `deleteProject`,
  `addTask`, `updateTask`, `deleteTask`, `addCategory`); the DB is authoritative.
- `GET /api/events` → Server-Sent Events; the server pushes full state on every
  change (from the web *or* Telegram).

## Updating

```bash
git pull
sudo ./deploy.sh      # reinstalls files, restarts service; database is preserved
```

## Operations

```bash
systemctl status tracker
journalctl -u tracker -f
```

## License

MIT — see [LICENSE](LICENSE).
