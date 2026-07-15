#!/usr/bin/env python3
"""
server.py — web board + Telegram poller for the Personal Project Tracker.

One process, standard library only:
  - serves index.html and a JSON API backed by SQLite (db.py, the source of truth)
  - GET  /api/state   -> {categories, projects, tasks, rev, authRequired}
  - POST /api/op      -> mutate (addProject/updateProject/deleteProject/addTask/
                          updateTask/deleteTask/addCategory/renameCategory/
                          deleteCategory/updateCategory/reorderTasks/
                          reorderProjects/reorderCategories) {..., originId}
  - GET  /api/events  -> Server-Sent Events; pushes full state on every change
  - POST /api/login, /api/logout -> session cookie auth (see below)
  - background thread polls Telegram and writes the same DB (see telegram.py-style
    logic below); its changes broadcast to the board live too.

Config via env (see config.example.env):
  WEBROOT (.)  PORT (8182)  BIND (0.0.0.0)
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   (optional — omit to disable Telegram)
  ANTHROPIC_API_KEY, CLAUDE_MODEL         (optional — omit to keep the old rigid
                                           #Category !Priority parser; set the key
                                           to let Claude parse free-form messages,
                                           call tools, and ask clarifying questions)
  TRACKER_PASSWORD                       (optional — omit to leave the board open)
DB path: $TRACKER_DB or $STATE_DIRECTORY/tracker.db

Auth (only active if TRACKER_PASSWORD is set): a single shared password gates
/api/state, /api/op and /api/events. POST /api/login sets an HttpOnly session
cookie (30 days); POST /api/logout clears it. index.html itself is always
served (it holds no data) so the SPA can show its own login screen.
"""

import hmac
import json
import mimetypes
import os
import posixpath
import queue
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import db

WEBROOT = os.path.abspath(os.environ.get("WEBROOT", "."))
PORT = int(os.environ.get("PORT", "8182"))
BIND = os.environ.get("BIND", "0.0.0.0")

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-8").strip()

TRACKER_PASSWORD = os.environ.get("TRACKER_PASSWORD", "").strip()
COOKIE_NAME = "tracker_session"
SESSION_TTL = 60 * 60 * 24 * 30  # 30 days
SESSIONS = {}                     # token -> expiry epoch seconds (in-memory; cleared on restart)
_sess_lock = threading.Lock()

_subscribers = set()  # set[queue.Queue]


def log(msg):
    sys.stderr.write(f"[tracker] {msg}\n")
    sys.stderr.flush()


# ── broadcast ─────────────────────────────────────────────────────────────

def public_state():
    state = db.get_state()
    state["authRequired"] = bool(TRACKER_PASSWORD)
    return state


def push_state(origin=None):
    state = public_state()
    state["originId"] = origin
    payload = "data: " + json.dumps(state) + "\n\n"
    for q in list(_subscribers):
        try:
            q.put_nowait(payload)
        except Exception:
            _subscribers.discard(q)


# ── API op dispatch ───────────────────────────────────────────────────────

