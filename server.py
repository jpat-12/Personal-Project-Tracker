#!/usr/bin/env python3
"""
server.py — static file server + tiny cross-device sync backend for the
Project Statusboard. Standard library only (no pip installs).

Serves index.html and exposes:
    GET  /api/state    -> current shared board  {done, statusOverride, rev}
    POST /api/op       -> apply one change       {op: setDone|setStatus|reset, ...}
    GET  /api/events   -> Server-Sent Events stream; pushes the full board to
                          every connected device whenever anything changes.

State persists to $STATE_DIRECTORY/state.json (systemd sets STATE_DIRECTORY;
falls back to the current dir when run by hand). Config via env:
    WEBROOT (default ./ )   PORT (8182)   BIND (0.0.0.0)
"""

import json
import mimetypes
import os
import posixpath
import queue
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WEBROOT = os.path.abspath(os.environ.get("WEBROOT", "."))
PORT = int(os.environ.get("PORT", "8182"))
BIND = os.environ.get("BIND", "0.0.0.0")
STATE_DIR = os.environ.get("STATE_DIRECTORY") or os.environ.get("STATE_DIR") or "."
STATE_FILE = os.path.join(STATE_DIR, "state.json")

VALID_STATUS = ("active", "blocked", "shipped", "backlog")

_lock = threading.Lock()
_subscribers = set()  # set[queue.Queue]
state = {"done": {}, "statusOverride": {}, "rev": 0}


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        state["done"] = data.get("done", {}) or {}
        state["statusOverride"] = data.get("statusOverride", {}) or {}
        state["rev"] = int(data.get("rev", 0) or 0)
    except FileNotFoundError:
        pass
    except Exception as e:  # corrupt file etc. — start clean rather than crash
        sys.stderr.write(f"[statusboard] state load error: {e}\n")


def save_state():
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)  # atomic
    except Exception as e:
        sys.stderr.write(f"[statusboard] state save error: {e}\n")


def snapshot():
    return {"done": state["done"], "statusOverride": state["statusOverride"], "rev": state["rev"]}


def broadcast():
    payload = "data: " + json.dumps(snapshot()) + "\n\n"
    for q in list(_subscribers):
        try:
            q.put_nowait(payload)
        except Exception:
            _subscribers.discard(q)


def apply_op(op):
    """Mutate authoritative state. Returns True if it changed. Caller holds _lock."""
    t = op.get("op")
    if t == "setDone":
        key = op.get("key")
        if not key:
            return False
        if op.get("value"):
            state["done"][key] = True
        else:
            state["done"].pop(key, None)
    elif t == "setStatus":
        pid, st = op.get("id"), op.get("status")
        if not pid or st not in VALID_STATUS:
            return False
        state["statusOverride"][pid] = st
    elif t == "reset":
        state["done"] = {}
        state["statusOverride"] = {}
    else:
        return False
    state["rev"] += 1
    return True


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
            with _lock:
                self._json(snapshot())
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
        with _lock:
            ok = apply_op(op)
            if ok:
                save_state()
                broadcast()
            rev = state["rev"]
        self._json({"ok": ok, "rev": rev})

    def _sse(self):
        self.close_connection = True  # long-lived stream; don't try to keep-alive after
        q = queue.Queue(maxsize=128)
        _subscribers.add(q)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            with _lock:
                first = "data: " + json.dumps(snapshot()) + "\n\n"
            self.wfile.write(first.encode("utf-8"))
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=20)
                    self.wfile.write(msg.encode("utf-8"))
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")  # keep connection/proxies alive
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
        pass  # quiet; systemd/journald captures stderr for real errors


def main():
    load_state()
    httpd = ThreadingHTTPServer((BIND, PORT), Handler)
    httpd.daemon_threads = True
    sys.stderr.write(
        f"[statusboard] serving {WEBROOT} on {BIND}:{PORT} | state file: {STATE_FILE}\n"
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
