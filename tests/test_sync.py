"""Unit tests for the Todoist sync layer against a mocked Todoist API v1.

FakeTodoist implements the parts of https://api.todoist.com/api/v1 that the app uses
(/sync with sync_token + commands, /tasks/quick, /tasks/completed/by_completion_date)
behind a fake requests.Session, so the real HTTP client code is exercised too.
"""
import datetime as dt
import json
import re
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import todoist_sync as ts  # noqa: E402

TOKEN = "0123456789abcdef0123456789abcdef01234567"  # dummy test token  gitleaks:allow
TODAY = dt.date(2026, 10, 22)


class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.content = b"" if body is None else json.dumps(body).encode()

    def json(self):
        return self._body


class FakeTodoist:
    def __init__(self):
        self.version = 1
        self.online = True
        self.token = TOKEN
        self.projects = {}
        self.sections = {}
        self.items = {}
        self.changed = {}  # id -> version
        self.requests = []
        self.sync_tokens = []
        self.commands_seen = []
        self._n = 0
        self.user = {"id": "u1", "full_name": "Demo User", "email": "m@example.com",
                     "inbox_project_id": "inbox"}
        self.add_project("inbox", "Inbox", "charcoal", inbox=True)

    # helpers ------------------------------------------------------------
    def _id(self, prefix="t"):
        self._n += 1
        return f"{prefix}{self._n}"

    def _touch(self, obj_id):
        self.version += 1
        self.changed[obj_id] = self.version

    def add_project(self, pid, name, color, inbox=False):
        self.projects[pid] = {"id": pid, "name": name, "color": color, "child_order": len(self.projects),
                              "is_deleted": False, "is_archived": False, "inbox_project": inbox}
        self._touch(pid)

    def add_section(self, sid, pid, name, order=1):
        self.sections[sid] = {"id": sid, "project_id": pid, "name": name, "section_order": order,
                              "is_deleted": False, "is_archived": False}
        self._touch(sid)

    def add_item(self, content, project_id="inbox", **kw):
        iid = kw.pop("id", None) or self._id()
        it = {"id": iid, "content": content, "project_id": project_id, "section_id": None,
              "parent_id": None, "child_order": len(self.items) + 1, "priority": 1, "due": None,
              "checked": False, "is_deleted": False, "labels": [], "completed_at": None}
        it.update(kw)
        self.items[iid] = it
        self._touch(iid)
        return it

    def remote_edit(self, iid, **kw):
        self.items[iid].update(kw)
        self._touch(iid)

    # transport ------------------------------------------------------------
    def request(self, method, url, headers=None, timeout=None, data=None, json=None, params=None):
        self.requests.append((method, url))
        if not self.online:
            raise requests.ConnectionError("network down")
        if headers.get("Authorization") != f"Bearer {self.token}":
            return FakeResponse(401, {"error": "Unauthorized"})
        path = url.split("/api/v1", 1)[1]
        if path == "/sync":
            return FakeResponse(200, self._sync(data))
        if path == "/tasks/quick":
            return FakeResponse(200, self._quick(json["text"]))
        if path == "/tasks/completed/by_completion_date":
            done = [i for i in self.items.values() if i["checked"] and i["project_id"] == params["project_id"]]
            return FakeResponse(200, {"items": done, "next_cursor": None})
        return FakeResponse(404, {"error": "not found"})

    def _sync(self, data):
        token = data["sync_token"]
        self.sync_tokens.append(token)
        types = __import__("json").loads(data["resource_types"])
        cmds = __import__("json").loads(data.get("commands", "[]"))
        status = {}
        for c in cmds:
            self.commands_seen.append(c["type"])
            status[c["uuid"]] = self._apply(c)
        full = token == "*"
        since = 0 if full else int(token)
        out = {"sync_token": str(self.version), "full_sync": full, "sync_status": status,
               "temp_id_mapping": {}}
        if "user" in types:
            out["user"] = self.user
        if types == ["user"]:
            return out

        def pick(coll, active_only):
            res = []
            for o in coll.values():
                if full and active_only and (o.get("checked") or o.get("is_deleted")):
                    continue
                if full or self.changed.get(o["id"], 0) > since:
                    res.append(dict(o))
            return res

        out["projects"] = pick(self.projects, True)
        out["sections"] = pick(self.sections, True)
        out["items"] = pick(self.items, True)
        out["labels"] = [{"name": "errand", "is_deleted": False}] if full else []
        return out

    def _apply(self, c):
        a = c["args"]
        it = self.items.get(a.get("id"))
        if it is None or it["is_deleted"]:
            return {"error_tag": "ITEM_NOT_FOUND", "error": "Task not found", "http_code": 404}
        t = c["type"]
        if t == "item_close":
            due = it.get("due")
            if due and due.get("is_recurring"):
                d = dt.date.fromisoformat(due["date"]) + dt.timedelta(days=1)
                it["due"] = dict(due, date=d.isoformat())
            else:
                it["checked"] = True
                it["completed_at"] = "2026-10-22T10:00:00Z"
        elif t == "item_uncomplete":
            it["checked"] = False
        elif t == "item_update":
            it.update({k: v for k, v in a.items() if k != "id"})
        elif t == "item_delete":
            it["is_deleted"] = True
            for ch in self.items.values():
                if ch.get("parent_id") == it["id"]:
                    ch["is_deleted"] = True
                    self._touch(ch["id"])
        elif t == "item_move":
            if "section_id" in a:
                it["section_id"] = a["section_id"]
                it["project_id"] = self.sections[a["section_id"]]["project_id"]
            else:
                it["project_id"] = a["project_id"]
                it["section_id"] = None
        else:
            return {"error": "unknown", "http_code": 400}
        self._touch(it["id"])
        return "ok"

    def _quick(self, text):
        kw = {}
        proj = "inbox"
        m = re.search(r"#(\w+)", text)
        if m:
            for p in self.projects.values():
                if p["name"].lower() == m.group(1).lower():
                    proj = p["id"]
        if re.search(r"\bp1\b", text):
            kw["priority"] = 4
        if "tomorrow" in text:
            kw["due"] = {"date": (TODAY + dt.timedelta(days=1)).isoformat(), "is_recurring": False,
                         "string": "tomorrow"}
        content = re.sub(r"\s*(#\w+|\bp[1-4]\b|\btomorrow\b)", "", text).strip()
        return self.add_item(content, proj, **kw)


