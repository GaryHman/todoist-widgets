#!/usr/bin/env python3
"""Todoist Widgets - your Todoist projects as task-card desktop widgets.

Run with pythonw (no console):  pythonw todoist_widgets.py
Options:  --demo   show sample projects without signing in (nothing is synced)

Uses the official Todoist API v1. Not created by, affiliated with, or supported by Todoist.
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # the app folder is shared/synced between PCs: keep no __pycache__ there

if sys.version_info < (3, 11):
    sys.stderr.write("Todoist Widgets needs Python 3.11 or newer.\n")
    sys.exit(1)

import argparse
import copy
import datetime as _dt
import logging
import os
import subprocess
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

_APP_NAME = "Todoist Widgets"
APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

def _startup_failure(exc: BaseException) -> None:
    """A required package is missing/broken (e.g. an incomplete .venv). pythonw has no console,
    so write the reason to the log file and show a Windows message box instead of exiting silently."""
    import ctypes
    import datetime
    from pathlib import Path as _P
    msg = (f"{_APP_NAME} could not start because a required Python package is missing or broken:\n\n"
           f"{exc}\n\nPython used: {sys.executable}\n\nRun install.bat in\n{_P(__file__).resolve().parent}\n"
           "or install the packages with:  py -m pip install -r requirements.txt")
    try:
        d = _P(os.environ.get("TDW_DATA_DIR") or _P(os.environ.get("APPDATA", str(_P.home()))) / "TodoistWidgets")
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "todoistwidgets.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} ERROR Startup failed: {exc!r} "
                    f"(python: {sys.executable})\n")
    except OSError:
        pass
    if sys.stderr is not None:
        sys.stderr.write(msg + "\n")
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(None, msg, _APP_NAME, 0x10)
        except Exception:  # noqa: BLE001
            pass
    sys.exit(1)


try:
    from PySide6.QtCore import QLockFile, QObject, QPoint, QPointF, QRectF, Qt, QTimer, QUrl, Signal
    from PySide6.QtGui import (QColor, QDesktopServices, QFont, QGuiApplication, QIcon, QPainter,
                               QPainterPath, QPalette, QPen, QPixmap)
    from PySide6.QtWidgets import (QAbstractButton, QApplication, QButtonGroup, QCheckBox, QComboBox,
                                   QDialog, QDialogButtonBox, QGraphicsDropShadowEffect,
                                   QFormLayout, QFrame, QGroupBox, QHBoxLayout, QInputDialog, QLabel,
                                   QLineEdit, QMenu, QMessageBox, QPushButton, QRadioButton,
                                   QScrollArea, QSizeGrip, QSpinBox, QSystemTrayIcon,
                                   QToolButton, QVBoxLayout, QWidget)

    import todoist_sync as ts  # noqa: E402
    import win_hotkeys as hk  # noqa: E402
except ImportError as _exc:  # pragma: no cover - exercised on broken installs
    _startup_failure(_exc)


APP_TITLE = "Todoist Widgets"
log = logging.getLogger("todoistwidgets")
TOKEN_URL = "https://app.todoist.com/app/settings/integrations/developer"

# Official Todoist project colours (name -> hex)
TODOIST_COLOURS = {
    "berry_red": "#B8255F", "red": "#DC4C3E", "orange": "#FF9933", "yellow": "#FAD000",
    "olive_green": "#AFB83B", "lime_green": "#7ECC49", "green": "#299438", "mint_green": "#6ACCBC",
    "teal": "#158FAD", "sky_blue": "#14AAF5", "light_blue": "#96C3EB", "blue": "#4073FF",
    "grape": "#884DFF", "violet": "#AF38EB", "lavender": "#EB96EB", "magenta": "#E05194",
    "salmon": "#FF8D85", "charcoal": "#808080", "grey": "#B8B8B8", "taupe": "#CCAC93",
}
TODOIST_RED = "#DC4C3E"
NEUTRAL_PROJECT_COLOURS = {"charcoal", "grey"}  # these use Todoist red as accent
# v1 (sticky-notes) per-widget overrides -> Todoist colour names, so old layout.json keeps working
LEGACY_COLOUR_NAMES = {"Yellow": "yellow", "Orange": "orange", "Green": "green", "Pink": "magenta",
                       "Purple": "grape", "Blue": "blue", "Gray": "grey", "Charcoal": "charcoal"}
PRIORITY_COLOURS = {4: "#D1453B", 3: "#EB8909", 2: "#246FE0", 1: "#999999"}
DUE_COLOURS = {"overdue": "#D1453B", "today": "#058527", "tomorrow": "#AD6200", "future": "#692EC2"}
DUE_COLOURS_DARK = {"overdue": "#FF7066", "today": "#25B84C", "tomorrow": "#FF9A14", "future": "#A970FF"}
THEMES = {
    "Light": {"card": "#FFFFFF", "text": "#202020", "sub": "#808080", "sep": "#EEEEEE",
              "pill": "#F0F0F0", "hover": "#F7F7F7", "edit": "#FFF6F5", "border": "rgba(0,0,0,0.08)",
              "shadow": 70},
    "Dark": {"card": "#1E1E1E", "text": "#E8E8E8", "sub": "#9A9A9A", "sep": "#2C2C2C",
             "pill": "#333333", "hover": "#262626", "edit": "#2A2120", "border": "rgba(255,255,255,0.08)",
             "shadow": 140},
}
SHADOW_MARGIN = 12  # transparent margin around the card for the drop shadow
DEFAULT_W, DEFAULT_H = 300, 320


def accent_for(colour_name: str | None) -> str:
    if not colour_name or colour_name in NEUTRAL_PROJECT_COLOURS:
        return TODOIST_RED
    return TODOIST_COLOURS.get(colour_name, TODOIST_RED)


# --------------------------------------------------------------------------
# Helpers (shared design with KeepNotes Widgets)
# --------------------------------------------------------------------------
def setup_logging(directory: Path) -> None:
    handler = RotatingFileHandler(directory / "todoistwidgets.log", maxBytes=512_000, backupCount=2,
                                  encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    if sys.stderr is not None:
        log.addHandler(logging.StreamHandler())


def draw_icon_pixmap(size: int) -> QPixmap:
    """Red rounded square with a white check mark (distinct from KeepNotes Widgets)."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    s = size / 64.0
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(0, 0, 0, 45))
    p.drawRoundedRect(QRectF(5 * s, 7 * s, 54 * s, 54 * s), 13 * s, 13 * s)
    p.setBrush(QColor(TODOIST_RED))
    p.drawRoundedRect(QRectF(5 * s, 4 * s, 54 * s, 54 * s), 13 * s, 13 * s)
    pen = QPen(QColor("#FFFFFF"), 7 * s, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    path = QPainterPath(QPointF(18 * s, 32 * s))
    path.lineTo(28 * s, 42 * s)
    path.lineTo(46 * s, 21 * s)
    p.drawPath(path)
    p.end()
    return pm


def app_icon(paused: bool = False) -> QIcon:
    ico = APP_DIR / "icon.ico"
    base = QPixmap(str(ico)) if ico.exists() else QPixmap()
    if base.isNull():
        base = draw_icon_pixmap(64)
    if not paused:
        return QIcon(base)
    pm = base.scaled(64, 64, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QColor("#FFFFFF"))
    p.setBrush(QColor("#E8710A"))
    p.drawEllipse(34, 34, 28, 28)
    p.setBrush(QColor("#FFFFFF"))
    p.drawRect(42, 41, 4, 14)
    p.drawRect(50, 41, 4, 14)
    p.end()
    return QIcon(pm)


def calendar_pixmap(colour: str, size: int = 12) -> QPixmap:
    pm = QPixmap(size * 2, size * 2)  # 2x for crisp HiDPI
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    c = QColor(colour)
    p.setPen(QPen(c, 2.2))
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(QRectF(2.5, 4.5, size * 2 - 5, size * 2 - 7), 3, 3)
    p.setBrush(c)
    p.drawRect(QRectF(2.5, 4.5, size * 2 - 5, 4))
    p.setPen(QPen(c, 2.4, Qt.SolidLine, Qt.RoundCap))
    p.drawLine(QPointF(8, 2), QPointF(8, 6))
    p.drawLine(QPointF(size * 2 - 8, 2), QPointF(size * 2 - 8, 6))
    p.end()
    pm.setDevicePixelRatio(2)
    return pm


def startup_shortcut_path() -> Path | None:
    if os.name != "nt":
        return None
    return (Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" /
            "Programs" / "Startup" / "Todoist Widgets.lnk")


def pythonw_executable() -> str:
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.exists() else exe)


