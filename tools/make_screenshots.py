"""Headless UI smoke test + screenshots (QT_QPA_PLATFORM=offscreen).

Runs the real app on a demo (fake) store with 3 projects, exercises the UI
(complete, quick add, inline edit, delete, settings views), then renders three
project widgets onto a desktop-like background and grabs the setup dialog.
"""
import datetime as dt
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["TDW_DATA_DIR"] = tempfile.mkdtemp(prefix="tdw-")

from PySide6.QtCore import QRect, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPixmap, QRadialGradient  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import todoist_sync as ts  # noqa: E402
import todoist_widgets as tw  # noqa: E402

TODAY = dt.date(2026, 10, 22)


def pump(app, secs=0.3):
    end = time.time() + secs
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def desktop_background(W, H):
    canvas = QPixmap(W, H)
    p = QPainter(canvas)
    grad = QLinearGradient(0, 0, W, H)
    grad.setColorAt(0, QColor("#0B3A6E"))
    grad.setColorAt(0.55, QColor("#1D6FB8"))
    grad.setColorAt(1, QColor("#7FB5E6"))
    p.fillRect(canvas.rect(), grad)
    bloom = QRadialGradient(W * 0.62, H * 0.55, 420)
    bloom.setColorAt(0, QColor(255, 255, 255, 110))
    bloom.setColorAt(1, QColor(255, 255, 255, 0))
    p.fillRect(canvas.rect(), bloom)
    p.fillRect(QRect(0, H - 48, W, 48), QColor(20, 24, 32, 230))
    p.setPen(QColor("white"))
    p.drawText(QRect(W - 140, H - 48, 130, 48), Qt.AlignCenter, "13:35\n22-10-2026")
    p.drawPixmap(W - 175, H - 35, tw.app_icon().pixmap(22, 22))
    return canvas, p


def render_desktop(qapp, app, layout, out):
    W, H = 1280, 760
    canvas, p = desktop_background(W, H)
    for key, (x, y, w_, h_) in layout.items():
        w = app.widgets[key]
        w.resize(w_, h_)
        w.show()
        pump(qapp, 0.2)
        shot = w.grab()  # translucent window: rounded card + soft shadow, transparent corners
        p.drawPixmap(x, y, shot)
    p.end()
    canvas.save(str(out))
    print("saved", out, canvas.width(), "x", canvas.height())


def before_after(root):
    before = QPixmap(str(root / "before-v1.png"))
    after = QPixmap(str(root / "screenshot-widgets.png"))
    crop = QRect(0, 0, 1100, 500)
    b, a = before.copy(crop), after.copy(crop)
    W = b.width() + a.width() + 30
    H = b.height() + 60
    out = QPixmap(W, H)
    out.fill(QColor("#F4F4F4"))
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    f = QFont("Segoe UI", 16)
    f.setBold(True)
    p.setFont(f)
    p.setPen(QColor("#333333"))
    p.drawText(QRect(10, 8, b.width(), 40), Qt.AlignLeft | Qt.AlignVCenter, "Before (v1, sticky-note look)")
    p.drawText(QRect(b.width() + 20, 8, a.width(), 40), Qt.AlignLeft | Qt.AlignVCenter, "After (v2, task-card look)")
    p.drawPixmap(10, 50, b)
    p.drawPixmap(b.width() + 20, 50, a)
    p.end()
    path = root / "before-after.png"
    out.save(str(path))
    print("saved", path, out.width(), "x", out.height())


