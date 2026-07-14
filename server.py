#!/usr/bin/env python3
"""
server.py — web board + Telegram poller for the Personal Project Tracker.

One process, standard library only:
  - serves index.html and a JSON API backed by SQLite (db.py, the source of truth)
  - GET  /api/state   -> {categories, projects, tasks, rev}
  - POST /api/op      -> mutate (addProject/updateProject/deleteProject/
                          addTask/updateTask/deleteTask/addCategory) {..., originId}
  - GET  /api/events  -> Server-Sent Events; pushes full state on every change
  - background thread polls Telegram and writes the same DB (see telegram.py-style
    logic below); its changes broadcast to the board live too.

Config via env (see config.example.env):
  WEBROOT (.)  PORT (8182)  BIND (0.0.0.0)
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   (optional — omit to disable Telegram)
DB path: $TRACKER_DB or $STATE_DIRECTORY/tracker.db
"""

import json
import mimetypes
import os
import posixpath
import queue
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import db

WEBROOT = os.path.abspath(os.environ.get("WEBROOT", "."))
PORT = int(os.environ.get("PORT", "8182"))
BIND = os.environ.get("BIND", "0.0.0.0")

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

_subscribers = set()  # set[queue.Queue]


def log(msg):
    sys.stderr.write(f"[tracker] {msg}\n")
    sys.stderr.flush()


# ── broadcast ─────────────────────────────────────────────────────────────

def push_state(origin=None):
    state = db.get_state()
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
        db.add_project(op.get("category", "Other"), op["name"].strip(),
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
        if not (op.get("project_id") and (op.get("name") or "").strip()):
            return False
        db.add_task(op["project_id"], op["name"].strip(),
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
        db.add_category(op["name"].strip())
    elif kind == "renameCategory":
        if not (op.get("old") and (op.get("new") or "").strip()):
            return False
        db.rename_category(op["old"], op["new"].strip())
    elif kind == "deleteCategory":
        if not op.get("name"):
            return False
        db.delete_category(op["name"])
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

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/state":
            self._json(db.get_state())
            return
        if path == "/api/events":
            self._sse()
            return
        self._static(path)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path != "/api/op":
            self.send_error(404)
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
            state = db.get_state()
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
DEFAULT_CATEGORY = "Other"
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
        if tok.startswith("#"):
            key = tok[1:].lower().replace("-", " ").replace("_", " ")
            if key in aliases:
                category = aliases[key]; i += 1; continue
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
    tg_reply(
        "Add a project:\n"
        "#TAK-CAP !High Radio config tool | Needs the new repeater list\n\n"
        f"Categories:\n{cats}\n\n"
        "Priorities: !Critical !High !Medium !Low (default Medium)\n\n"
        "Commands:\n"
        "/status - active projects\n"
        "/find <keyword> - search\n"
        "/task <ProjectID> <task text> - add a task to a project\n"
        "/done <ProjectID> - mark a project Complete\n"
        "/categories - list categories")


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
    db.add_task(p["id"], text)
    tg_reply(f'➕ Added task to {p["id"]}: "{text}"')


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
    parsed = parse_task_message(text)
    if not parsed:
        tg_reply("Couldn't parse that. Send /help for the format."); return False
    category, priority, title, desc = parsed
    pid = db.add_project(category, title, description=desc, priority=priority)
    tg_reply(f'✅ Added {pid} to {category}: "{title}" ({priority}).')
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