def create_shortcut(lnk: Path, target: str, args: str, workdir: str, icon: str) -> None:
    env = dict(os.environ, TDW_LNK=str(lnk), TDW_TARGET=target, TDW_ARGS=args, TDW_WD=workdir,
               TDW_ICON=icon)
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:TDW_LNK);"
          "$s.TargetPath=$env:TDW_TARGET;$s.Arguments=$env:TDW_ARGS;"
          "$s.WorkingDirectory=$env:TDW_WD;$s.IconLocation=$env:TDW_ICON;"
          "$s.Description='Todoist Widgets';$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], env=env,
                   check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=30)


class LayoutStore:
    DEFAULT_SETTINGS = {"mode": "project", "view_today": False, "view_overdue": False, "labels": [],
                        "hide_empty": False, "interval": 45, "show_completed": [],
                        "theme": "Light",
                        "hotkeys": {"toggle": "Ctrl+Alt+T", "show": "", "hide": ""}}

    def __init__(self, path: Path):
        self.path = path
        self.data = ts.read_json(path, {})
        self.data.setdefault("widgets", {})
        self.data.setdefault("settings", {})
        for k, v in self.DEFAULT_SETTINGS.items():
            self.data["settings"].setdefault(k, copy.deepcopy(v))
        self._timer = QTimer()
        self._timer.setSingleShot(True)
        self._timer.setInterval(600)
        self._timer.timeout.connect(self.save)

    @property
    def settings(self) -> dict:
        return self.data["settings"]

    def widget(self, key: str) -> dict:
        return self.data["widgets"].setdefault(key, {})

    def save_soon(self) -> None:
        self._timer.start()

    def save(self) -> None:
        try:
            ts.atomic_write_json(self.path, self.data)
        except OSError:
            log.exception("Could not save layout.json")


# --------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------
class HeaderBar(QWidget):
    """Header row; doubles as the drag handle."""

    def __init__(self, owner):
        super().__init__(owner)
        self._drag = None
        self.setCursor(Qt.SizeAllCursor)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            handle = self.window().windowHandle()
            if handle is not None and handle.startSystemMove():
                return
            self._drag = ev.globalPosition().toPoint() - self.window().frameGeometry().topLeft()

    def mouseMoveEvent(self, ev):
        if self._drag is not None and ev.buttons() & Qt.LeftButton:
            self.window().move(ev.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, ev):
        self._drag = None


class CheckCircle(QAbstractButton):
    """Todoist-style round checkbox: ring in priority colour, tick on hover, filled when done."""

    def __init__(self, priority: int, checked: bool):
        super().__init__()
        self.priority = priority
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(20, 20)
        self._hover = False

    def enterEvent(self, ev):
        self._hover = True
        self.update()
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        self._hover = False
        self.update()
        super().leaveEvent(ev)

    def paintEvent(self, ev):
        c = QColor(PRIORITY_COLOURS.get(self.priority, PRIORITY_COLOURS[1]))
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(2, 2, 16, 16)
        if self.isChecked():
            p.setPen(QPen(c, 1.6))
            p.setBrush(c)
        else:
            fill = QColor(c)
            fill.setAlpha(0 if self.priority == 1 else 28)
            if self._hover:
                fill.setAlpha(60)
            p.setPen(QPen(c, 1.6))
            p.setBrush(fill)
        p.drawEllipse(r)
        if self.isChecked() or self._hover:
            p.setPen(QPen(QColor("#FFFFFF") if self.isChecked() else c, 1.6, Qt.SolidLine, Qt.RoundCap,
                          Qt.RoundJoin))
            path = QPainterPath(QPointF(6.5, 10.2))
            path.lineTo(9, 12.6)
            path.lineTo(13.6, 7.6)
            p.drawPath(path)
        p.end()