def apply_op(op):
    """Return True if the board changed. Raises nothing the caller can't handle."""
    kind = op.get("op")
    if kind == "addProject":
        if not (op.get("name") or "").strip():
            return False
        db.add_project(op.get("category") or "", op["name"].strip(),
                       description=op.get("description", ""), status=op.get("status", "Planning"),
                       priority=op.get("priority", "Medium"), due_date=op.get("due_date") or None,
                       parent_id=op.get("parent_id") or None, tags=op.get("tags") or [])
    elif kind == "updateProject":
        if not op.get("id"):
            return False
        db.update_project(op["id"], op.get("patch", {}))
    elif kind == "deleteProject":
        if not op.get("id"):
            return False
        db.delete_project(op["id"])
    elif kind == "addTask":
        name = (op.get("name") or "").strip()
        project_id = op.get("project_id") or None
        category = op.get("category") or None
        if not name or not (project_id or category):
            return False
        db.add_task(name, project_id=project_id, category=category,
                    state=op.get("state", "todo"), due_date=op.get("due_date") or None)
    elif kind == "updateTask":
        if not op.get("id"):
            return False
        db.update_task(op["id"], op.get("patch", {}))
    elif kind == "deleteTask":
        if not op.get("id"):
            return False
        db.delete_task(op["id"])
    elif kind == "addCategory":
        if not (op.get("name") or "").strip():
            return False
        db.add_category(op["name"].strip(), op.get("parent") or None)
    elif kind == "renameCategory":
        if not (op.get("old") and (op.get("new") or "").strip()):
            return False
        db.rename_category(op["old"], op["new"].strip())
    elif kind == "deleteCategory":
        if not op.get("name"):
            return False
        db.delete_category(op["name"])
    elif kind == "updateCategory":
        if not op.get("name"):
            return False
        db.update_category(op["name"], op.get("patch", {}))
    elif kind == "reorderTasks":
        ids = op.get("ids")
        if not isinstance(ids, list) or not ids:
            return False
        db.reorder_tasks(ids)
    elif kind == "reorderProjects":
        ids = op.get("ids")
        if not isinstance(ids, list) or not ids:
            return False
        db.reorder_projects(ids)
    elif kind == "reorderCategories":
        names = op.get("names")
        if not isinstance(names, list) or not names:
            return False
        db.reorder_categories(names)
    else:
        return False
    return True


# ── HTTP handler ──────────────────────────────────────────────────────────

def content_type(path):
    ctype, _ = mimetypes.guess_type(path)
    if ctype is None:
        ctype = "application/octet-stream"
    if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
        ctype += "; charset=utf-8"
    return ctype


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _get_token(self):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        c = SimpleCookie()
        try:
            c.load(raw)
        except Exception:
            return None
        m = c.get(COOKIE_NAME)
        return m.value if m else None

    def _is_authed(self):
        if not TRACKER_PASSWORD:
            return True
        tok = self._get_token()
        if not tok:
            return False
        with _sess_lock:
            exp = SESSIONS.get(tok)
        return bool(exp and exp > time.time())

    def _set_session_cookie(self, token, max_age):
        c = SimpleCookie()
        c[COOKIE_NAME] = token
        c[COOKIE_NAME]["httponly"] = True
        c[COOKIE_NAME]["secure"] = True
        c[COOKIE_NAME]["samesite"] = "Lax"
        c[COOKIE_NAME]["path"] = "/"
        c[COOKIE_NAME]["max-age"] = max_age
        self.send_header("Set-Cookie", c[COOKIE_NAME].OutputString())

    def _clear_session_cookie(self):
        c = SimpleCookie()
        c[COOKIE_NAME] = ""
        c[COOKIE_NAME]["path"] = "/"
        c[COOKIE_NAME]["max-age"] = 0
        self.send_header("Set-Cookie", c[COOKIE_NAME].OutputString())

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/state":
            if not self._is_authed():
                self._json({"ok": False, "error": "auth"}, 401)
                return
            self._json(public_state())
            return
        if path == "/api/events":
            if not self._is_authed():
                self.send_response(401)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._sse()
            return
        self._static(path)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/login":
            self._handle_login()
            return
        if path == "/api/logout":
            self._handle_logout()
            return
        if path != "/api/op":
            self.send_error(404)
            return
        if not self._is_authed():
            self._json({"ok": False, "error": "auth"}, 401)
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            op = json.loads(raw.decode("utf-8"))
        except Exception:
            self._json({"ok": False, "error": "bad json"}, 400)
            return
        try:
            ok = apply_op(op)
        except Exception as e:
            log(f"op error: {e}")
            self._json({"ok": False, "error": "op failed"}, 500)
            return
        if ok:
            push_state(op.get("originId"))
        self._json({"ok": ok, "rev": db.get_rev()})

    def _handle_login(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = {}
        pw = str(body.get("password", ""))
        if not TRACKER_PASSWORD:
            self._json({"ok": True})
            return
        if not hmac.compare_digest(pw, TRACKER_PASSWORD):
            self._json({"ok": False, "error": "bad password"}, 401)
            return
        token = secrets.token_urlsafe(32)
        now = time.time()
        with _sess_lock:
            for k in [k for k, exp in SESSIONS.items() if exp <= now]:
                del SESSIONS[k]
            SESSIONS[token] = now + SESSION_TTL
        body_bytes = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.send_header("Cache-Control", "no-store")
        self._set_session_cookie(token, SESSION_TTL)
        self.end_headers()
        self.wfile.write(body_bytes)

    def _handle_logout(self):
        tok = self._get_token()
        if tok:
            with _sess_lock:
                SESSIONS.pop(tok, None)
        body_bytes = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.send_header("Cache-Control", "no-store")
        self._clear_session_cookie()
        self.end_headers()
        self.wfile.write(body_bytes)

    def _sse(self):
        self.close_connection = True
        q = queue.Queue(maxsize=128)
        _subscribers.add(q)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            state = public_state()
            state["originId"] = None
            self.wfile.write(("data: " + json.dumps(state) + "\n\n").encode("utf-8"))
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=20)
                    self.wfile.write(msg.encode("utf-8"))
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            _subscribers.discard(q)

    def _static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        rel = posixpath.normpath(urllib.parse.unquote(path)).lstrip("/")
        full = os.path.abspath(os.path.join(WEBROOT, rel))
        if full != WEBROOT and not full.startswith(WEBROOT + os.sep):
            self.send_error(403)
            return
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
        if not os.path.isfile(full):
            self.send_error(404)
            return
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type(full))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


