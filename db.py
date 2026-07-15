"""
db.py — SQLite source of truth for the Personal Project Tracker.

Hierarchy:  Category -> Project -> (Tasks & Sub-Projects -> Tasks)
  - categories: a small ordered list (AI_Agents, Plugins, ...)
  - projects:   belong to a category; parent_id set => it's a sub-project
  - tasks:      belong to a project (top-level or sub), 3-state (todo/doing/done)

Both the web UI and the Telegram poller call into this module; every mutation
bumps a monotonic `rev` so the web layer can broadcast "something changed".
Standard library only.
"""

import json
import os
import sqlite3
import threading
import time
from datetime import datetime

DB_PATH = os.environ.get("TRACKER_DB") or os.path.join(
    os.environ.get("STATE_DIRECTORY") or os.environ.get("STATE_DIR") or ".", "tracker.db"
)

# Clean slate: no categories are seeded. You create your own (and sub-categories)
# from the web UI or by using #Category in Telegram (auto-created on first use).
DEFAULT_CATEGORIES = []

PROJECT_STATUSES = ["Planning", "Active", "Blocked", "Review", "Complete", "Backlog"]
PRIORITIES = ["Critical", "High", "Medium", "Low"]
TASK_STATES = ["todo", "doing", "done"]

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS categories (
    name TEXT PRIMARY KEY,
    parent TEXT,
    sort INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    category    TEXT NOT NULL,
    parent_id   TEXT,
    name        TEXT NOT NULL,
    description TEXT DEFAULT '',
    status      TEXT DEFAULT 'Planning',
    priority    TEXT DEFAULT 'Medium',
    due_date    TEXT,
    sort        INTEGER DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL,
    name        TEXT NOT NULL,
    state       TEXT DEFAULT 'todo',
    due_date    TEXT,
    sort        INTEGER DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_projects_cat ON projects(category);
CREATE INDEX IF NOT EXISTS idx_projects_parent ON projects(parent_id);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
"""


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c


def _now():
    return datetime.now().isoformat(timespec="seconds")


def init():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with _lock, _conn() as c:
        c.executescript(SCHEMA)
        # migration: cross-cutting tags on projects (added after initial schema)
        cols = [r["name"] for r in c.execute("PRAGMA table_info(projects)")]
        if "tags" not in cols:
            c.execute("ALTER TABLE projects ADD COLUMN tags TEXT DEFAULT '[]'")
        # migration: sub-categories (parent column on categories)
        ccols = [r["name"] for r in c.execute("PRAGMA table_info(categories)")]
        if "parent" not in ccols:
            c.execute("ALTER TABLE categories ADD COLUMN parent TEXT")
        if c.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0:
            c.executemany("INSERT INTO categories(name,sort) VALUES(?,?)",
                          [(n, i) for i, n in enumerate(DEFAULT_CATEGORIES)])
        if c.execute("SELECT v FROM meta WHERE k='rev'").fetchone() is None:
            c.execute("INSERT INTO meta(k,v) VALUES('rev','0')")


# ── revision / meta ───────────────────────────────────────────────────────

def get_rev():
    with _lock, _conn() as c:
        r = c.execute("SELECT v FROM meta WHERE k='rev'").fetchone()
        return int(r["v"]) if r else 0


def _bump(c):
    c.execute("UPDATE meta SET v = CAST(CAST(v AS INTEGER)+1 AS TEXT) WHERE k='rev'")


def meta_get(k, default=None):
    with _lock, _conn() as c:
        r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r["v"] if r else default


def meta_set(k, v):
    with _lock, _conn() as c:
        c.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


# ── ids ───────────────────────────────────────────────────────────────────

def _cat_prefix(cat):
    words = [w for w in (cat or "").replace("-", "_").split("_") if w]
    p = "".join(w[0] for w in words)[:3].upper()
    return p or "P"


def next_project_id(category):
    prefix = _cat_prefix(category)
    with _lock, _conn() as c:
        rows = c.execute("SELECT id FROM projects WHERE id LIKE ?", (prefix + "-%",)).fetchall()
    n = 0
    for r in rows:
        try:
            n = max(n, int(r["id"].rsplit("-", 1)[-1]))
        except ValueError:
            pass
    return f"{prefix}-{n + 1:03d}"


def _task_id():
    return "t" + format(int(time.time() * 1000) % 0x7fffffff, "x") + os.urandom(2).hex()


# ── reads ─────────────────────────────────────────────────────────────────

def get_state():
    """Flat lists the client assembles into the tree, plus rev."""
    with _lock, _conn() as c:
        cats = [dict(r) for r in c.execute("SELECT * FROM categories ORDER BY sort, name")]
        projects = [dict(r) for r in c.execute("SELECT * FROM projects ORDER BY sort, created_at")]
        tasks = [dict(r) for r in c.execute("SELECT * FROM tasks ORDER BY sort, created_at")]
        rev = int(c.execute("SELECT v FROM meta WHERE k='rev'").fetchone()["v"])
    for p in projects:  # tags stored as JSON text -> return as a list
        try:
            p["tags"] = json.loads(p.get("tags") or "[]")
        except Exception:
            p["tags"] = []
    return {"categories": cats, "projects": projects, "tasks": tasks, "rev": rev}


def get_project(pid):
    with _lock, _conn() as c:
        r = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        return dict(r) if r else None


def find_projects(keyword, limit=10):
    like = f"%{keyword}%"
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT * FROM projects WHERE name LIKE ? OR description LIKE ? OR id LIKE ? "
            "ORDER BY updated_at DESC LIMIT ?", (like, like, like, limit)).fetchall()
    return [dict(r) for r in rows]


def summary():
    with _lock, _conn() as c:
        top = [dict(r) for r in c.execute("SELECT * FROM projects WHERE parent_id IS NULL ORDER BY category, sort")]
    return top


# ── writes (each bumps rev) ───────────────────────────────────────────────

def add_category(name, parent=None):
    name = (name or "").strip()
    if not name:
        return
    parent = (parent or "").strip() or None
    with _lock, _conn() as c:
        mx = c.execute("SELECT COALESCE(MAX(sort),-1) FROM categories").fetchone()[0]
        c.execute("INSERT OR IGNORE INTO categories(name,parent,sort) VALUES(?,?,?)", (name, parent, mx + 1))
        _bump(c)


def rename_category(old, new):
    new = (new or "").strip()
    if not new or new == old:
        return
    with _lock, _conn() as c:
        exists = c.execute("SELECT 1 FROM categories WHERE name=?", (new,)).fetchone()
        if exists:
            c.execute("DELETE FROM categories WHERE name=?", (old,))   # merge into existing
        else:
            c.execute("UPDATE categories SET name=? WHERE name=?", (new, old))
        c.execute("UPDATE projects SET category=? WHERE category=?", (new, old))
        c.execute("UPDATE categories SET parent=? WHERE parent=?", (new, old))  # keep sub-categories attached
        _bump(c)


def delete_category(name):
    """Delete a category and all its sub-categories; their projects move up to the
    deleted category's parent (or become uncategorized if it was top-level).
    A clean slate (zero categories) is allowed."""
    with _lock, _conn() as c:
        row = c.execute("SELECT parent FROM categories WHERE name=?", (name,)).fetchone()
        if not row:
            return None
        target = row["parent"] or ""            # "" = uncategorized
        subtree, stack = [name], [name]
        while stack:
            cur = stack.pop()
            for r in c.execute("SELECT name FROM categories WHERE parent=?", (cur,)):
                subtree.append(r["name"]); stack.append(r["name"])
        qm = ",".join("?" * len(subtree))
        c.execute(f"UPDATE projects SET category=? WHERE category IN ({qm})", (target, *subtree))
        c.execute(f"DELETE FROM categories WHERE name IN ({qm})", subtree)
        _bump(c)
    return target


def _category_is_descendant(c, candidate, of_name):
    """True if the category `candidate` is somewhere under `of_name` in the category tree."""
    seen = set()
    cur = candidate
    while cur and cur not in seen:
        seen.add(cur)
        row = c.execute("SELECT parent FROM categories WHERE name=?", (cur,)).fetchone()
        if not row or not row["parent"]:
            return False
        if row["parent"] == of_name:
            return True
        cur = row["parent"]
    return False


def update_category(name, patch):
    """Drag-and-drop support: reparent (patch.parent) and/or reorder (patch.sort) a category.
    Rejects a parent change that would create a cycle or self-parent."""
    fields = {}
    if "parent" in patch:
        fields["parent"] = (patch["parent"] or "").strip() or None
    if "sort" in patch:
        fields["sort"] = patch["sort"]
    if not fields:
        return
    with _lock, _conn() as c:
        if not c.execute("SELECT 1 FROM categories WHERE name=?", (name,)).fetchone():
            return
        if "parent" in fields and fields["parent"]:
            new_parent = fields["parent"]
            if new_parent == name or _category_is_descendant(c, new_parent, name):
                return  # would create a cycle — reject
        sets = ", ".join(f"{k}=?" for k in fields)
        c.execute(f"UPDATE categories SET {sets} WHERE name=?", (*fields.values(), name))
        _bump(c)


def reorder_categories(names):
    """Bulk-set sort=index for exactly the given category names (one sibling group)."""
    with _lock, _conn() as c:
        c.executemany("UPDATE categories SET sort=? WHERE name=?",
                      [(i, n) for i, n in enumerate(names)])
        _bump(c)


def add_project(category, name, description="", status="Planning", priority="Medium",
                due_date=None, parent_id=None, tags=None):
    cat = (category or "").strip()               # "" = uncategorized
    if cat and cat not in _category_names():
        add_category(cat)                        # auto-create unknown category (e.g. from Telegram)
    pid = next_project_id(cat)
    now = _now()
    tags_json = json.dumps(tags or [])
    with _lock, _conn() as c:
        mx = c.execute("SELECT COALESCE(MAX(sort),-1) FROM projects WHERE category=?", (cat,)).fetchone()[0]
        c.execute(
            "INSERT INTO projects(id,category,parent_id,name,description,status,priority,due_date,tags,sort,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, cat, parent_id, name, description, status, priority, due_date, tags_json, mx + 1, now, now))
        _bump(c)
    return pid


def _project_is_descendant(c, candidate_id, of_id):
    """True if candidate_id is somewhere under of_id in the project tree."""
    seen = set()
    cur = candidate_id
    while cur and cur not in seen:
        seen.add(cur)
        row = c.execute("SELECT parent_id FROM projects WHERE id=?", (cur,)).fetchone()
        if not row or not row["parent_id"]:
            return False
        if row["parent_id"] == of_id:
            return True
        cur = row["parent_id"]
    return False


def update_project(pid, patch):
    fields = {k: v for k, v in patch.items()
              if k in ("category", "parent_id", "name", "description", "status", "priority", "due_date", "sort", "tags")}
    if not fields:
        return
    if "tags" in fields and isinstance(fields["tags"], list):
        fields["tags"] = json.dumps(fields["tags"])
    fields["updated_at"] = _now()
    with _lock, _conn() as c:
        if "parent_id" in fields and fields["parent_id"]:
            new_parent = fields["parent_id"]
            if new_parent == pid or _project_is_descendant(c, new_parent, pid):
                return  # would create a cycle — reject the whole update
        sets = ", ".join(f"{k}=?" for k in fields)
        c.execute(f"UPDATE projects SET {sets} WHERE id=?", (*fields.values(), pid))
        _bump(c)


def reorder_projects(ids):
    """Bulk-set sort=index for exactly the given project ids (one sibling group)."""
    with _lock, _conn() as c:
        c.executemany("UPDATE projects SET sort=?, updated_at=? WHERE id=?",
                      [(i, _now(), pid) for i, pid in enumerate(ids)])
        _bump(c)


def delete_project(pid):
    """Delete a project, its sub-projects (recursively) and all their tasks."""
    with _lock, _conn() as c:
        ids = [pid]
        frontier = [pid]
        while frontier:
            cur = frontier.pop()
            kids = [r["id"] for r in c.execute("SELECT id FROM projects WHERE parent_id=?", (cur,))]
            ids.extend(kids)
            frontier.extend(kids)
        qmarks = ",".join("?" * len(ids))
        c.execute(f"DELETE FROM tasks WHERE project_id IN ({qmarks})", ids)
        c.execute(f"DELETE FROM projects WHERE id IN ({qmarks})", ids)
        _bump(c)


def add_task(project_id, name, state="todo", due_date=None):
    tid = _task_id()
    now = _now()
    with _lock, _conn() as c:
        mx = c.execute("SELECT COALESCE(MAX(sort),-1) FROM tasks WHERE project_id=?", (project_id,)).fetchone()[0]
        c.execute("INSERT INTO tasks(id,project_id,name,state,due_date,sort,created_at,updated_at)"
                  " VALUES(?,?,?,?,?,?,?,?)",
                  (tid, project_id, name, state, due_date, mx + 1, now, now))
        _bump(c)
    return tid


def update_task(tid, patch):
    fields = {k: v for k, v in patch.items() if k in ("name", "state", "due_date", "project_id", "sort")}
    if not fields:
        return
    fields["updated_at"] = _now()
    sets = ", ".join(f"{k}=?" for k in fields)
    with _lock, _conn() as c:
        c.execute(f"UPDATE tasks SET {sets} WHERE id=?", (*fields.values(), tid))
        _bump(c)


def delete_task(tid):
    with _lock, _conn() as c:
        c.execute("DELETE FROM tasks WHERE id=?", (tid,))
        _bump(c)


def reorder_tasks(ids):
    """Bulk-set sort=index for exactly the given task ids (one project's task list)."""
    with _lock, _conn() as c:
        c.executemany("UPDATE tasks SET sort=?, updated_at=? WHERE id=?",
                      [(i, _now(), tid) for i, tid in enumerate(ids)])
        _bump(c)


def _category_names():
    with _lock, _conn() as c:
        return [r["name"] for r in c.execute("SELECT name FROM categories")]


def category_names():
    return _category_names()


if __name__ == "__main__":
    init()
    print(f"DB ready at {DB_PATH}")
    print("categories:", category_names())
    print("rev:", get_rev())