class TaskRow(QWidget):
    def __init__(self, w: "ViewWidget", row: ts.RowData):
        super().__init__()
        self.w = w
        self.row = row
        self.setObjectName("taskRow")
        self.setAttribute(Qt.WA_StyledBackground, True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(row.depth * 22, 6, 0, 6)
        lay.setSpacing(8)
        self.check = CheckCircle(row.priority, row.checked)
        self.check.setToolTip("Complete" if not row.checked else "Mark as not completed")
        self.check.toggled.connect(self._toggled)
        lay.addWidget(self.check, 0, Qt.AlignTop)

        col = QVBoxLayout()
        col.setSpacing(1)
        col.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit(row.content)
        self.edit.setFrame(False)
        self.edit.setObjectName("taskEdit")
        self.edit.setToolTip("Click to edit, Enter to save, Esc to cancel")
        self.edit.setCursorPosition(0)
        self.edit.setProperty("done", row.checked)
        self.edit.setProperty("pending", row.pending)
        f = self.edit.font()
        f.setStrikeOut(row.checked)
        self.edit.setFont(f)
        self.edit.editingFinished.connect(self._save)
        self.edit.installEventFilter(self)
        col.addWidget(self.edit)
        meta = QHBoxLayout()
        meta.setContentsMargins(1, 0, 0, 0)
        meta.setSpacing(8)
        if row.due_text:
            dues = DUE_COLOURS_DARK if w.app.theme_name() == "Dark" else DUE_COLOURS
            colour = dues.get(row.due_state, "#808080")
            chip = QWidget()
            cl = QHBoxLayout(chip)
            cl.setContentsMargins(0, 0, 0, 0)
            cl.setSpacing(3)
            ic = QLabel()
            ic.setPixmap(calendar_pixmap(colour))
            txt = QLabel(row.due_text + ("  ↻" if row.recurring else ""))
            txt.setObjectName("dueText")
            txt.setStyleSheet(f"color:{colour}; font-size:8pt;")
            cl.addWidget(ic)
            cl.addWidget(txt)
            chip.setToolTip("Recurring task" if row.recurring else "Due date")
            meta.addWidget(chip)
        if row.priority > 1:
            pr = QLabel(f"P{5 - row.priority}")
            pr.setStyleSheet(f"color:{PRIORITY_COLOURS[row.priority]}; font-size:8pt; font-weight:700;")
            pr.setToolTip(f"Priority {5 - row.priority}")
            meta.addWidget(pr)
        if row.pending:
            pend = QLabel("⟳ not synced yet")
            pend.setObjectName("pendingLbl")
            meta.addWidget(pend)
        meta.addStretch(1)
        if meta.count() > 1:
            col.addLayout(meta)
        lay.addLayout(col, 1)

        self.del_btn = QToolButton()
        self.del_btn.setText("✕")
        self.del_btn.setObjectName("rowDelete")
        self.del_btn.setToolTip("Delete task")
        self.del_btn.setCursor(Qt.PointingHandCursor)
        sp = self.del_btn.sizePolicy()
        sp.setRetainSizeWhenHidden(True)
        self.del_btn.setSizePolicy(sp)
        self.del_btn.hide()
        self.del_btn.clicked.connect(self._delete)
        lay.addWidget(self.del_btn, 0, Qt.AlignTop)

    def enterEvent(self, ev):
        self.del_btn.show()
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        self.del_btn.hide()
        super().leaveEvent(ev)

    def eventFilter(self, obj, ev):
        if obj is self.edit and ev.type() == ev.Type.KeyPress and ev.key() == Qt.Key_Escape:
            self.edit.setText(self.row.content)
            self.edit.clearFocus()
            return True
        return False

    def _toggled(self, value: bool) -> None:
        self.w.touched()
        self.w.app.submit({"op": "complete" if value else "uncomplete", "id": self.row.id})

    def _save(self) -> None:
        text = self.edit.text().strip()
        if text and text != self.row.content:
            self.w.touched()
            self.row.content = text
            self.w.app.submit({"op": "update", "id": self.row.id, "content": text})
        elif not text:
            self.edit.setText(self.row.content)
        self.edit.setCursorPosition(0)

    def _delete(self) -> None:
        r = QMessageBox.question(self, APP_TITLE, f"Delete this task (and its sub-tasks) in Todoist?\n\n"
                                 f"“{self.row.content}”")
        if r == QMessageBox.Yes:
            self.w.touched()
            self.w.app.submit({"op": "delete", "id": self.row.id})
            self.hide()


class SectionHeader(QLabel):
    """Small-caps grey section title with a line under it."""

    def __init__(self, row: ts.RowData):
        super().__init__(row.content)
        self.setObjectName("section")
        f = self.font()
        f.setCapitalization(QFont.SmallCaps)
        f.setLetterSpacing(QFont.PercentageSpacing, 106)
        self.setFont(f)


# --------------------------------------------------------------------------
# Widget (one per project / section / smart view): a rounded "task card"
# --------------------------------------------------------------------------
class ViewWidget(QWidget):
    def __init__(self, app: "TodoistWidgetsApp", view: ts.ViewData, index: int = 0):
        super().__init__(None)
        self.app = app
        self.key = view.key
        self.view = view
        self.lay = app.layout_store.widget(view.key)
        self.lay.setdefault("on_top", True)
        self.lay.setdefault("visible", True)
        self._last_edit = 0.0
        self._sig = None
        self._deferred: ts.ViewData | None = None

        flags = Qt.FramelessWindowHint | Qt.Tool
        if self.lay["on_top"]:
            flags |= Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMinimumSize(220 + 2 * SHADOW_MARGIN, 150 + 2 * SHADOW_MARGIN)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(SHADOW_MARGIN, SHADOW_MARGIN - 4, SHADOW_MARGIN, SHADOW_MARGIN + 4)
        self.card = QFrame()
        self.card.setObjectName("card")
        self.shadow = QGraphicsDropShadowEffect(self.card)
        self.shadow.setBlurRadius(22)
        self.shadow.setOffset(0, 3)
        self.card.setGraphicsEffect(self.shadow)
        outer.addWidget(self.card)

        cardl = QHBoxLayout(self.card)
        cardl.setContentsMargins(0, 0, 0, 0)
        cardl.setSpacing(0)
        self.accent = QFrame()
        self.accent.setObjectName("accent")
        self.accent.setFixedWidth(5)
        cardl.addWidget(self.accent)
        content = QVBoxLayout()
        content.setContentsMargins(12, 8, 8, 4)
        content.setSpacing(4)
        cardl.addLayout(content, 1)

        self.header = HeaderBar(self)
        hl = QHBoxLayout(self.header)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(6)
        self.title_lbl = QLabel()
        self.title_lbl.setObjectName("title")
        self.count_lbl = QLabel("0")
        self.count_lbl.setObjectName("pill")
        self.count_lbl.setToolTip("Open tasks")
        self.count_lbl.setFixedHeight(18)
        self.count_lbl.setMinimumWidth(22)
        self.count_lbl.setAlignment(Qt.AlignCenter)
        self.add_btn = self._hbtn("+", "Add task")
        self.add_btn.clicked.connect(lambda: (self.add_edit.setFocus(), self.scroll_to_bottom()))
        self.menu_btn = self._hbtn("⋯", "Menu")
        self.menu_btn.clicked.connect(self.show_menu)
        self.close_btn = self._hbtn("✕", "Hide widget (nothing is deleted)")
        self.close_btn.clicked.connect(self.hide_widget)
        hl.addWidget(self.title_lbl)
        hl.addWidget(self.count_lbl, 0, Qt.AlignVCenter)
        hl.addStretch(1)
        hl.addWidget(self.add_btn)
        hl.addWidget(self.menu_btn)
        hl.addWidget(self.close_btn)
        content.addWidget(self.header)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setObjectName("scroll")
        inner = QWidget()
        inner.setObjectName("inner")
        self.rows_layout = QVBoxLayout(inner)
        self.rows_layout.setContentsMargins(0, 0, 4, 0)
        self.rows_layout.setSpacing(0)
        addrow = QWidget()
        addrow.setObjectName("addRow")
        al = QHBoxLayout(addrow)
        al.setContentsMargins(0, 6, 0, 2)
        al.setSpacing(8)
        self.add_plus = QLabel("+")
        self.add_plus.setObjectName("addPlus")
        self.add_plus.setFixedWidth(20)
        self.add_plus.setAlignment(Qt.AlignCenter)
        self.add_edit = QLineEdit()
        self.add_edit.setFrame(False)
        self.add_edit.setObjectName("addTask")
        self.add_edit.setToolTip("Quick Add: e.g. 'Call Anna tomorrow 10am p1 #Work @phone'")
        self.add_edit.returnPressed.connect(self._quick_add)
        al.addWidget(self.add_plus)
        al.addWidget(self.add_edit, 1)
        self.rows_layout.addWidget(addrow)
        self.rows_layout.addStretch(1)
        self.scroll.setWidget(inner)
        content.addWidget(self.scroll, 1)

        foot = QHBoxLayout()
        foot.setContentsMargins(0, 0, 0, 0)
        self.status_lbl = QLabel("")
        self.status_lbl.setObjectName("foot")
        foot.addWidget(self.status_lbl)
        foot.addStretch(1)
        self.grip = QSizeGrip(self.card)
        self.grip.setFixedSize(14, 14)
        foot.addWidget(self.grip, 0, Qt.AlignBottom | Qt.AlignRight)
        content.addLayout(foot)

        self.row_widgets: list[QWidget] = []
        self.apply_style()
        self.update_from(view, force=True)
        self._place(index)

    def _hbtn(self, text, tip):
        b = QToolButton()
        b.setText(text)
        b.setToolTip(tip)
        b.setObjectName("hbtn")
        b.setCursor(Qt.PointingHandCursor)
        b.setAutoRaise(True)
        return b

    def _place(self, index: int) -> None:
        g = self.lay.get("geom")
        if g and len(g) == 4:
            self.resize(max(self.minimumWidth(), g[2]), max(self.minimumHeight(), g[3]))
            self.move(g[0], g[1])
            if not any(s.availableGeometry().intersects(self.frameGeometry())
                       for s in QGuiApplication.screens()):
                g = None
        if not g:
            scr = QGuiApplication.primaryScreen().availableGeometry()
            per_col = max(1, (scr.height() - 20) // DEFAULT_H)
            col, row = divmod(index, per_col)
            self.resize(DEFAULT_W, DEFAULT_H)
            self.move(max(scr.left(), scr.right() - (col + 1) * DEFAULT_W),
                      scr.top() + 4 + row * DEFAULT_H)
        self._remember_geometry()

    # -- appearance -------------------------------------------------------
    def accent_colour(self) -> str:
        override = self.lay.get("color")
        if override:
            override = LEGACY_COLOUR_NAMES.get(override, override)
            if override in TODOIST_COLOURS:
                return accent_for(override)
        return accent_for(self.view.color)

    def apply_style(self) -> None:
        t = THEMES.get(self.app.theme_name(), THEMES["Light"])
        acc = self.accent_colour()
        self.shadow.setColor(QColor(0, 0, 0, t["shadow"]))
        self.setStyleSheet(f"""
            QFrame#card {{ background:{t['card']}; border:1px solid {t['border']}; border-radius:10px; }}
            QFrame#accent {{ background:{acc}; border:none; border-top-left-radius:10px;
                border-bottom-left-radius:10px; }}
            QLabel#title {{ color:{t['text']}; font-weight:700; font-size:11pt; }}
            QLabel#pill {{ color:{t['sub']}; background:{t['pill']}; border-radius:9px;
                padding:0px 6px; font-size:8pt; font-weight:600; }}
            QToolButton#hbtn {{ color:{t['sub']}; font-size:14px; border:none; padding:1px 5px;
                border-radius:5px; }}
            QToolButton#hbtn:hover {{ background:{t['hover']}; color:{TODOIST_RED}; }}
            QWidget#taskRow {{ border-bottom:1px solid {t['sep']}; background:transparent; }}
            QWidget#taskRow:hover {{ background:{t['hover']}; }}
            QLabel#section {{ color:{t['sub']}; font-weight:700; font-size:8.5pt; padding-top:10px;
                padding-bottom:3px; border-bottom:1px solid {t['sep']}; }}
            QLineEdit {{ background:transparent; color:{t['text']}; border:none; border-radius:4px;
                selection-background-color:{TODOIST_RED}; selection-color:white; font-size:10pt;
                padding:0px 2px; }}
            QLineEdit:focus {{ background:{t['edit']}; border-bottom:1px solid {TODOIST_RED}; }}
            QLineEdit[done="true"] {{ color:{t['sub']}; }}
            QLineEdit[pending="true"] {{ font-style:italic; }}
            QLabel#addPlus {{ color:{TODOIST_RED}; font-size:15pt; font-weight:400; }}
            QLineEdit#addTask {{ color:{t['text']}; }}
            QLabel#pendingLbl, QLabel#foot {{ color:{t['sub']}; font-size:8pt; }}
            #scroll, #inner, #addRow {{ background:transparent; }}
            QToolButton#rowDelete {{ color:{t['sub']}; border:none; font-size:10px; }}
            QToolButton#rowDelete:hover {{ color:{TODOIST_RED}; }}
            QScrollBar:vertical {{ width:6px; background:transparent; }}
            QScrollBar::handle:vertical {{ background:{t['sep']}; border-radius:3px; min-height:20px; }}
            QScrollBar::add-line, QScrollBar::sub-line {{ height:0; }}
            QSizeGrip {{ background:transparent; }}
        """)
        pal = self.add_edit.palette()
        pal.setColor(QPalette.PlaceholderText, QColor(TODOIST_RED if t is THEMES["Light"] else "#E8796F"))
        self.add_edit.setPalette(pal)

    def scroll_to_bottom(self):
        QTimer.singleShot(0, lambda: self.scroll.verticalScrollBar().setValue(
            self.scroll.verticalScrollBar().maximum()))

    # -- data ---------------------------------------------------------------
    def touched(self) -> None:
        self._last_edit = time.monotonic()

    def _editing(self) -> bool:
        return any(isinstance(r, TaskRow) and r.edit.hasFocus() for r in self.row_widgets)

    def update_from(self, view: ts.ViewData, force: bool = False) -> None:
        colour_changed = view.color != self.view.color
        self.view = view
        self.setWindowTitle(view.title)
        self.title_lbl.setText(view.title)
        self.count_lbl.setText(str(view.open_count))
        if view.kind == "label":
            hint = f"Add task (@{view.label})"
        elif view.kind in ("today", "overdue"):
            hint = "Add task (Inbox; type 'today' for a date)"
        else:
            hint = "Add task"
        self.add_edit.setPlaceholderText(hint)
        if colour_changed and not self.lay.get("color"):
            self.apply_style()
        sig = tuple((r.kind, r.id, r.content, r.checked, r.depth, r.priority, r.due_text,
                     r.recurring, r.pending) for r in view.rows)
        if sig == self._sig and not force:
            return
        if not force and self._editing():
            self._deferred = view  # don't rebuild under the user's cursor
            QTimer.singleShot(1500, self._apply_deferred)
            return
        self._sig = sig
        for rw in self.row_widgets:
            rw.setParent(None)
            rw.deleteLater()
        self.row_widgets = []
        for idx, r in enumerate(view.rows):
            rw = SectionHeader(r) if r.kind == "header" else TaskRow(self, r)
            self.rows_layout.insertWidget(idx, rw)
            self.row_widgets.append(rw)
        if not view.rows:
            empty = QLabel("All done 🎉" if view.kind != "label" else "No tasks")
            empty.setObjectName("foot")
            empty.setContentsMargins(2, 8, 0, 4)
            self.rows_layout.insertWidget(0, empty)
            self.row_widgets.append(empty)

    def _apply_deferred(self) -> None:
        if self._deferred is not None:
            v, self._deferred = self._deferred, None
            self.update_from(v)

    def _quick_add(self) -> None:
        text = self.add_edit.text().strip()
        if not text:
            return
        v = self.view
        if v.kind == "label" and f"@{v.label}" not in text:
            text += f" @{v.label}"
        self.touched()
        self.app.submit({"op": "quick_add", "text": text, "project_id": v.project_id,
                         "section_id": v.section_id})
        self.add_edit.clear()
        self.scroll_to_bottom()

    # -- menu -----------------------------------------------------------------
    def show_menu(self) -> None:
        m = QMenu(self)
        colours = m.addMenu("Accent colour")
        auto = colours.addAction("Todoist project colour")
        auto.setCheckable(True)
        auto.setChecked(not self.lay.get("color"))
        auto.triggered.connect(lambda: self.set_colour(None))
        colours.addSeparator()
        current = LEGACY_COLOUR_NAMES.get(self.lay.get("color"), self.lay.get("color"))
        for name, hexc in TODOIST_COLOURS.items():
            pm = QPixmap(14, 14)
            pm.fill(QColor(hexc))
            a = colours.addAction(QIcon(pm), name.replace("_", " ").capitalize())
            a.setCheckable(True)
            a.setChecked(current == name)
            a.triggered.connect(lambda _=False, n=name: self.set_colour(n))
        if self.view.project_id and self.view.kind in ("project", "section"):
            sc = m.addAction("Show completed tasks")
            sc.setCheckable(True)
            sc.setChecked(self.key in self.app.layout_store.settings.get("show_completed", []))
            sc.triggered.connect(self.set_show_completed)
        top = m.addAction("Always on top")
        top.setCheckable(True)
        top.setChecked(self.lay["on_top"])
        top.triggered.connect(self.set_on_top)
        m.addAction("Hide this widget", self.hide_widget)
        m.addSeparator()
        m.addAction("Sync now", self.app.worker.sync_now)
        m.addAction("Open in Todoist", self.open_in_todoist)
        m.exec(self.menu_btn.mapToGlobal(QPoint(0, self.menu_btn.height())))

    def open_in_todoist(self) -> None:
        v = self.view
        if v.kind in ("today", "overdue"):
            url = "https://app.todoist.com/app/today"
        elif v.kind == "label":
            url = f"https://app.todoist.com/app/label/{v.label}"
        else:
            url = f"https://app.todoist.com/app/project/{v.project_id}"
        QDesktopServices.openUrl(QUrl(url))

    def set_colour(self, name) -> None:
        if name:
            self.lay["color"] = name
        else:
            self.lay.pop("color", None)
        self.app.layout_store.save_soon()
        self.apply_style()

    def set_show_completed(self, on: bool) -> None:
        s = self.app.layout_store.settings
        lst = set(s.get("show_completed", []))
        (lst.add if on else lst.discard)(self.key)
        s["show_completed"] = sorted(lst)
        self.app.layout_store.save_soon()
        self.app.apply_show_completed()
        self.app.worker.sync_now()

    def set_on_top(self, on: bool) -> None:
        self.lay["on_top"] = bool(on)
        self.app.layout_store.save_soon()
        self.setWindowFlag(Qt.WindowStaysOnTopHint, bool(on))
        self.show()

    def hide_widget(self) -> None:
        self.lay["visible"] = False
        self.app.layout_store.save_soon()
        self.hide()

    def _remember_geometry(self) -> None:
        g = self.geometry()
        self.lay["geom"] = [g.x(), g.y(), g.width(), g.height()]
        self.app.layout_store.save_soon()

    def moveEvent(self, ev):
        super().moveEvent(ev)
        if self.isVisible():
            self._remember_geometry()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if self.isVisible():
            self._remember_geometry()
# --------------------------------------------------------------------------
# Setup dialog
# --------------------------------------------------------------------------
SETUP_STEPS = f"""
<p>Todoist Widgets uses the <b>official Todoist API</b> with your personal API token.
The token is stored only in <b>Windows Credential Manager</b>, never in a file.</p>
<ol>
<li>Click <b>Open Todoist token page</b>, or open
<a href="{TOKEN_URL}">Settings &rsaquo; Integrations &rsaquo; Developer</a> in Todoist.</li>
<li>Under <b>API token</b>, click <b>Copy API token</b>.</li>
<li>Paste it below and click <b>Connect</b>.</li>
</ol>
<p style="color:#666">Anyone with this token can read and change your Todoist, so treat it like a
password. If you ever need to, you can issue a new token on the same page.</p>
"""


class SetupDialog(QDialog):
    finished_signin = Signal(bool, str)

    def __init__(self, app: "TodoistWidgetsApp"):
        super().__init__()
        self.app = app
        self.setWindowTitle(f"{APP_TITLE} – Connect Todoist")
        self.setWindowIcon(app_icon())
        self.setMinimumWidth(520)
        v = QVBoxLayout(self)
        info = QLabel(SETUP_STEPS)
        info.setWordWrap(True)
        info.setOpenExternalLinks(True)
        info.setTextFormat(Qt.RichText)
        v.addWidget(info)
        open_btn = QPushButton("Open Todoist token page")
        open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(TOKEN_URL)))
        v.addWidget(open_btn, 0, Qt.AlignLeft)
        form = QFormLayout()
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.Password)
        self.token.setPlaceholderText("Paste your 40-character API token")
        form.addRow("API token:", self.token)
        v.addLayout(form)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        v.addWidget(self.status)
        bb = QDialogButtonBox()
        self.ok_btn = bb.addButton("Connect", QDialogButtonBox.AcceptRole)
        bb.addButton(QDialogButtonBox.Cancel)
        self.ok_btn.clicked.connect(self.connect_token)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.finished_signin.connect(self._done)

    def connect_token(self) -> None:
        value = ts.clean_token(self.token.text())
        if not ts.looks_like_token(value):
            self.status.setText("That does not look like a Todoist API token (40 letters and digits).")
            return
        self.ok_btn.setEnabled(False)
        self.status.setText("Checking the token with Todoist…")

        def work():
            try:
                user = self.app.engine.set_token(value)
                self.finished_signin.emit(True, user.get("full_name") or user.get("email") or "")
            except Exception as exc:  # noqa: BLE001
                log.warning("Token check failed: %s", type(exc).__name__)
                self.finished_signin.emit(False, ts._friendly_error(exc))

        threading.Thread(target=work, daemon=True).start()

    def _done(self, ok: bool, msg: str) -> None:
        self.ok_btn.setEnabled(True)
        if ok:
            self.token.clear()
            self.app.worker.sync_now()
            self.app.tray.showMessage(APP_TITLE, f"Connected to Todoist{(' as ' + msg) if msg else ''}.",
                                      QSystemTrayIcon.Information, 4000)
            self.accept()
        else:
            self.status.setText(f"<span style='color:#B3261E'>Could not connect: {msg}</span>")