# ── Telegram poller (task tracking only; no Claude execution) ──────────────

TG_API = f"https://api.telegram.org/bot{TG_TOKEN}" if TG_TOKEN else None
DEFAULT_CATEGORY = ""          # "" = uncategorized (clean slate has no categories)
DEFAULT_PRIORITY = "Medium"
VALID_PRIORITIES = {"critical", "high", "medium", "low"}


def _cat_aliases():
    return {c.lower().replace("-", " ").replace("_", " "): c for c in db.category_names()}


def tg_call(method, params):
    url = f"{TG_API}/{method}"
    data = json.dumps(params).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode("utf-8"))


def tg_reply(text):
    try:
        tg_call("sendMessage", {"chat_id": TG_CHAT, "text": text})
    except Exception as e:
        log(f"telegram reply failed: {e}")


def parse_task_message(text):
    """'#Category !Priority Title | Description' -> (category, priority, title, description)."""
    aliases = _cat_aliases()
    tokens = text.strip().split()
    category, priority = DEFAULT_CATEGORY, DEFAULT_PRIORITY
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("#") and len(tok) > 1:
            key = tok[1:].lower().replace("-", " ").replace("_", " ")
            category = aliases.get(key, tok[1:])   # known -> canonical; unknown -> new category (auto-created)
            i += 1; continue
        if tok.startswith("!"):
            key = tok[1:].lower()
            if key in VALID_PRIORITIES:
                priority = key.capitalize(); i += 1; continue
        break
    rest = " ".join(tokens[i:]).strip()
    if not rest:
        return None
    if "|" in rest:
        title, desc = rest.split("|", 1)
        return category, priority, title.strip(), desc.strip()
    return category, priority, rest, ""


def tg_cmd_help():
    cats = "\n".join(f"  #{c}" for c in db.category_names())
    commands = (
        "Commands:\n"
        "/status - active projects\n"
        "/find <keyword> - search\n"
        "/task <ProjectID> <task text> - add a task to a project\n"
        "/done <ProjectID> - mark a project Complete\n"
        "/categories - list categories")
    if ANTHROPIC_API_KEY:
        tg_reply(
            "Just tell me what you want in plain English — e.g. \"add a high priority "
            "task to fix the radio config tool under TAK-CAP\" or \"mark the reporting "
            "dashboard project as done\". I'll ask if I need more info.\n\n" + commands)
    else:
        tg_reply(
            "Add a project:\n"
            "#TAK-CAP !High Radio config tool | Needs the new repeater list\n\n"
            f"Categories:\n{cats}\n\n"
            "Priorities: !Critical !High !Medium !Low (default Medium)\n\n" + commands)


def tg_cmd_status():
    tops = [p for p in db.summary() if p["status"] != "Complete"]
    if not tops:
        tg_reply("No active projects.")
        return
    lines = [f"{p['id']} [{p['category']}] {p['name']} - {p['status']}/{p['priority']}" for p in tops[:20]]
    tg_reply(f"Active projects ({len(tops)}):\n" + "\n".join(lines))