def main():
    qapp = QApplication(sys.argv[:1])
    qapp.setFont(QFont("Segoe UI", 10))
    engine = tw.build_demo_engine(ts.data_dir(), today=TODAY)
    app = tw.TodoistWidgetsApp(qapp, engine, demo=True)
    app.worker.debounce = 0.2
    app.start()
    pump(qapp, 1.0)
    keys = sorted(app.widgets)
    print("widgets:", keys)
    assert keys == ["project:home", "project:inbox", "project:work"], keys
    assert all(w.windowFlags() & Qt.WindowStaysOnTopHint for w in app.widgets.values()), "on top default"
    work = app.widgets["project:work"]
    rows = [(type(r).__name__, getattr(r, "row", None) and r.row.content) for r in work.row_widgets]
    print("work rows:", rows)
    assert [r.row.depth for r in work.row_widgets if isinstance(r, tw.TaskRow)][:3] == [0, 1, 1], "subtasks indented"
    assert any(isinstance(r, tw.SectionHeader) and r.text() == "Meetings" for r in work.row_widgets)
    assert work.accent_colour() == "#4073FF" and app.widgets["project:home"].accent_colour() == "#7ECC49"
    assert app.widgets["project:inbox"].accent_colour() == "#DC4C3E", "charcoal/grey -> Todoist red"
    assert work.count_lbl.text() == "6"
    assert work.testAttribute(Qt.WA_TranslucentBackground)
    # legacy v1 colour override in layout.json still works (as accent bar colour)
    work.lay["color"] = "Purple"
    assert work.accent_colour() == "#884DFF"
    work.set_colour("teal")
    assert work.accent_colour() == "#158FAD"
    work.set_colour(None)
    assert work.accent_colour() == "#4073FF"
    print("accent colours OK (project colour, grey->red, override, legacy override)")
    first = work.row_widgets[0]
    assert first.row.due_state == "overdue" and first.row.priority == 4

    # complete a normal task -> disappears
    inbox = app.widgets["project:inbox"]
    t = next(r for r in inbox.row_widgets if isinstance(r, tw.TaskRow) and r.row.content == "Pick up dry cleaning")
    t.check.setChecked(True)
    pump(qapp, 0.8)
    assert engine.items["i4"]["checked"] and not any(
        isinstance(r, tw.TaskRow) and r.row.id == "i4" for r in inbox.row_widgets)
    # recurring task stays visible (pending) when completed
    t = next(r for r in inbox.row_widgets if isinstance(r, tw.TaskRow) and r.row.id == "i2")
    t.check.setChecked(True)
    pump(qapp, 0.8)
    assert any(isinstance(r, tw.TaskRow) and r.row.id == "i2" and r.row.pending for r in inbox.row_widgets)
    print("complete OK (normal hides, recurring stays until Todoist reschedules)")

    # inline edit
    home = app.widgets["project:home"]
    row = next(r for r in home.row_widgets if isinstance(r, tw.TaskRow) and r.row.id == "h6")
    row.edit.setText("Plan weekend trip to Ghent")
    row.edit.editingFinished.emit()
    pump(qapp, 0.8)
    assert engine.items["h6"]["content"] == "Plan weekend trip to Ghent"
    assert engine.queue[-1]["type"] == "item_update"
    print("inline edit OK")

    # delete with confirm (auto-confirm)
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
    row = next(r for r in home.row_widgets if isinstance(r, tw.TaskRow) and r.row.id == "h5")
    row.del_btn.click()
    pump(qapp, 0.8)
    assert engine.items["h5"]["is_deleted"] and engine.queue[-1]["type"] == "item_delete"
    print("delete OK")

    # quick add from the Work widget
    work.add_edit.setText("Send invoice tomorrow p1")
    work._quick_add()
    pump(qapp, 0.8)
    assert engine.quick_queue[-1]["project_id"] == "work"
    assert any(isinstance(r, tw.TaskRow) and r.row.pending and r.row.content == "Send invoice tomorrow p1"
               for r in work.row_widgets)
    print("quick add OK (queued with project, shown as pending)")

    # extra views + section mode
    s = app.layout_store.settings
    s["view_today"] = True
    s["labels"] = ["errand"]
    app.worker.refresh()
    pump(qapp, 0.8)
    assert "today" in app.widgets and "label:errand" in app.widgets
    today_titles = [r.row.content for r in app.widgets["today"].row_widgets if isinstance(r, tw.TaskRow)]
    print("today view:", today_titles)
    assert today_titles[0] == "Renew passport"
    s["mode"] = "section"
    app.worker.refresh()
    pump(qapp, 0.8)
    assert "section:s-meet" in app.widgets
    print("section mode OK:", sorted(app.widgets))

    # paused indicator
    engine.status, engine.last_error = "paused", "offline (no connection to Todoist)"
    app.demo = False
    app.status_info["pending"] = 3
    app.update_status()
    print("tray status (paused):", app.status_action.text())
    assert "paused" in app.status_action.text().lower()
    assert app.widgets["project:work"].status_lbl.text() == "sync paused · 3 waiting"

    # ---- screenshot: fresh demo store, 3 project widgets ----
    app.quit()
    pump(qapp, 0.3)
    for w in list(app.widgets.values()):
        w.close()
    os.environ["TDW_DATA_DIR"] = tempfile.mkdtemp(prefix="tdw-shot-")
    engine2 = tw.build_demo_engine(ts.data_dir(), today=TODAY)
    app2 = tw.TodoistWidgetsApp(qapp, engine2, demo=True)
    app2.start()
    pump(qapp, 1.0)
    layout = {"project:inbox": (40, 40, 320, 330), "project:work": (390, 56, 340, 410),
              "project:home": (760, 30, 330, 370)}
    render_desktop(qapp, app2, layout, ROOT / "screenshot-widgets.png")
    app2.layout_store.settings["theme"] = "Dark"
    app2.apply_theme()
    pump(qapp, 0.3)
    assert app2.theme_name() == "Dark"
    render_desktop(qapp, app2, layout, ROOT / "screenshot-dark.png")
    app2.layout_store.settings["theme"] = "Light"
    app2.apply_theme()
    pump(qapp, 0.3)
    if (ROOT / "before-v1.png").exists():
        before_after(ROOT)
    else:
        print("before-v1.png not present - before-after.png not regenerated")

    dlg = tw.SetupDialog(app2)
    dlg.show()
    pump(qapp, 0.4)
    shot = dlg.grab()
    out2 = ROOT / "screenshot-setup.png"
    shot.save(str(out2))
    print("saved", out2, shot.width(), "x", shot.height())
    dlg.token.setText("not a token")
    dlg.connect_token()
    assert "does not look like" in dlg.status.text()
    print("setup validation OK")
    # ---- global hotkeys ----
    app = app2
    assert app.layout_store.settings["hotkeys"] == {"toggle": "Ctrl+Alt+T", "show": "", "hide": ""}
    errs = app.apply_hotkeys(notify=False)
    assert errs == [] and app.hotkeys.active == {"toggle": "Ctrl+Alt+T"}, (errs, app.hotkeys.active)
    app.on_hotkey("toggle")
    assert not any(w.isVisible() for w in app.widgets.values()), "toggle should hide all"
    app.on_hotkey("toggle")
    assert any(w.isVisible() for w in app.widgets.values()), "toggle should show all"
    app.on_hotkey("hide")
    assert not any(w.isVisible() for w in app.widgets.values())
    app.on_hotkey("show")
    assert all(w.isVisible() for w in app.widgets.values())
    app.layout_store.settings["hotkeys"] = {"toggle": "Ctrl+Alt+T", "show": "Ctrl+Alt+T", "hide": "Alt+H"}
    errs = app.apply_hotkeys(notify=False)
    print("hotkey conflicts detected:", errs)
    assert [e[0] for e in errs] == ["show", "hide"]
    app.layout_store.settings["hotkeys"] = {"toggle": "Ctrl+Alt+T", "show": "", "hide": ""}
    app.apply_hotkeys(notify=False)
    sd = tw.SettingsDialog(app)
    sd.show()
    pump(qapp, 0.4)
    shot = sd.grab()
    shot.save(str(ROOT / "screenshot-settings.png"))
    print("saved", ROOT / "screenshot-settings.png", shot.width(), "x", shot.height())
    warned = []
    QMessageBox.warning = staticmethod(lambda *a, **k: warned.append(a))
    sd.hotkey_group.edits["show"].setKeySequence("Ctrl+Alt+Shift+T")
    sd._save()
    assert not warned and app.layout_store.settings["hotkeys"]["show"] == "Ctrl+Alt+Shift+T"
    sd2 = tw.SettingsDialog(app)
    sd2.hotkey_group.edits["hide"].setKeySequence("Ctrl+Alt+T")
    sd2._save()
    assert warned and sd2.result() == 0, "conflict must show a message and keep the dialog open"
    sd2.hotkey_group.edits["show"].clear()
    sd2.hotkey_group.edits["hide"].clear()
    sd2._save()
    assert app.layout_store.settings["hotkeys"] == {"toggle": "Ctrl+Alt+T", "show": "", "hide": ""}
    print("hotkeys OK (toggle/show/hide, duplicate + single-modifier rejected, settings save + conflict message)")
    for d in (sd, sd2, dlg):
        d.close()
        d.deleteLater()
    pump(qapp, 0.2)

    app2.quit()
    print("UI SMOKE TEST PASSED")


if __name__ == "__main__":
    main()