# --------------------------------------------------------------------------
# Settings dialog
# --------------------------------------------------------------------------
class SettingsDialog(QDialog):
    def __init__(self, app: "TodoistWidgetsApp"):
        super().__init__()
        self.app = app
        s = app.layout_store.settings
        self.setWindowTitle(f"{APP_TITLE} – Settings")
        self.setWindowIcon(app_icon())
        v = QVBoxLayout(self)

        g1 = QGroupBox("Widgets")
        g1l = QVBoxLayout(g1)
        self.rb_project = QRadioButton("One widget per project (sections shown as headings)")
        self.rb_section = QRadioButton("One widget per section")
        (self.rb_section if s.get("mode") == "section" else self.rb_project).setChecked(True)
        grp = QButtonGroup(self)
        grp.addButton(self.rb_project)
        grp.addButton(self.rb_section)
        g1l.addWidget(self.rb_project)
        g1l.addWidget(self.rb_section)
        self.cb_empty = QCheckBox("Hide projects without open tasks")
        self.cb_empty.setChecked(bool(s.get("hide_empty")))
        g1l.addWidget(self.cb_empty)
        v.addWidget(g1)

        g2 = QGroupBox("Extra views (each gets its own widget)")
        g2l = QFormLayout(g2)
        self.cb_today = QCheckBox("Today (includes overdue)")
        self.cb_today.setChecked(bool(s.get("view_today")))
        self.cb_overdue = QCheckBox("Overdue")
        self.cb_overdue.setChecked(bool(s.get("view_overdue")))
        g2l.addRow(self.cb_today)
        g2l.addRow(self.cb_overdue)
        self.labels = QLineEdit(", ".join(s.get("labels", [])))
        self.labels.setPlaceholderText("e.g. errand, phone")
        avail = ", ".join(app.labels) if app.labels else "none yet"
        self.labels.setToolTip(f"Your labels: {avail}")
        g2l.addRow("Label widgets:", self.labels)
        v.addWidget(g2)

        form = QFormLayout()
        self.theme = QComboBox()
        self.theme.addItems(list(THEMES))
        self.theme.setCurrentText(app.theme_name())
        form.addRow("Theme:", self.theme)
        self.interval = QSpinBox()
        self.interval.setRange(30, 600)
        self.interval.setSuffix(" s")
        self.interval.setValue(int(s.get("interval", 45)))
        form.addRow("Background sync every:", self.interval)
        self.account = QLabel(app.status_info.get("user") or ("connected" if app.engine.creds.get() else "not connected"))
        form.addRow("Todoist account:", self.account)
        v.addLayout(form)
        self.hotkey_group = hk.HotkeyGroup(s.get("hotkeys") or {},
                                           "Click a field and press a combination (default toggle: "
                                           "Ctrl+Alt+T). Leave empty for no shortcut.")
        v.addWidget(self.hotkey_group)
        acc = QHBoxLayout()
        b1 = QPushButton("Change API token…")
        b1.clicked.connect(lambda: (self.accept(), app.show_setup()))
        b2 = QPushButton("Sign out")
        b2.clicked.connect(self._sign_out)
        b3 = QPushButton("Open data folder")
        b3.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(app.engine.dir))))
        for b in (b1, b2, b3):
            acc.addWidget(b)
        v.addLayout(acc)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _sign_out(self) -> None:
        if QMessageBox.question(self, APP_TITLE, "Remove the saved Todoist token from this PC?\n"
                                "Cached tasks stay visible until you connect again.") == QMessageBox.Yes:
            self.app.engine.sign_out()
            self.account.setText("not connected")
            self.app.update_status()

    def _save(self) -> None:
        s = self.app.layout_store.settings
        s["mode"] = "section" if self.rb_section.isChecked() else "project"
        s["hide_empty"] = self.cb_empty.isChecked()
        s["view_today"] = self.cb_today.isChecked()
        s["view_overdue"] = self.cb_overdue.isChecked()
        s["labels"] = [x.strip().lstrip("@") for x in self.labels.text().split(",") if x.strip()]
        s["interval"] = self.interval.value()
        s["theme"] = self.theme.currentText()
        self.app.apply_theme()
        self.app.worker.interval = float(self.interval.value())
        s["hotkeys"] = self.hotkey_group.values()
        self.app.layout_store.save()
        self.app.worker.refresh()
        errors = self.app.apply_hotkeys(notify=False)
        if errors:
            QMessageBox.warning(self, APP_TITLE, "These global hotkeys could not be registered:\n\n"
                                + hk.describe_errors(errors) +
                                "\n\nThe other settings are saved. Choose a different combination "
                                "(or clear the field) and press OK again.")
            return
        self.accept()