@pytest.fixture
def server():
    s = FakeTodoist()
    s.add_project("work", "Work", "blue")
    s.add_section("s1", "work", "Meetings")
    s.add_item("Write report", "work", id="w1", priority=4,
               due={"date": "2026-10-21", "is_recurring": False, "string": "yesterday"})
    s.add_item("Outline", "work", id="w2", parent_id="w1")
    s.add_item("Book room", "work", id="w3", section_id="s1")
    s.add_item("Water plants", "inbox", id="r1",
               due={"date": "2026-10-22", "is_recurring": True, "string": "every day"})
    return s


@pytest.fixture
def engine(tmp_path, server):
    eng = ts.TodoistEngine(creds=ts.MemoryCredentialStore(TOKEN), directory=tmp_path,
                           client_factory=lambda tok: ts.TodoistClient(tok, session=server),
                           today_fn=lambda: TODAY)
    eng.start()
    return eng


def view(engine, key, settings=None):
    return next(v for v in engine.build_views(settings or {}) if v.key == key)


def test_full_then_incremental_sync(engine, server):
    assert engine.sync()["ok"]
    assert server.sync_tokens[-1] == "*" and engine.full_syncs == 1
    first_token = engine.sync_token
    server.add_item("From phone", "work", id="w9")
    assert engine.sync()["ok"]
    assert server.sync_tokens[-1] == first_token, "second sync must be incremental"
    assert engine.full_syncs == 1
    assert "w9" in engine.items
    assert engine.user["full_name"] == "Demo User"


def test_views_projects_sections_subtasks_due(engine):
    engine.sync()
    work = view(engine, "project:work")
    assert work.title == "Work" and work.color == "blue"
    assert [(r.kind, r.content, r.depth) for r in work.rows] == [
        ("task", "Write report", 0), ("task", "Outline", 1), ("header", "Meetings", 0), ("task", "Book room", 0)]
    wr = work.rows[0]
    assert wr.priority == 4 and wr.due_state == "overdue" and wr.due_text == "Yesterday"
    today = view(engine, "today", {"view_today": True})
    assert [r.content for r in today.rows] == ["Write report", "Water plants"]
    assert today.rows[1].due_text == "Today" and today.rows[1].recurring
    # per-section mode
    keys = [v.key for v in engine.build_views({"mode": "section", "view_today": False})]
    assert "section:s1" in keys and "project:work" in keys


