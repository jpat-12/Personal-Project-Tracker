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
  masonry cards). List, Gallery, or **Calendar** view, filter by category.
- **Due-date reminders + a daily Claude focus digest**, both pushed via Telegram.

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
cp config.example.env config.env      # then edit config.env (see below) — first install only
chmod +x deploy.sh
sudo ./deploy.sh
```

**⚠️ After the first install, `/etc/tracker/config.env` is the live config —
edit it directly, never the repo-local copy.** `deploy.sh` only seeds
`/etc/tracker/config.env` from the repo-local `config.env` (or the blank
template) the *first* time it's deployed; every redeploy after that leaves it
alone. To change a setting (add a Telegram token, an API key, a password),
edit it in place and restart:

```bash
sudo nano /etc/tracker/config.env
sudo systemctl restart tracker
```

A stale repo-local `config.env` left over from setup is otherwise harmless —
`deploy.sh` won't touch the live one again once it exists.

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

### Natural language (set `ANTHROPIC_API_KEY`)

Just say what you want — no syntax required:

```
add a high priority task to fix the radio config tool under TAK-CAP
mark the reporting dashboard project as done
what's still open under Infra?
```

Claude reads the message, looks up existing categories/projects with read-only
tools before acting (so it reuses `TAK-CAP` instead of creating `Tak Cap`),
calls the right tool (add/update project, add/update task, add category), and
replies with a short confirmation. If your message is ambiguous — which
project, which category — it asks a clarifying question back over Telegram
instead of guessing; the conversation keeps a short rolling memory per chat so
your answer is understood as a follow-up, not a new request. Calls the Claude
API directly over HTTPS (no SDK, no pip install) using `CLAUDE_MODEL`
(default `claude-opus-4-8`) — costs apply per message. Leave
`ANTHROPIC_API_KEY` blank to fall back to the syntax below.

### Rigid syntax (fallback, or when no API key is set)

```
#TAK-CAP !High Radio config tool | needs the new repeater list
```

`#Category` and `!Priority` are optional (default `#Other` / `!Medium`).

### Commands (always available, both modes)

| Command | Does |
|---|---|
| `#Cat !Pri Title \| Description` | Add a project to a category (fallback mode only) |
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

## Calendar view

Toggle **Calendar** in the toolbar for a month grid of everything with a due
date — projects and tasks alike, color-coded (amber = due within a week, red =
overdue). Click an item to jump to it: a project opens directly; a task opens
its parent project, or filters to its category if it isn't attached to a
project. Prev/Next/Today navigate months; the displayed month resets to
today's on reload (not persisted).

## Reminders & daily digest

Both need Telegram configured; the digest additionally needs `ANTHROPIC_API_KEY`.

- **Due-date reminders** — a background check (every 30 min) sends one
  Telegram message per project/task the first time it's within
  `REMINDER_DAYS_BEFORE` days of its due date (default 4), including anything
  already overdue. Each item is only ever reminded once per due date — if you
  push the date out, it can remind again.
- **Daily focus digest** — once a day, at `DAILY_DIGEST_HOUR` (local server
  time, default 8), Claude reviews everything open on the board — overdue and
  due-soon items, priorities, what's currently "Working" — and sends a short,
  decisive Telegram message on what to focus on that day. Fires at most once
  per calendar day.

## GitHub integration

Needs `GITHUB_WEBHOOK_SECRET` **and** `ANTHROPIC_API_KEY` — there's no
non-Claude fallback here, since deciding what a commit/PR/issue means needs
real interpretation.

1. In your GitHub repo (or org) → **Settings → Webhooks → Add webhook**:
   - Payload URL: `https://<your-domain>/api/github-webhook`
   - Content type: `application/json`
   - Secret: same value as `GITHUB_WEBHOOK_SECRET`
   - Events: "Send me everything", or at least Pushes, Pull requests, Issues,
     Releases
2. GitHub sends a `ping` on save — check `journalctl -u tracker | grep github`
   for `ping received (setup OK)` to confirm the secret and URL are right.

From there, every push (to the default branch), opened/merged PR, opened/closed
issue, and published release gets summarized and handed to Claude, which
decides whether it's worth a tracker change:

- **Linking** — a project has an optional `repo` field (`owner/repo`), editable
  manually in the project modal or set automatically by Claude the first time
  it confidently matches an event to an existing project by name.
- **What it can do** — link a repo to a project, create a new project for a
  repo with no match (using judgment — a single trivial commit usually isn't
  enough), mark a task done when a commit/PR/issue clearly references
  finishing it, and nudge project status on a merged PR or published release.
- **When it does nothing** — there's no one to ask a clarifying question here,
  so if Claude isn't confident which project an event relates to, it does
  nothing rather than guess. Only actual tracker changes send a Telegram
  notification (prefixed 🐙) — silence means nothing happened, check the logs
  if you want to know why.

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