# --------------------------------------------------------------------------
# App + tray
# --------------------------------------------------------------------------
class Bridge(QObject):
    event = Signal(str, object)


class TodoistWidgetsApp(QObject):
    def __init__(self, qapp: QApplication, engine: ts.TodoistEngine, demo: bool = False):
        super().__init__()
        self.qapp = qapp
        self.engine = engine
        self.demo = demo
        self.layout_store = LayoutStore(engine.dir / "layout.json")
        self.widgets: dict[str, ViewWidget] = {}
        self.views: list[ts.ViewData] = []
        self.labels: list[str] = []
        self.status_info: dict = {}
        self.bridge = Bridge()
        self.bridge.event.connect(self.on_event)
        self.worker = ts.SyncWorker(engine, self.bridge.event.emit, settings_fn=self.settings_copy,
                                    interval=self.layout_store.settings.get("interval", 45))
        self._setup_dlg = None
        self.apply_show_completed()
        self._build_tray()
        self.hotkeys = hk.HotkeyManager(self)
        self.hotkeys.triggered.connect(self.on_hotkey)

    # -- global hotkeys -------------------------------------------------------
    def apply_hotkeys(self, notify: bool = True) -> list:
        errors = self.hotkeys.apply(self.layout_store.settings.get("hotkeys") or {})
        for action, text in self.hotkeys.active.items():
            log.info("Global hotkey %s = %s", action, text)
        for action, text, reason in errors:
            log.warning("Global hotkey %s = %s not registered: %s", action, text, reason)
        if errors and notify:
            self.tray.showMessage(APP_TITLE, "Some global hotkeys could not be registered (see Settings):\n"
                                  + hk.describe_errors(errors), QSystemTrayIcon.Warning, 8000)
        return errors

    def on_hotkey(self, action: str) -> None:
        {"toggle": self.toggle_all, "show": self.show_all, "hide": self.hide_all}.get(action, lambda: None)()

    def toggle_all(self) -> None:
        if any(w.isVisible() for w in self.widgets.values()):
            self.hide_all()
        else:
            self.show_all()

    def theme_name(self) -> str:
        name = self.layout_store.settings.get("theme", "Light")
        return name if name in THEMES else "Light"

    def apply_theme(self) -> None:
        for w in self.widgets.values():
            w.apply_style()
            w.update_from(w.view, force=True)  # due-date colours depend on the theme

    def settings_copy(self) -> dict:
        return dict(self.layout_store.settings)

    def apply_show_completed(self) -> None:
        keys = set(self.layout_store.settings.get("show_completed", []))
        pids = {k.split(":", 1)[1] for k in keys if k.startswith("project:")}
        # section widgets use their project's completed list
        for k in keys:
            if k.startswith("section:"):
                for v in self.views:
                    if v.key == k and v.project_id:
                        pids.add(v.project_id)
        self.engine.show_completed = pids

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(app_icon(), self)
        self.tray.setToolTip(APP_TITLE)
        m = QMenu()
        self.status_action = m.addAction("Starting…")
        self.status_action.setEnabled(False)
        m.addSeparator()
        m.addAction("Sync now", self.worker.sync_now)
        m.addAction("Show all widgets", self.show_all)
        m.addAction("Hide all widgets", self.hide_all)
        m.addAction("Toggle all widgets", self.toggle_all)
        m.addSeparator()
        m.addAction("Quick add task…", self.quick_add_dialog)
        m.addAction("Settings…", self.show_settings)
        self.autostart_action = m.addAction("Start with Windows")
        self.autostart_action.setCheckable(True)
        lnk = startup_shortcut_path()
        self.autostart_action.setEnabled(lnk is not None)
        self.autostart_action.setChecked(bool(lnk and lnk.exists()))
        self.autostart_action.toggled.connect(self.set_autostart)
        m.addSeparator()
        m.addAction("Quit", self.quit)
        self.tray_menu = m
        self.tray.setContextMenu(m)
        self.tray.activated.connect(
            lambda r: self.show_all() if r in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick) else None)
        self.tray.show()

    def update_status(self) -> None:
        st = self.status_info
        status = self.engine.status
        pending = st.get("pending", 0)
        if self.demo:
            text, paused = "Demo mode – not synced", False
        elif status == "ok":
            ls = st.get("last_sync")
            text = f"Synced {ls:%H:%M}" if isinstance(ls, _dt.datetime) else "Connected"
            if st.get("user"):
                text += f" · {st['user']}"
            paused = False
        elif status == "paused":
            text = f"Sync paused: {self.engine.last_error or 'unknown error'} – showing cached tasks"
            paused = True
        else:
            text, paused = "Not connected – open Settings to paste your API token", True
        if pending and not self.demo:
            text += f" · {pending} change(s) waiting"
        self.status_action.setText(text)
        self.tray.setIcon(app_icon(paused))
        self.tray.setToolTip(f"{APP_TITLE}\n{text}")
        if paused:
            foot = "sync paused" if status == "paused" else "not connected"
            if pending:
                foot += f" · {pending} waiting"
        else:
            foot = ""
        for w in self.widgets.values():
            w.status_lbl.setText(foot)

    def on_event(self, kind: str, payload: dict) -> None:
        self.views = payload["views"]
        self.labels = payload.get("labels", [])
        self.status_info = payload
        self.refresh_widgets()
        if kind == "synced":
            res = payload.get("result") or {}
            if res.get("conflicts"):
                self.tray.showMessage(APP_TITLE, f"{len(res['conflicts'])} task(s) were changed in Todoist "
                                      "and here at the same time; Todoist's version was kept.",
                                      QSystemTrayIcon.Warning, 6000)
        self.update_status()

    def refresh_widgets(self) -> None:
        s = self.layout_store.settings
        views = [v for v in self.views
                 if not (s.get("hide_empty") and v.kind in ("project", "section") and v.open_count == 0)]
        wanted = {v.key for v in views}
        for key in list(self.widgets):
            if key not in wanted:
                w = self.widgets.pop(key)
                w.close()
                w.deleteLater()
        for v in views:
            w = self.widgets.get(v.key)
            if w is None:
                w = ViewWidget(self, v, index=len(self.widgets))
                self.widgets[v.key] = w
                if w.lay.get("visible", True):
                    w.show()
            else:
                w.update_from(v)
        self.layout_store.save_soon()

    def submit(self, op: dict) -> None:
        self.worker.submit(op)

    def quick_add_dialog(self) -> None:
        text, ok = QInputDialog.getText(None, f"{APP_TITLE} – Quick add",
                                        "Task (Quick Add syntax, e.g. 'Pay rent tomorrow p1 #Home'):")
        if ok and text.strip():
            self.submit({"op": "quick_add", "text": text.strip()})

    def show_all(self) -> None:
        for w in self.widgets.values():
            w.lay["visible"] = True
            w.show()
            w.raise_()
        self.layout_store.save_soon()

    def hide_all(self) -> None:
        for w in self.widgets.values():
            w.lay["visible"] = False
            w.hide()
        self.layout_store.save_soon()

    def show_settings(self) -> None:
        SettingsDialog(self).exec()

    def show_setup(self) -> None:
        if self._setup_dlg is None:
            self._setup_dlg = SetupDialog(self)
        self._setup_dlg.show()
        self._setup_dlg.raise_()
        self._setup_dlg.activateWindow()

    def set_autostart(self, on: bool) -> None:
        lnk = startup_shortcut_path()
        if lnk is None:
            return
        try:
            if on:
                create_shortcut(lnk, pythonw_executable(), f'"{APP_DIR / "todoist_widgets.py"}"',
                                str(APP_DIR), str(APP_DIR / "icon.ico"))
            elif lnk.exists():
                lnk.unlink()
            log.info("Start with Windows %s", "enabled" if on else "disabled")
        except Exception as exc:  # noqa: BLE001
            log.exception("Could not change startup shortcut")
            QMessageBox.warning(None, APP_TITLE, f"Could not change the startup shortcut:\n{exc}")
            self.autostart_action.blockSignals(True)
            self.autostart_action.setChecked(lnk.exists())
            self.autostart_action.blockSignals(False)

    def start(self) -> None:
        self.apply_hotkeys()
        self.worker.start()
        if not self.demo and self.engine.creds.get() is None:
            QTimer.singleShot(400, self.show_setup)

    def quit(self) -> None:
        self.hotkeys.unregister_all()
        self.layout_store.save()
        self.worker.stop()
        self.worker.join(timeout=5)
        self.tray.hide()
        self.qapp.quit()