def test_complete_and_uncomplete(engine, server):
    engine.sync()
    engine.submit({"op": "complete", "id": "w3"})
    engine.apply_pending()
    assert all(r.id != "w3" for r in view(engine, "project:work").rows)  # optimistic
    assert engine.sync()["ok"]
    assert server.items["w3"]["checked"] is True
    engine.submit({"op": "uncomplete", "id": "w3"})
    engine.apply_pending()
    assert engine.sync()["ok"]
    assert server.items["w3"]["checked"] is False
    assert any(r.id == "w3" for r in view(engine, "project:work").rows)
    assert server.commands_seen[-2:] == ["item_close", "item_uncomplete"]


def test_recurring_completion_advances_date(engine, server):
    engine.sync()
    engine.submit({"op": "complete", "id": "r1"})
    engine.apply_pending()
    pend = next(r for r in view(engine, "project:inbox").rows if r.id == "r1")
    assert pend.pending and not pend.checked  # recurring task stays visible until Todoist reschedules it
    engine.sync()
    assert server.items["r1"]["checked"] is False
    assert engine.items["r1"]["due"]["date"] == "2026-10-23"
    row = next(r for r in view(engine, "project:inbox").rows if r.id == "r1")
    assert not row.checked and row.due_text == "Tomorrow" and row.recurring


def test_edit_content_coalesced(engine, server):
    engine.sync()
    for t in ["Write", "Write the", "Write the Q4 report"]:
        engine.submit({"op": "update", "id": "w1", "content": t})
    engine.apply_pending()
    assert len(engine.queue) == 1
    engine.sync()
    assert server.items["w1"]["content"] == "Write the Q4 report"


def test_delete_with_subtasks(engine, server):
    engine.sync()
    engine.submit({"op": "delete", "id": "w1"})
    engine.apply_pending()
    assert [r.id for r in view(engine, "project:work").rows if r.kind == "task"] == ["w3"]
    engine.sync()
    assert server.items["w1"]["is_deleted"] and server.items["w2"]["is_deleted"]
    assert "w1" not in engine.items and "w2" not in engine.items


def test_quick_add_syntax_and_move_to_widget_project(engine, server):
    engine.sync()
    engine.submit({"op": "quick_add", "text": "Call bank tomorrow p1", "project_id": "work"})
    engine.apply_pending()
    pend = [r for r in view(engine, "project:work").rows if r.pending]
    assert len(pend) == 1  # shows immediately, marked pending
    assert engine.sync()["ok"]
    new = next(i for i in server.items.values() if i["content"] == "Call bank")
    assert new["priority"] == 4 and new["due"]["date"] == "2026-10-23"
    assert new["project_id"] == "work", "added from the Work widget -> moved to Work"
    assert "item_move" in server.commands_seen
    # explicit #Project wins
    engine.submit({"op": "quick_add", "text": "Plan offsite #Work", "project_id": "inbox"})
    engine.apply_pending()
    engine.sync()
    new2 = next(i for i in server.items.values() if i["content"] == "Plan offsite")
    assert new2["project_id"] == "work"
    assert not engine.pending_items and not engine.quick_queue


def test_offline_queue_survives_restart(engine, server, tmp_path):
    engine.sync()
    server.online = False
    engine.submit({"op": "complete", "id": "w3"})
    engine.submit({"op": "update", "id": "w1", "content": "Offline edit"})
    engine.submit({"op": "quick_add", "text": "Buy stamps", "project_id": "work"})
    engine.apply_pending()
    res = engine.sync()
    assert not res["ok"] and engine.status == "paused" and "offline" in engine.last_error
    assert engine.pending_count() == 3
    work = view(engine, "project:work")
    assert work.rows[0].content == "Offline edit" and any(r.content == "Buy stamps" for r in work.rows)

    eng2 = ts.TodoistEngine(creds=ts.MemoryCredentialStore(TOKEN), directory=tmp_path,
                            client_factory=lambda tok: ts.TodoistClient(tok, session=server),
                            today_fn=lambda: TODAY)
    eng2.start()
    assert eng2.pending_count() == 3 and view(eng2, "project:work").rows[0].content == "Offline edit"
    server.online = True
    assert eng2.sync()["ok"] and eng2.pending_count() == 0
    assert server.items["w3"]["checked"] and server.items["w1"]["content"] == "Offline edit"
    assert any(i["content"] == "Buy stamps" and i["project_id"] == "work" for i in server.items.values())


