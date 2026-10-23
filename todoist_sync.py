"""Sync layer for Todoist Widgets (no Qt dependency, unit-testable).

Uses the official Todoist API v1 (https://api.todoist.com/api/v1):
  * POST /sync            incremental sync (sync_token) + batched, idempotent commands
  * POST /tasks/quick     Quick Add (natural language: "tomorrow p1 #Project @label")
  * GET  /tasks/completed/by_completion_date   recently completed tasks (optional)

Local edits are applied optimistically and queued; the queue survives restarts
and is sent with the next successful sync. The API token lives only in Windows
Credential Manager (keyring) and is never written to files or logs.
"""
from __future__ import annotations

import copy
import datetime as _dt
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

APP_NAME = "TodoistWidgets"
KEYRING_SERVICE = "TodoistWidgets"
KEYRING_TOKEN_KEY = "api-token"
API_BASE = "https://api.todoist.com/api/v1"
RESOURCE_TYPES = ["projects", "sections", "items", "labels", "user"]
TERMINAL_HTTP = {400, 403, 404, 409, 410, 422}
MAX_RETRIES = 5

log = logging.getLogger("todoistwidgets")


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------
def data_dir() -> Path:
    """%APPDATA%\\TodoistWidgets on Windows (override with TDW_DATA_DIR)."""
    override = os.environ.get("TDW_DATA_DIR")
    if override:
        p = Path(override)
    elif os.name == "nt":
        p = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
    else:
        p = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def read_json(path: Path, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        log.warning("Could not read %s (%s); using defaults", path.name, exc)
        return default


def atomic_write_json(path: Path, data: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, default=str)
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------
class CredentialStore:
    def get(self) -> str | None:
        import keyring

        return keyring.get_password(KEYRING_SERVICE, KEYRING_TOKEN_KEY) or None

    def set(self, token: str) -> None:
        import keyring

        keyring.set_password(KEYRING_SERVICE, KEYRING_TOKEN_KEY, token)

    def clear(self) -> None:
        import keyring

        try:
            keyring.delete_password(KEYRING_SERVICE, KEYRING_TOKEN_KEY)
        except Exception:  # noqa: BLE001
            pass


class MemoryCredentialStore(CredentialStore):
    def __init__(self, token: str | None = None):
        self._t = token

    def get(self):
        return self._t

    def set(self, token):
        self._t = token

    def clear(self):
        self._t = None


def clean_token(value: str) -> str:
    value = value.strip().strip('"').strip("'").strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    return value


def looks_like_token(value: str) -> bool:
    v = clean_token(value)
    return len(v) >= 30 and all(c.isalnum() for c in v)


# --------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class AuthError(ApiError):
    pass


class TodoistClient:
    def __init__(self, token: str, session=None, base: str = API_BASE, timeout: float = 25):
        if session is None:
            import requests

            session = requests.Session()
        self._token = token
        self.session = session
        self.base = base
        self.timeout = timeout

    def __repr__(self) -> str:  # never show the token
        return "TodoistClient(<token hidden>)"

    def _req(self, method: str, path: str, **kw):
        headers = {"Authorization": f"Bearer {self._token}"}
        r = self.session.request(method, self.base + path, headers=headers, timeout=self.timeout, **kw)
        code = r.status_code
        if code == 401:
            raise AuthError("Todoist rejected the API token (HTTP 401)", 401)
        if code == 429:
            raise ApiError("Todoist rate limit reached (HTTP 429), will retry", 429)
        if code >= 400:
            raise ApiError(f"Todoist returned HTTP {code}", code)
        if not r.content:
            return None
        return r.json()

    def sync(self, sync_token: str, commands: list[dict] | None = None,
             resource_types: list[str] | None = None) -> dict:
        data = {"sync_token": sync_token or "*",
                "resource_types": json.dumps(resource_types or RESOURCE_TYPES)}
        if commands:
            data["commands"] = json.dumps(commands)
        return self._req("POST", "/sync", data=data)

    def whoami(self) -> dict:
        res = self.sync("*", resource_types=["user"])
        return res.get("user") or {}

    def quick_add(self, text: str) -> dict:
        return self._req("POST", "/tasks/quick", json={"text": text})

    def completed(self, project_id: str, days: int = 14, limit: int = 200) -> list[dict]:
        until = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(minutes=5)
        since = until - _dt.timedelta(days=days)
        out, cursor = [], None
        while len(out) < limit:
            params = {"since": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "until": until.strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "project_id": project_id, "limit": 50}
            if cursor:
                params["cursor"] = cursor
            res = self._req("GET", "/tasks/completed/by_completion_date", params=params) or {}
            out.extend(res.get("items", []))
            cursor = res.get("next_cursor")
            if not cursor:
                break
        return out


def _friendly_error(exc: BaseException) -> str:
    name = type(exc).__name__
    if name in ("ConnectionError", "Timeout", "ConnectTimeout", "ReadTimeout", "gaierror"):
        return "offline (no connection to Todoist)"
    if isinstance(exc, AuthError):
        return "Todoist rejected the API token - paste a new one in Settings"
    if isinstance(exc, ApiError):
        return str(exc)
    msg = str(exc).splitlines()[0][:160] if str(exc) else ""
    return f"{name}: {msg}" if msg else name


# --------------------------------------------------------------------------
# View data handed to the UI
# --------------------------------------------------------------------------
@dataclass
class RowData:
    kind: str  # "task" | "header"
    id: str
    content: str
    checked: bool = False
    depth: int = 0
    priority: int = 1  # API value: 4 = p1 (urgent) ... 1 = p4 (none)
    due_text: str = ""
    due_state: str = ""  # overdue | today | tomorrow | future | ""
    recurring: bool = False
    pending: bool = False


@dataclass
class ViewData:
    key: str
    kind: str  # project | section | today | overdue | label
    title: str
    color: str  # Todoist colour name
    project_id: str | None = None
    section_id: str | None = None
    label: str | None = None
    rows: list[RowData] = field(default_factory=list)

    @property
    def open_count(self) -> int:
        return sum(1 for r in self.rows if r.kind == "task" and not r.checked)


def parse_due(due: dict | None, today: _dt.date) -> tuple[_dt.date | None, str, str]:
    """Return (date, label, state) for a Todoist due object."""
    if not due or not due.get("date"):
        return None, "", ""
    ds = due["date"]
    t = None
    try:
        if len(ds) == 10:
            d = _dt.date.fromisoformat(ds)
        else:
            dt = _dt.datetime.fromisoformat(ds.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                dt = dt.astimezone()
            d, t = dt.date(), dt.time()
    except ValueError:
        return None, ds, ""
    delta = (d - today).days
    if delta < 0:
        state = "overdue"
        label = "Yesterday" if delta == -1 else f"{d.day} {d:%b}" + (f" {d.year}" if d.year != today.year else "")
    elif delta == 0:
        state, label = "today", "Today"
    elif delta == 1:
        state, label = "tomorrow", "Tomorrow"
    elif delta < 7:
        state, label = "future", f"{d:%A}"
    else:
        state = "future"
        label = f"{d.day} {d:%b}" + (f" {d.year}" if d.year != today.year else "")
    if t is not None:
        label += f" {t:%H:%M}"
    return d, label, state


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------
class TodoistEngine:
    def __init__(self, creds: CredentialStore | None = None, directory: Path | str | None = None,
                 client_factory: Callable[[str], TodoistClient] | None = None,
                 offline_only: bool = False, today_fn: Callable[[], _dt.date] = _dt.date.today):
        self.creds = creds if creds is not None else CredentialStore()
        self.dir = Path(directory) if directory else data_dir()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cache_path = self.dir / "state.json"
        self.client_factory = client_factory or (lambda token: TodoistClient(token))
        self.offline_only = offline_only
        self.today_fn = today_fn
        self.client: TodoistClient | None = None
        self.status = "signed_out"  # signed_out | ok | paused
        self.last_error: str | None = None
        self.last_sync: _dt.datetime | None = None
        self.user: dict = {}
        # data
        self.sync_token = "*"
        self.projects: dict[str, dict] = {}
        self.sections: dict[str, dict] = {}
        self.items: dict[str, dict] = {}
        self.labels: list[str] = []
        self.pending_items: dict[str, dict] = {}  # tmp-id -> optimistic quick-add item
        self.completed: dict[str, list[dict]] = {}  # project id -> recently completed
        self.show_completed: set[str] = set()  # project ids wanting completed tasks
        self.queue: list[dict] = []  # sync commands not yet confirmed
        self.quick_queue: list[dict] = []  # quick adds not yet sent
        self.full_syncs = 0
        self._ops: list[dict] = []
        self._ops_lock = threading.Lock()
        self._lock = threading.RLock()

    # -- cache ------------------------------------------------------------
    def load_cache(self) -> bool:
        data = read_json(self.cache_path, None)
        if not data:
            return False
        with self._lock:
            self.sync_token = data.get("sync_token", "*")
            self.projects = data.get("projects", {})
            self.sections = data.get("sections", {})
            self.items = data.get("items", {})
            self.labels = data.get("labels", [])
            self.pending_items = data.get("pending_items", {})
            self.completed = data.get("completed", {})
            self.queue = data.get("queue", [])
            self.quick_queue = data.get("quick_queue", [])
            self.user = data.get("user", {})
        log.info("Loaded cache: %d projects, %d tasks, %d queued edits",
                 len(self.projects), len(self.items), len(self.queue) + len(self.quick_queue))
        return True

    def save_cache(self) -> None:
        try:
            with self._lock:
                state = {"sync_token": self.sync_token, "projects": self.projects,
                         "sections": self.sections, "items": self.items, "labels": self.labels,
                         "pending_items": self.pending_items, "completed": self.completed,
                         "queue": self.queue, "quick_queue": self.quick_queue,
                         "user": {k: self.user.get(k) for k in ("id", "full_name", "email",
                                                                 "inbox_project_id") if k in self.user},
                         "saved_at": _dt.datetime.now().isoformat(timespec="seconds")}
                state = copy.deepcopy(state)
            atomic_write_json(self.cache_path, state)
        except Exception:  # noqa: BLE001
            log.exception("Could not write offline cache")

    def _reset_data(self) -> None:
        self.sync_token = "*"
        self.projects, self.sections, self.items = {}, {}, {}
        self.labels, self.pending_items, self.completed = [], {}, {}
        self.queue, self.quick_queue = [], []

    # -- auth -------------------------------------------------------------
    def start(self) -> None:
        self.load_cache()
        if self.offline_only:
            self.status = "ok"
            return
        token = self.creds.get()
        if not token:
            self.status = "signed_out"
            return
        self.client = self.client_factory(token)

    def set_token(self, token: str) -> dict:
        """Validate a pasted API token against Todoist, then store it. Raises on failure."""
        token = clean_token(token)
        client = self.client_factory(token)
        user = client.whoami()
        if not user.get("id"):
            raise ApiError("Todoist did not return a user for this token")
        with self._lock:
            if self.user.get("id") and str(self.user["id"]) != str(user["id"]):
                log.info("Different Todoist account; dropping cached data of the old account")
                self._reset_data()
            self.user = user
            self.client = client
        self.creds.set(token)
        self.save_cache()
        log.info("Signed in to Todoist as %s", user.get("full_name") or user.get("id"))
        return user

    def sign_out(self) -> None:
        self.creds.clear()
        self.client = None
        self.status = "signed_out"
        log.info("Signed out (token removed from Credential Manager)")

    def _pause(self, exc: BaseException) -> None:
        self.status = "paused"
        self.last_error = _friendly_error(exc)
        log.warning("Sync paused: %s", self.last_error)

    # -- local edits ------------------------------------------------------
    def submit(self, op: dict) -> None:
        with self._ops_lock:
            self._ops.append(op)

    def pending_count(self) -> int:
        with self._lock:
            return len(self.queue) + len(self.quick_queue)

    def _item(self, item_id: str) -> dict | None:
        return self.items.get(item_id) or self.pending_items.get(item_id)

    def _cmd(self, ctype: str, args: dict) -> None:
        if ctype == "item_update":
            # coalesce consecutive edits of the same task
            for c in reversed(self.queue):
                if c["type"] == "item_update" and c["args"]["id"] == args["id"]:
                    c["args"].update(args)
                    return
                if c["args"].get("id") == args["id"]:
                    break
        self.queue.append({"type": ctype, "uuid": str(uuid.uuid4()), "args": args, "tries": 0})

    def apply_pending(self) -> list[dict]:
        with self._ops_lock:
            ops, self._ops = self._ops, []
        if not ops:
            return []
        events = []
        with self._lock:
            for op in ops:
                try:
                    ev = self._apply(op)
                    if ev:
                        events.append(ev)
                except Exception:  # noqa: BLE001
                    log.exception("Could not apply local edit %s", op.get("op"))
        self.save_cache()
        return events

    def _apply(self, op: dict) -> dict | None:
        kind = op["op"]
        if kind == "_noop":
            return {"event": "refresh"}
        if kind == "quick_add":
            text = op["text"].strip()
            if not text:
                return None
            tmp = f"tmp-{uuid.uuid4()}"
            self.pending_items[tmp] = {
                "id": tmp, "content": text, "project_id": op.get("project_id") or
                self.user.get("inbox_project_id"), "section_id": op.get("section_id"),
                "parent_id": None, "child_order": 10**9, "priority": 1, "due": None,
                "checked": False, "is_deleted": False, "labels": [], "_pending": True}
            self.quick_queue.append({"uuid": str(uuid.uuid4()), "tmp_id": tmp, "text": text,
                                     "project_id": op.get("project_id"),
                                     "section_id": op.get("section_id"), "tries": 0})
            return {"event": "added", "id": tmp}
        item = self._item(op["id"])
        if item is None and kind == "uncomplete":
            # a task from the "recently completed" list: reopen it on the server
            for pid, lst in self.completed.items():
                for done in lst:
                    if (done.get("id") or done.get("task_id")) == op["id"]:
                        lst.remove(done)
                        restored = dict(done, id=op["id"], checked=False, is_deleted=False, _pending=True)
                        self.items[op["id"]] = restored
                        break
            self._cmd("item_uncomplete", {"id": op["id"]})
            return {"event": "edited", "id": op["id"]}
        if item is None:
            log.warning("Edit for unknown task ignored")
            return None
        if kind == "complete":
            if (item.get("due") or {}).get("is_recurring"):
                item["_pending"] = True  # stays visible; Todoist moves it to the next date
            else:
                item["checked"] = True
                item["completed_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
            self._cmd("item_close", {"id": op["id"]})
        elif kind == "uncomplete":
            item["checked"] = False
            item["completed_at"] = None
            self._cmd("item_uncomplete", {"id": op["id"]})
        elif kind == "update":
            content = op["content"].strip()
            if not content or content == item.get("content"):
                return None
            item["content"] = content
            self._cmd("item_update", {"id": op["id"], "content": content})
        elif kind == "delete":
            item["is_deleted"] = True
            self._cmd("item_delete", {"id": op["id"]})
            # children go too
            for child in list(self.items.values()):
                if child.get("parent_id") == op["id"]:
                    child["is_deleted"] = True
        else:
            log.warning("Unknown op %s", kind)
            return None
        return {"event": "edited", "id": op["id"]}

    # -- sync -------------------------------------------------------------
    def set_show_completed(self, project_id: str, on: bool) -> None:
        with self._lock:
            (self.show_completed.add if on else self.show_completed.discard)(project_id)

    def sync(self) -> dict:
        """Returns {"ok": bool, "error": str|None, "conflicts": [...]}."""
        if self.offline_only:
            self.status, self.last_sync = "ok", _dt.datetime.now()
            return {"ok": True, "error": None, "conflicts": []}
        if self.client is None:
            token = self.creds.get()
            if not token:
                self.status = "signed_out"
                return {"ok": False, "error": "not signed in", "conflicts": []}
            self.client = self.client_factory(token)
        try:
            self._send_quick_adds()
            conflicts = self._sync_once()
            self._fetch_completed()
        except Exception as exc:  # noqa: BLE001
            self._pause(exc)
            self.save_cache()
            return {"ok": False, "error": self.last_error, "conflicts": []}
        self.status, self.last_error = "ok", None
        self.last_sync = _dt.datetime.now()
        self.save_cache()
        return {"ok": True, "error": None, "conflicts": conflicts}

    def _send_quick_adds(self) -> None:
        while True:
            with self._lock:
                if not self.quick_queue:
                    return
                q = dict(self.quick_queue[0])
            try:
                task = self.client.quick_add(q["text"])
            except ApiError as exc:
                if exc.status in TERMINAL_HTTP:
                    log.error("Quick add rejected by Todoist (HTTP %s); dropped: %r", exc.status, q["text"][:60])
                    with self._lock:
                        self.quick_queue.pop(0)
                        self.pending_items.pop(q["tmp_id"], None)
                    continue
                raise
            with self._lock:
                self.quick_queue.pop(0)
                self.pending_items.pop(q["tmp_id"], None)
                if task and task.get("id"):
                    real = task["id"]
                    self.items[real] = task
                    for c in self.queue:  # edits made to the optimistic copy
                        if c["args"].get("id") == q["tmp_id"]:
                            c["args"]["id"] = real
                    # Added from a project/section widget without an explicit #project:
                    # move it there (Quick Add itself defaults to the Inbox).
                    explicit = "#" in q["text"]
                    if not explicit:
                        if q.get("section_id") and task.get("section_id") != q["section_id"]:
                            self._cmd("item_move", {"id": real, "section_id": q["section_id"]})
                            task["section_id"] = q["section_id"]
                            task["project_id"] = q.get("project_id") or task.get("project_id")
                        elif q.get("project_id") and task.get("project_id") != q["project_id"]:
                            self._cmd("item_move", {"id": real, "project_id": q["project_id"]})
                            task["project_id"] = q["project_id"]
                    log.info("Quick add OK")

    def _sync_once(self) -> list[str]:
        with self._lock:
            cmds = [{k: v for k, v in c.items() if k != "tries"} for c in self.queue]
            pushed_content = {c["args"]["id"]: c["args"]["content"] for c in cmds
                              if c["type"] == "item_update" and "content" in c["args"]}
            token = self.sync_token
        try:
            resp = self.client.sync(token, cmds)
        except ApiError as exc:
            if exc.status == 400 and token != "*" and not cmds:
                log.warning("Sync token rejected; doing a full sync")
                with self._lock:
                    self.sync_token = "*"
                resp = self.client.sync("*", cmds)
            else:
                raise
        statuses = resp.get("sync_status", {}) or {}
        with self._lock:
            sent = {c["uuid"] for c in cmds}
            keep = []
            for c in self.queue:
                if c["uuid"] not in sent:
                    keep.append(c)  # added while the request was running
                    continue
                st = statuses.get(c["uuid"])
                if st == "ok":
                    continue
                if isinstance(st, dict):
                    code = st.get("http_code")
                    if code in TERMINAL_HTTP or code is None:
                        log.error("Todoist rejected %s (%s); dropped", c["type"], st.get("error_tag") or st.get("error"))
                        continue
                c["tries"] = c.get("tries", 0) + 1
                if c["tries"] >= MAX_RETRIES:
                    log.error("Giving up on %s after %d tries", c["type"], c["tries"])
                    continue
                keep.append(c)
            self.queue = keep
            self._merge(resp)
            conflicts = []
            for tid, content in pushed_content.items():
                it = self.items.get(tid)
                if it and it.get("content") != content:
                    conflicts.append(tid)
                    log.warning("Conflict on task %s: changed here and elsewhere; last write wins, "
                                "Todoist's version is kept", tid)
        return conflicts

    def _merge(self, resp: dict) -> None:
        full = bool(resp.get("full_sync"))
        if full:
            self.full_syncs += 1
            self.projects, self.sections, self.items = {}, {}, {}
        for p in resp.get("projects", []) or []:
            if p.get("is_deleted") or p.get("is_archived"):
                self.projects.pop(p["id"], None)
            else:
                self.projects[p["id"]] = p
        for s in resp.get("sections", []) or []:
            if s.get("is_deleted") or s.get("is_archived"):
                self.sections.pop(s["id"], None)
            else:
                self.sections[s["id"]] = s
        for it in resp.get("items", []) or []:
            if it.get("is_deleted"):
                self.items.pop(it["id"], None)
            else:
                self.items[it["id"]] = it
        if "labels" in resp and resp["labels"] is not None:
            if full:
                self.labels = []
            names = set(self.labels)
            for lb in resp["labels"]:
                (names.discard if lb.get("is_deleted") else names.add)(lb.get("name"))
            self.labels = sorted(n for n in names if n)
        if resp.get("user"):
            self.user = resp["user"]
        if resp.get("sync_token"):
            self.sync_token = resp["sync_token"]
        # forget local copies of completed items the server no longer reports active
        for pid in list(self.completed):
            if pid not in self.projects:
                self.completed.pop(pid)

    def _fetch_completed(self) -> None:
        with self._lock:
            wanted = [p for p in self.show_completed if p in self.projects]
        for pid in wanted:
            items = self.client.completed(pid)
            with self._lock:
                self.completed[pid] = items

    # -- views ------------------------------------------------------------
    def build_views(self, settings: dict) -> list[ViewData]:
        with self._lock:
            return build_views(self.projects, self.sections, self.items, self.pending_items,
                               self.completed, settings, self.today_fn(),
                               self.user.get("inbox_project_id"))


def _sort_key_order(x: dict):
    return (x.get("child_order") if x.get("child_order") is not None else 10**9, x.get("order_key") or "")


def _task_row(it: dict, today: _dt.date, depth: int) -> RowData:
    _d, label, state = parse_due(it.get("due"), today)
    return RowData(kind="task", id=it["id"], content=it.get("content", ""),
                   checked=bool(it.get("checked")), depth=depth,
                   priority=int(it.get("priority") or 1), due_text=label, due_state=state,
                   recurring=bool((it.get("due") or {}).get("is_recurring")),
                   pending=bool(it.get("_pending")))


def _tree_rows(tasks: list[dict], today: _dt.date) -> list[RowData]:
    ids = {t["id"] for t in tasks}
    children: dict[str | None, list[dict]] = {}
    for t in tasks:
        parent = t.get("parent_id") if t.get("parent_id") in ids else None
        children.setdefault(parent, []).append(t)
    rows: list[RowData] = []

    def walk(parent, depth):
        for t in sorted(children.get(parent, []), key=_sort_key_order):
            rows.append(_task_row(t, today, min(depth, 4)))
            walk(t["id"], depth + 1)

    walk(None, 0)
    return rows


def build_views(projects, sections, items, pending, completed, settings, today, inbox_id=None):
    mode = settings.get("mode", "project")
    show_completed = set(settings.get("show_completed", []))
    all_items = [i for i in list(items.values()) + list(pending.values()) if not i.get("is_deleted")]
    active = [i for i in all_items if not i.get("checked")]
    views: list[ViewData] = []

    # smart views first
    def due_date(it):
        return parse_due(it.get("due"), today)[0]

    smart = []
    if settings.get("view_today", False):
        smart.append(("today", "Today", lambda d: d is not None and d <= today, "berry_red"))
    if settings.get("view_overdue", False):
        smart.append(("overdue", "Overdue", lambda d: d is not None and d < today, "red"))
    for key, title, pred, colour in smart:
        sel = [i for i in active if pred(due_date(i))]
        sel.sort(key=lambda i: (due_date(i) or today, -(int(i.get("priority") or 1)), _sort_key_order(i)))
        views.append(ViewData(key=key, kind=key, title=title, color=colour,
                              rows=[_task_row(i, today, 0) for i in sel]))
    for label in settings.get("labels", []) or []:
        sel = [i for i in active if label in (i.get("labels") or [])]
        sel.sort(key=_sort_key_order)
        views.append(ViewData(key=f"label:{label}", kind="label", title=f"@{label}", color="grape",
                              label=label, rows=_tree_rows(sel, today)))

    def project_tasks(pid, section_filter=None):
        sel = [i for i in all_items if i.get("project_id") == pid and
               (section_filter is None or (i.get("section_id") or None) == section_filter)]
        open_ = [i for i in sel if not i.get("checked")]
        done = [i for i in sel if i.get("checked")]
        return open_, done

    ordered_projects = sorted(projects.values(), key=lambda p: (0 if p.get("inbox_project") or p["id"] == inbox_id else 1,
                                                                 _sort_key_order(p)))
    for p in ordered_projects:
        pid = p["id"]
        psections = sorted([s for s in sections.values() if s.get("project_id") == pid],
                           key=lambda s: (s.get("section_order") or 0, s.get("order_key") or ""))
        if mode == "section":
            groups = [(None, p["name"])] + [(s["id"], f'{p["name"]} / {s["name"]}') for s in psections]
            for sid, title in groups:
                open_, done = project_tasks(pid, sid)
                if sid is not None or open_ or not psections:
                    key = f"section:{sid}" if sid else f"project:{pid}"
                    v = ViewData(key=key, kind="section" if sid else "project", title=title,
                                 color=p.get("color", "charcoal"), project_id=pid, section_id=sid,
                                 rows=_tree_rows(open_, today))
                    if key in show_completed:
                        v.rows += _completed_rows(done, completed.get(pid, []), today, sid)
                    views.append(v)
        else:
            key = f"project:{pid}"
            open_, done = project_tasks(pid)
            rows = _tree_rows([i for i in open_ if not i.get("section_id") or i.get("section_id") not in sections], today)
            for s in psections:
                srows = _tree_rows([i for i in open_ if i.get("section_id") == s["id"]], today)
                rows.append(RowData(kind="header", id=s["id"], content=s["name"]))
                rows.extend(srows)
            v = ViewData(key=key, kind="project", title=p["name"], color=p.get("color", "charcoal"),
                         project_id=pid, rows=rows)
            if key in show_completed:
                v.rows += _completed_rows(done, completed.get(pid, []), today, None)
            views.append(v)
    return views


def _completed_rows(local_done, remote_done, today, section_id):
    seen, rows = set(), []
    for it in list(local_done) + list(remote_done):
        if section_id is not None and it.get("section_id") != section_id:
            continue
        iid = it.get("id") or it.get("task_id")
        if not iid or iid in seen:
            continue
        seen.add(iid)
        r = _task_row({**it, "id": iid, "checked": True}, today, 0)
        rows.append(r)
    if rows:
        rows.insert(0, RowData(kind="header", id="__completed__", content="Completed"))
    return rows


# --------------------------------------------------------------------------
# Background worker
# --------------------------------------------------------------------------
class SyncWorker(threading.Thread):
    """Applies local edits immediately; syncs every ``interval`` s and ``debounce`` s after edits.
    ``callback(kind, payload)`` with kind in {"snapshot", "local", "synced"}."""

    def __init__(self, engine: TodoistEngine, callback, settings_fn, interval: float = 45,
                 debounce: float = 1.5):
        super().__init__(name="TodoistSync", daemon=True)
        self.engine = engine
        self.callback = callback
        self.settings_fn = settings_fn
        self.interval = max(30.0, float(interval))
        self.debounce = debounce
        self._wake = threading.Event()
        self._stop_evt = threading.Event()
        self._next_periodic = 0.0
        self._debounce_at: float | None = None
        self._now = False

    def payload(self, **extra) -> dict:
        e = self.engine
        d = {"views": e.build_views(self.settings_fn()), "labels": list(e.labels),
             "projects": {pid: p.get("name") for pid, p in e.projects.items()},
             "status": e.status, "error": e.last_error, "last_sync": e.last_sync,
             "user": e.user.get("full_name") or e.user.get("email") or "",
             "pending": e.pending_count()}
        d.update(extra)
        return d

    def submit(self, op: dict) -> None:
        self.engine.submit(op)
        self._debounce_at = time.monotonic() + self.debounce
        self._wake.set()

    def sync_now(self) -> None:
        self._now = True
        self._wake.set()

    def refresh(self) -> None:
        """Rebuild views without syncing (e.g. settings changed)."""
        self.engine.submit({"op": "_noop"})
        self._wake.set()

    def stop(self) -> None:
        self._stop_evt.set()
        self._wake.set()

    def run(self) -> None:
        try:
            self.engine.start()
        except Exception:  # noqa: BLE001
            log.exception("Startup failed")
        self.callback("snapshot", self.payload())
        while not self._stop_evt.is_set():
            events = self.engine.apply_pending()
            if events:
                self.callback("local", self.payload(events=events))
            now = time.monotonic()
            due = [self._next_periodic]
            if self._debounce_at is not None:
                due.append(self._debounce_at)
            if self._now:
                due.append(now)
            if now >= min(due):
                self._now = False
                self._debounce_at = None
                try:
                    result = self.engine.sync()
                except Exception as exc:  # noqa: BLE001
                    log.exception("Unexpected sync failure")
                    self.engine._pause(exc)
                    result = {"ok": False, "error": self.engine.last_error, "conflicts": []}
                self._next_periodic = time.monotonic() + self.interval
                self.callback("synced", self.payload(result=result))
                continue
            self._wake.wait(timeout=max(0.05, min(due) - now))
            self._wake.clear()
        self.engine.apply_pending()
        self.engine.save_cache()