# --------------------------------------------------------------------------
# Demo data (also used by the screenshot/smoke test)
# --------------------------------------------------------------------------
def build_demo_engine(directory: Path, today: _dt.date | None = None) -> ts.TodoistEngine:
    today = today or _dt.date.today()
    d = lambda n: (today + _dt.timedelta(days=n)).isoformat()  # noqa: E731
    eng = ts.TodoistEngine(creds=ts.MemoryCredentialStore(), directory=directory, offline_only=True,
                           today_fn=lambda: today)
    eng.cache_path = directory / "demo-state.json"
    eng.user = {"id": "demo", "full_name": "Demo", "inbox_project_id": "inbox"}
    eng.projects = {
        "inbox": {"id": "inbox", "name": "Inbox", "color": "charcoal", "child_order": 0, "inbox_project": True},
        "work": {"id": "work", "name": "Work", "color": "blue", "child_order": 1},
        "home": {"id": "home", "name": "Home", "color": "lime_green", "child_order": 2},
    }
    eng.sections = {"s-meet": {"id": "s-meet", "project_id": "work", "name": "Meetings", "section_order": 1}}
    items = [
        ("i1", "inbox", None, None, "Reply to Anna's email", 1, d(0), False),
        ("i2", "inbox", None, None, "Water the plants", 1, d(0), True),
        ("i3", "inbox", None, None, "Renew passport", 3, d(-2), False),
        ("i4", "inbox", None, None, "Pick up dry cleaning", 1, None, False),
        ("w1", "work", None, None, "Finish Q4 report", 4, d(-1), False),
        ("w2", "work", None, "w1", "Collect sales numbers", 1, None, False),
        ("w3", "work", None, "w1", "Draft summary", 1, d(1), False),
        ("w4", "work", None, None, "Review pull requests", 2, d(0), False),
        ("w5", "work", "s-meet", None, "Prepare weekly sync agenda", 1, d(3), True),
        ("w6", "work", "s-meet", None, "Book room for offsite", 3, None, False),
        ("h1", "home", None, None, "Call the plumber", 4, d(0), False),
        ("h2", "home", None, None, "Groceries", 1, d(1), False),
        ("h3", "home", None, "h2", "Milk", 1, None, False),
        ("h4", "home", None, "h2", "Coffee beans", 1, None, False),
        ("h5", "home", None, None, "Take out the bins", 1, d(2), True),
        ("h6", "home", None, None, "Plan weekend trip", 2, d(9), False),
    ]
    for n, (iid, pid, sid, parent, content, prio, due, rec) in enumerate(items):
        eng.items[iid] = {"id": iid, "project_id": pid, "section_id": sid, "parent_id": parent,
                          "content": content, "priority": prio, "child_order": n,
                          "due": {"date": due, "is_recurring": rec, "string": "every day" if rec else ""} if due else None,
                          "checked": False, "is_deleted": False, "labels": []}
    eng.sync_token = "demo"
    return eng


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=APP_TITLE)
    ap.add_argument("--demo", action="store_true", help="show sample projects without signing in")
    ap.add_argument("--data-dir", help="override the data folder (default %%APPDATA%%\\TodoistWidgets)")
    args = ap.parse_args(argv)
    if args.data_dir:
        os.environ["TDW_DATA_DIR"] = args.data_dir
    directory = ts.data_dir()
    if args.demo:
        directory = directory / "demo"
        directory.mkdir(exist_ok=True)
    setup_logging(directory)

    QApplication.setQuitOnLastWindowClosed(False)
    qapp = QApplication(sys.argv[:1])
    qapp.setApplicationName(APP_TITLE)
    qapp.setWindowIcon(app_icon())
    qapp.setFont(QFont("Segoe UI", 10))

    lock = QLockFile(str(directory / "app.lock"))
    if not lock.tryLock(200):
        QMessageBox.information(None, APP_TITLE, "Todoist Widgets is already running (see the tray icon).")
        return 0
    engine = build_demo_engine(directory) if args.demo else ts.TodoistEngine(directory=directory)
    app = TodoistWidgetsApp(qapp, engine, demo=args.demo)
    app.start()
    log.info("Todoist Widgets started (data folder: %s)", directory)
    rc = qapp.exec()
    lock.unlock()
    return rc


if __name__ == "__main__":
    sys.exit(main())