def tg_cmd_find(kw):
    if not kw:
        tg_reply("Usage: /find <keyword>"); return
    m = db.find_projects(kw)
    if not m:
        tg_reply(f'No projects matching "{kw}".'); return
    tg_reply(f"Found {len(m)}:\n" + "\n".join(f"{p['id']} [{p['category']}] {p['name']} - {p['status']}" for p in m))


def tg_cmd_done(pid):
    if not pid:
        tg_reply("Usage: /done <ProjectID>, e.g. /done TC-001"); return
    p = db.get_project(pid.upper())
    if not p:
        tg_reply(f"No project with ID {pid}."); return
    db.update_project(p["id"], {"status": "Complete"})
    tg_reply(f"✅ Marked {p['id']} ({p['name']}) Complete.")


def tg_cmd_task(rest):
    parts = rest.split(None, 1)
    if len(parts) < 2:
        tg_reply("Usage: /task <ProjectID> <task text>"); return
    pid, text = parts[0].upper(), parts[1].strip()
    p = db.get_project(pid)
    if not p:
        tg_reply(f"No project with ID {pid}."); return
    db.add_task(text, project_id=p["id"])
    tg_reply(f'➕ Added task to {p["id"]}: "{text}"')


# ── Claude-powered natural-language Telegram interface ─────────────────────
# Only active when ANTHROPIC_API_KEY is set; otherwise falls back to the rigid
# #Category !Priority parser above. Raw HTTPS via urllib (no SDK / pip install)
# to keep this project stdlib-only, matching the rest of server.py.

CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MAX_ITERS = 6          # tool-call round trips per incoming message
CLAUDE_HISTORY_TURNS = 24     # rolling window of messages kept per chat

_convo_lock = threading.Lock()
_conversations = {}  # chat_id -> list[{"role": ..., "content": ...}]