def test_edit_of_task_deleted_remotely_is_dropped(engine, server):
    engine.sync()
    server.remote_edit("w3", is_deleted=True)
    engine.submit({"op": "update", "id": "w3", "content": "too late"})
    engine.apply_pending()
    assert engine.sync()["ok"]
    assert engine.queue == [] and "w3" not in engine.items


def test_conflict_logged(engine, server, caplog):
    engine.sync()
    engine.submit({"op": "update", "id": "w1", "content": "Mine"})
    engine.apply_pending()
    orig = server._apply

    def apply_then_other_client(c):
        r = orig(c)
        server.remote_edit("w1", content="Theirs (later)")
        return r

    server._apply = apply_then_other_client
    with caplog.at_level("WARNING", logger="todoistwidgets"):
        res = engine.sync()
    assert res["conflicts"] == ["w1"] and "last write wins" in caplog.text
    assert engine.items["w1"]["content"] == "Theirs (later)"


def test_bad_token(tmp_path, server):
    eng = ts.TodoistEngine(creds=ts.MemoryCredentialStore("bad" * 14), directory=tmp_path,
                           client_factory=lambda tok: ts.TodoistClient(tok, session=server))
    eng.start()
    res = eng.sync()
    assert not res["ok"] and "rejected the API token" in eng.last_error
    with pytest.raises(ts.AuthError):
        eng.set_token("bad" * 14)


def test_set_token_validates_and_stores(tmp_path, server):
    creds = ts.MemoryCredentialStore()
    eng = ts.TodoistEngine(creds=creds, directory=tmp_path,
                           client_factory=lambda tok: ts.TodoistClient(tok, session=server))
    eng.start()
    assert eng.status == "signed_out"
    user = eng.set_token(f'  "{TOKEN}" ')
    assert user["id"] == "u1" and creds.get() == TOKEN
    assert eng.sync()["ok"]
    for f in tmp_path.iterdir():
        assert TOKEN not in f.read_text(encoding="utf-8", errors="ignore"), f


def test_show_completed(engine, server):
    engine.sync()
    engine.submit({"op": "complete", "id": "w3"})
    engine.apply_pending()
    engine.set_show_completed("work", True)
    engine.sync()
    v = view(engine, "project:work", {"show_completed": ["project:work"]})
    assert v.rows[-2].kind == "header" and v.rows[-1].id == "w3" and v.rows[-1].checked
    assert "w3" not in [r.id for r in view(engine, "project:work").rows]
    # reopen a task known only from the server's completed list (e.g. after a restart)
    engine.items.pop("w3")
    engine.submit({"op": "uncomplete", "id": "w3"})
    engine.apply_pending()
    assert engine.sync()["ok"] and server.items["w3"]["checked"] is False
    assert "w3" in [r.id for r in view(engine, "project:work").rows]


def test_parse_due_labels():
    f = lambda d: ts.parse_due({"date": d}, TODAY)[1:]  # noqa: E731
    assert f("2026-10-22") == ("Today", "today")
    assert f("2026-10-23") == ("Tomorrow", "tomorrow")
    assert f("2026-10-21") == ("Yesterday", "overdue")
    assert f("2026-10-15") == ("15 Oct", "overdue")
    assert f("2026-10-26") == ("Monday", "future")
    assert f("2027-01-05") == ("5 Jan 2027", "future")
    assert f("2026-10-22T14:30:00")[0] == "Today 14:30"


def test_worker_debounce(engine, server):
    import threading
    import time
    got, done = [], threading.Event()

    def cb(kind, payload):
        got.append(kind)
        if kind == "synced" and got.count("synced") >= 2:
            done.set()

    w = ts.SyncWorker(engine, cb, settings_fn=lambda: {}, interval=60, debounce=0.3)
    w.start()
    time.sleep(0.3)
    w.submit({"op": "complete", "id": "w3"})
    assert done.wait(5)
    w.stop(); w.join(5)
    assert got[0] == "snapshot" and "local" in got and server.items["w3"]["checked"]
