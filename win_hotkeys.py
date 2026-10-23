"""System-wide hotkeys for Windows via Win32 RegisterHotKey (ctypes only, no extra packages).

Shared by KeepNotes Widgets and Todoist Widgets (each app ships its own copy of this file).

* parse_hotkey("Ctrl+Alt+K") -> (modifiers, virtual-key code), or ValueError with a readable reason
* HotkeyManager.apply({"toggle": "Ctrl+Alt+K", "show": "", "hide": ""}) registers the shortcuts on
  the GUI thread (hWnd = NULL, so Windows posts WM_HOTKEY to the thread's message queue) and returns
  a list of (action, shortcut, reason) for the ones that could not be registered.
* HotkeyManager.triggered(action) fires when a registered shortcut is pressed anywhere in Windows.
"""
from __future__ import annotations

import ctypes
import sys

from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication, QObject, Qt, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QFormLayout, QGroupBox, QKeySequenceEdit, QLabel

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY = 0x0312
ERROR_HOTKEY_ALREADY_REGISTERED = 1409
ACTIONS = (("toggle", "Toggle all (show / hide)"), ("show", "Show all"), ("hide", "Hide all"))
HOTKEY_ID_BASE = 0xB100


def _vk_table() -> dict:
    K = Qt.Key
    t = {}
    for c in range(ord("A"), ord("Z") + 1):
        t[getattr(K, f"Key_{chr(c)}")] = c
    for d in range(10):
        t[getattr(K, f"Key_{d}")] = 0x30 + d
    for n in range(1, 25):
        t[getattr(K, f"Key_F{n}")] = 0x6F + n
    # Shift+digit can arrive as the US shifted symbol
    for sym, d in (("Exclam", 1), ("At", 2), ("NumberSign", 3), ("Dollar", 4), ("Percent", 5),
                   ("AsciiCircum", 6), ("Ampersand", 7), ("Asterisk", 8), ("ParenLeft", 9),
                   ("ParenRight", 0)):
        t[getattr(K, f"Key_{sym}")] = 0x30 + d
    t.update({
        K.Key_Space: 0x20, K.Key_PageUp: 0x21, K.Key_PageDown: 0x22, K.Key_End: 0x23,
        K.Key_Home: 0x24, K.Key_Left: 0x25, K.Key_Up: 0x26, K.Key_Right: 0x27, K.Key_Down: 0x28,
        K.Key_Insert: 0x2D, K.Key_Delete: 0x2E, K.Key_Return: 0x0D, K.Key_Enter: 0x0D,
        K.Key_Pause: 0x13, K.Key_Semicolon: 0xBA, K.Key_Equal: 0xBB, K.Key_Plus: 0xBB,
        K.Key_Comma: 0xBC, K.Key_Minus: 0xBD, K.Key_Period: 0xBE, K.Key_Slash: 0xBF,
        K.Key_QuoteLeft: 0xC0, K.Key_BracketLeft: 0xDB, K.Key_Backslash: 0xDC,
        K.Key_BracketRight: 0xDD, K.Key_Apostrophe: 0xDE,
    })
    return t


_VK = _vk_table()


def normalise(text: str) -> str:
    """Canonical portable text ("ctrl+alt+k" -> "Ctrl+Alt+K"); '' for empty or invalid."""
    seq = QKeySequence.fromString((text or "").strip(), QKeySequence.PortableText)
    return seq.toString(QKeySequence.PortableText) if seq.count() == 1 else ""


def parse_hotkey(text: str) -> tuple[int, int]:
    seq = QKeySequence.fromString((text or "").strip(), QKeySequence.PortableText)
    if seq.count() != 1:
        raise ValueError("enter exactly one key combination")
    combo = seq[0]
    key, qmods = combo.key(), combo.keyboardModifiers()
    if key in (Qt.Key.Key_Control, Qt.Key.Key_Alt, Qt.Key.Key_Shift, Qt.Key.Key_Meta, Qt.Key.Key_unknown):
        raise ValueError("add a normal key (letter, digit or F-key) to the modifiers")
    vk = _VK.get(key)
    if vk is None:
        raise ValueError("this key cannot be used for a global shortcut")
    mods = 0
    if qmods & Qt.ControlModifier:
        mods |= MOD_CONTROL
    if qmods & Qt.AltModifier:
        mods |= MOD_ALT
    if qmods & Qt.ShiftModifier:
        mods |= MOD_SHIFT
    if qmods & Qt.MetaModifier:
        mods |= MOD_WIN
    count = bin(mods).count("1")
    is_f13_24 = 0x7C <= vk <= 0x87
    if not (count >= 2 or (mods & MOD_WIN) or is_f13_24):
        raise ValueError("use at least two modifiers (e.g. Ctrl+Alt+…) so it does not clash with "
                         "normal app shortcuts")
    if mods & MOD_CONTROL and mods & MOD_ALT and vk == 0x2E:
        raise ValueError("Ctrl+Alt+Delete is reserved by Windows")
    return mods, vk