CLAUDE_TOOLS = [
    {
        "name": "list_categories",
        "description": "List all existing categories (and their parent, for sub-categories). Call this before creating a new category if you're not sure one already exists.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "search_projects",
        "description": "Search existing projects by name/description/ID keyword. Use this to find a project's ID before updating it, adding a task to it, or marking it done — never guess an ID.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Keyword to search for"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "add_project",
        "description": "Create a new project. Prefer reusing an existing category name (from list_categories) over inventing a new one, unless the user clearly wants a new one.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Project name/title"},
                "category": {"type": "string", "description": "Category name; omit or empty string for uncategorized"},
                "description": {"type": "string"},
                "status": {"type": "string", "enum": ["Planning", "Active", "Blocked", "Review", "Complete", "Backlog"]},
                "priority": {"type": "string", "enum": ["Critical", "High", "Medium", "Low"]},
                "due_date": {"type": "string", "description": "ISO date YYYY-MM-DD"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "update_project",
        "description": "Update an existing project. Get project_id from search_projects first. Only include fields that should change.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "name": {"type": "string"},
                "category": {"type": "string"},
                "status": {"type": "string", "enum": ["Planning", "Active", "Blocked", "Review", "Complete", "Backlog"]},
                "priority": {"type": "string", "enum": ["Critical", "High", "Medium", "Low"]},
                "due_date": {"type": "string", "description": "ISO date YYYY-MM-DD"},
                "description": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["project_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "add_task",
        "description": "Add a task, either to a project (project_id, from search_projects) or directly to a category (category name) — provide exactly one of the two, never both.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Task text"},
                "project_id": {"type": "string"},
                "category": {"type": "string"},
                "due_date": {"type": "string", "description": "ISO date YYYY-MM-DD"},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "update_task",
        "description": "Update a task's name, state, or due date. Get task_id via search_projects context or by asking the user for the project so you can look it up — if you don't know the task_id, ask the user instead of guessing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "name": {"type": "string"},
                "state": {"type": "string", "enum": ["todo", "doing", "done"]},
                "due_date": {"type": "string", "description": "ISO date YYYY-MM-DD"},
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "add_category",
        "description": "Create a new category, optionally as a sub-category of an existing one.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "parent": {"type": "string", "description": "Existing parent category name, for a sub-category"},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
]


def claude_system_prompt():
    today = time.strftime("%Y-%m-%d (%A)")
    cats = db.category_names()
    cat_list = ", ".join(cats) if cats else "(none yet — any category you use will be created)"
    return (
        "You are the natural-language interface to a personal project tracker, reached via Telegram. "
        "The user will describe what they want in plain English — do not require any special syntax. "
        f"Today's date is {today}. Existing categories: {cat_list}.\n\n"
        "Use the provided tools to make changes (add/update projects, tasks, categories) and to look "
        "things up (search_projects, list_categories) before acting — never guess a project_id or "
        "task_id; search for it first. Prefer reusing an existing category over creating a near-duplicate.\n\n"
        "If the request is ambiguous or missing something you need (which project, which category, "
        "what priority), do NOT call a tool — instead reply with a short, specific clarifying question "
        "in plain text. Once you have everything you need and have made the change(s), reply with a "
        "brief, friendly confirmation in plain text (no tool call) summarizing what you did. Keep all "
        "replies short — this is a Telegram chat, not a report."
    )


def claude_call(messages):
    body = {
        "model": CLAUDE_MODEL,
        "max_tokens": 1024,
        "system": claude_system_prompt(),
        "tools": CLAUDE_TOOLS,
        "messages": messages,
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        CLAUDE_API_URL,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def claude_run_tool(name, inp):
    """Execute one Claude tool call against db.py. Returns (result_text, changed)."""
    try:
        if name == "list_categories":
            cats = [{"name": c["name"], "parent": c.get("parent")} for c in db.get_state()["categories"]]
            return json.dumps(cats), False
        if name == "search_projects":
            q = inp.get("query", "")
            rows = db.find_projects(q, limit=10)
            out = [{"id": p["id"], "name": p["name"], "category": p["category"], "status": p["status"]} for p in rows]
            return json.dumps(out) if out else "No matches.", False
        if name == "add_project":
            pid = db.add_project(
                inp.get("category") or "", inp["name"],
                description=inp.get("description", ""), status=inp.get("status", "Planning"),
                priority=inp.get("priority", "Medium"), due_date=inp.get("due_date") or None,
                tags=inp.get("tags") or [])
            return f"Created project {pid}.", True
        if name == "update_project":
            pid = inp["project_id"]
            if not db.get_project(pid):
                return f"No project with ID {pid}.", False
            patch = {k: v for k, v in inp.items() if k not in ("project_id",)}
            db.update_project(pid, patch)
            return f"Updated project {pid}.", True
        if name == "add_task":
            project_id = inp.get("project_id") or None
            category = inp.get("category") or None
            if not project_id and not category:
                return "Need either project_id or category to add a task.", False
            if project_id and not db.get_project(project_id):
                return f"No project with ID {project_id}.", False
            tid = db.add_task(inp["name"], project_id=project_id, category=category,
                              due_date=inp.get("due_date") or None)
            return f"Added task {tid}.", True
        if name == "update_task":
            tid = inp["task_id"]
            patch = {k: v for k, v in inp.items() if k not in ("task_id",)}
            db.update_task(tid, patch)
            return f"Updated task {tid}.", True
        if name == "add_category":
            db.add_category(inp["name"], inp.get("parent") or None)
            return f"Created category {inp['name']}.", True
        return f"Unknown tool {name}.", False
    except Exception as e:
        return f"Error: {e}", False


def _get_history(chat_id):
    with _convo_lock:
        return list(_conversations.get(chat_id, []))


def _save_history(chat_id, messages):
    with _convo_lock:
        _conversations[chat_id] = messages[-CLAUDE_HISTORY_TURNS:]


def tg_handle_claude(text, chat_id):
    """Natural-language flow: Claude parses the message, calls tools, or asks
    a clarifying question. Returns True if any tool call changed the DB."""
    history = _get_history(chat_id)
    history.append({"role": "user", "content": text})
    changed_any = False

    for _ in range(CLAUDE_MAX_ITERS):
        try:
            resp = claude_call(history)
        except Exception as e:
            log(f"claude call failed: {e}")
            tg_reply("Sorry, I couldn't reach Claude just now — try again in a moment.")
            return changed_any

        if "error" in resp:
            log(f"claude error: {resp['error']}")
            tg_reply(f"Claude error: {resp['error'].get('message', 'unknown error')}")
            return changed_any

        content = resp.get("content", [])
        history.append({"role": "assistant", "content": content})

        tool_uses = [b for b in content if b.get("type") == "tool_use"]
        if not tool_uses:
            text_out = " ".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
            if text_out:
                tg_reply(text_out)
            _save_history(chat_id, history)
            return changed_any

        tool_results = []
        for tu in tool_uses:
            log(f"claude tool call: {tu['name']}({tu.get('input', {})})")
            result_text, changed = claude_run_tool(tu["name"], tu.get("input", {}))
            log(f"  -> {result_text}")
            changed_any = changed_any or changed
            tool_results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": result_text})
        history.append({"role": "user", "content": tool_results})

    tg_reply("That took more steps than expected — try rephrasing or breaking it into a simpler request.")
    _save_history(chat_id, history)
    return changed_any


def tg_handle(text):
    low = text.strip().lower()
    if low in ("/start", "/help"):
        tg_cmd_help(); return True
    if low in ("/categories", "/sheets", "/list"):
        tg_reply("Categories:\n" + "\n".join(f"  #{c}" for c in db.category_names())); return True
    if low == "/status":
        tg_cmd_status(); return True
    if low.startswith("/find"):
        tg_cmd_find(text.strip()[5:].strip()); return True
    if low.startswith("/task"):
        tg_cmd_task(text.strip()[5:].strip()); return True
    if low.startswith("/done"):
        tg_cmd_done(text.strip()[5:].strip()); return True

    if ANTHROPIC_API_KEY:
        log(f"routing to claude: {text!r}")
        return tg_handle_claude(text, TG_CHAT)

    log(f"claude disabled, using rigid parser for: {text!r}")
    parsed = parse_task_message(text)
    if not parsed:
        tg_reply("Couldn't parse that. Send /help for the format."); return False
    category, priority, title, desc = parsed
    pid = db.add_project(category, title, description=desc, priority=priority)
    tg_reply(f'✅ Added {pid} to {category or "Uncategorized"}: "{title}" ({priority}).')
    log(f"telegram added project {pid} [{category}] {title}")
    return True


def telegram_loop():
    log(f"telegram poller started (chat {TG_CHAT})")
    offset = int(db.meta_get("tg_offset", "0") or "0")
    while True:
        try:
            resp = tg_call("getUpdates", {"offset": offset + 1, "timeout": 25})
        except Exception as e:
            log(f"getUpdates failed: {e}")
            time.sleep(5)
            continue
        if not resp.get("ok"):
            time.sleep(5)
            continue
        changed = False
        for upd in resp.get("result", []):
            offset = upd["update_id"]
            msg = upd.get("message", {})
            chat_id = str(msg.get("chat", {}).get("id", ""))
            text = msg.get("text", "")
            if chat_id != TG_CHAT or not text:
                continue
            try:
                if tg_handle(text):
                    changed = True
            except Exception as e:
                log(f"telegram handle error: {e}")
        db.meta_set("tg_offset", offset)
        if changed:
            push_state(origin="telegram")


def start_telegram():
    if not (TG_TOKEN and TG_CHAT):
        log("telegram disabled (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set)")
        return
    if ANTHROPIC_API_KEY:
        masked = ANTHROPIC_API_KEY[:10] + "…" + ANTHROPIC_API_KEY[-4:] if len(ANTHROPIC_API_KEY) > 14 else "(short/invalid-looking)"
        log(f"claude natural-language parsing: ENABLED (model={CLAUDE_MODEL}, key={masked})")
    else:
        log("claude natural-language parsing: disabled (ANTHROPIC_API_KEY not set) — using rigid #Category !Priority parser")
    t = threading.Thread(target=telegram_loop, name="telegram", daemon=True)
    t.start()


# ── main ──────────────────────────────────────────────────────────────────

def main():
    db.init()
    start_telegram()
    httpd = ThreadingHTTPServer((BIND, PORT), Handler)
    httpd.daemon_threads = True
    log(f"serving {WEBROOT} on {BIND}:{PORT} | db {db.DB_PATH}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