class _Filter(QAbstractNativeEventFilter):
    def __init__(self, manager: "HotkeyManager"):
        super().__init__()
        self.manager = manager

    def nativeEventFilter(self, event_type, message):
        try:
            if bytes(event_type) == b"windows_generic_MSG":
                from ctypes import wintypes
                msg = wintypes.MSG.from_address(int(message))
                if msg.message == WM_HOTKEY and msg.wParam in self.manager._ids:
                    self.manager.triggered.emit(self.manager._ids[msg.wParam])
                    return True, 0
        except Exception:  # noqa: BLE001 - never break the event loop
            pass
        return False, 0


class HotkeyManager(QObject):
    triggered = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.supported = sys.platform == "win32"
        self._ids: dict[int, str] = {}
        self.active: dict[str, str] = {}
        self._filter = None
        if self.supported:
            from ctypes import wintypes
            self._user32 = ctypes.WinDLL("user32", use_last_error=True)
            self._user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
            self._user32.RegisterHotKey.restype = wintypes.BOOL
            self._user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
            self._user32.UnregisterHotKey.restype = wintypes.BOOL
            self._filter = _Filter(self)
            QCoreApplication.instance().installNativeEventFilter(self._filter)

    def apply(self, mapping: dict) -> list[tuple[str, str, str]]:
        """(Re)register all shortcuts. Returns [(action, shortcut, reason)] for failures."""
        self.unregister_all()
        errors: list[tuple[str, str, str]] = []
        seen: dict[tuple[int, int], str] = {}
        for n, (action, _label) in enumerate(ACTIONS):
            text = normalise(mapping.get(action, ""))
            if not text:
                continue
            try:
                key = parse_hotkey(text)
            except ValueError as exc:
                errors.append((action, text, str(exc)))
                continue
            if key in seen:
                errors.append((action, text, f"it is already used for '{label_of(seen[key])}'"))
                continue
            seen[key] = action
            if self.supported:
                hid = HOTKEY_ID_BASE + n
                if not self._user32.RegisterHotKey(None, hid, key[0] | MOD_NOREPEAT, key[1]):
                    err = ctypes.get_last_error()
                    reason = ("it is already in use by Windows or another program"
                              if err == ERROR_HOTKEY_ALREADY_REGISTERED else f"Windows error {err}")
                    errors.append((action, text, reason))
                    continue
                self._ids[hid] = action
            self.active[action] = text
        return errors

    def unregister_all(self) -> None:
        if self.supported:
            for hid in list(self._ids):
                self._user32.UnregisterHotKey(None, hid)
        self._ids.clear()
        self.active.clear()


def label_of(action: str) -> str:
    return dict(ACTIONS).get(action, action)


def describe_errors(errors) -> str:
    return "\n".join(f"• {label_of(a)} – {s}: {r}" for a, s, r in errors)


class HotkeyGroup(QGroupBox):
    """Settings section: one key-capture field per action (Backspace/clear button = no shortcut)."""

    def __init__(self, current: dict, note: str = "", parent=None):
        super().__init__("Global hotkeys (work in any program)", parent)
        form = QFormLayout(self)
        self.edits: dict[str, QKeySequenceEdit] = {}
        for action, label in ACTIONS:
            e = QKeySequenceEdit(QKeySequence.fromString(current.get(action, "") or "",
                                                         QKeySequence.PortableText))
            e.setMaximumSequenceLength(1)
            e.setClearButtonEnabled(True)
            e.setToolTip("Click, then press the key combination. Use the clear button for none.")
            self.edits[action] = e
            form.addRow(label + ":", e)
        hint = QLabel(note or "Click a field and press a combination such as Ctrl+Alt+K. "
                      "Leave empty for no shortcut.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#666; font-size:8pt;")
        form.addRow(hint)

    def values(self) -> dict:
        return {a: normalise(e.keySequence().toString(QKeySequence.PortableText))
                for a, e in self.edits.items()}
