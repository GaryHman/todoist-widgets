"""Tests for the global-hotkey helpers, the startup guard and single-instance relaunch."""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

import win_hotkeys as hk  # noqa: E402

APP_SCRIPT = next(p for p in ("todoist_widgets.py", "keep_widgets.py") if (ROOT / p).exists())
ENV_VAR = "TDW_DATA_DIR" if APP_SCRIPT.startswith("todoist") else "KNW_DATA_DIR"
LOG_NAME = "todoistwidgets.log" if APP_SCRIPT.startswith("todoist") else "keepnotes.log"


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_parse_valid(qapp):
    assert hk.parse_hotkey("Ctrl+Alt+K") == (hk.MOD_CONTROL | hk.MOD_ALT, ord("K"))
    assert hk.parse_hotkey("ctrl+alt+t") == (hk.MOD_CONTROL | hk.MOD_ALT, ord("T"))
    assert hk.parse_hotkey("Ctrl+Alt+Shift+F12") == (hk.MOD_CONTROL | hk.MOD_ALT | hk.MOD_SHIFT, 0x7B)
    assert hk.parse_hotkey("Meta+F9") == (hk.MOD_WIN, 0x78)
    assert hk.parse_hotkey("F13") == (0, 0x7C)
    assert hk.parse_hotkey("Ctrl+Shift+!") == (hk.MOD_CONTROL | hk.MOD_SHIFT, ord("1"))
    assert hk.normalise(" ctrl+alt+k ") == "Ctrl+Alt+K"
    assert hk.normalise("") == ""


@pytest.mark.parametrize("bad", ["", "Ctrl+K", "Alt+K", "K", "Ctrl+Alt+Del", "Ctrl+Alt+K, Ctrl+Alt+J"])
def test_parse_rejected(qapp, bad):
    with pytest.raises(ValueError):
        hk.parse_hotkey(bad)


def test_manager_conflicts(qapp):
    m = hk.HotkeyManager()
    errs = m.apply({"toggle": "Ctrl+Alt+K", "show": "ctrl+alt+k", "hide": "Ctrl+J"})
    assert [(a, s) for a, s, _ in errs] == [("show", "Ctrl+Alt+K"), ("hide", "Ctrl+J")]
    assert "already used for 'Toggle all" in errs[0][2]
    assert m.active == {"toggle": "Ctrl+Alt+K"}
    assert m.apply({"toggle": "", "show": "Ctrl+Alt+Shift+S", "hide": "Ctrl+Alt+Shift+H"}) == []
    assert m.active == {"show": "Ctrl+Alt+Shift+S", "hide": "Ctrl+Alt+Shift+H"}
    m.unregister_all()
    assert m.active == {}


def test_settings_group_roundtrip(qapp):
    g = hk.HotkeyGroup({"toggle": "Ctrl+Alt+K", "show": "", "hide": "Ctrl+Alt+Shift+H"})
    assert g.values() == {"toggle": "Ctrl+Alt+K", "show": "", "hide": "Ctrl+Alt+Shift+H"}
    g.edits["toggle"].clear()
    assert g.values()["toggle"] == ""


def test_missing_package_is_reported_not_silent(tmp_path):
    """Like an empty .venv: PySide6 cannot be imported -> log line + exit code 1 (not a silent exit)."""
    blocker = tmp_path / "block"
    blocker.mkdir()
    (blocker / "PySide6.py").write_text("raise ImportError('No module named PySide6 (simulated)')\n")
    env = dict(os.environ, PYTHONPATH=str(blocker), **{ENV_VAR: str(tmp_path / "data")})
    r = subprocess.run([sys.executable, str(ROOT / APP_SCRIPT)], env=env, capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 1
    assert "could not start" in r.stderr
    log = (tmp_path / "data" / LOG_NAME).read_text(encoding="utf-8")
    assert "ERROR Startup failed" in log and "PySide6" in log


def _wait_for(path: Path, text: str, count: int, timeout: float = 40.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if path.exists() and path.read_text(encoding="utf-8").count(text) >= count:
            return True
        time.sleep(0.2)
    return False


def test_relaunch_after_hard_kill_is_not_blocked_by_stale_lock(tmp_path):
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", **{ENV_VAR: str(tmp_path)})
    log = tmp_path / "demo" / LOG_NAME
    cmd = [sys.executable, str(ROOT / APP_SCRIPT), "--demo"]
    p1 = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert _wait_for(log, "started (data folder", 1), "first start not logged"
        assert (tmp_path / "demo" / "app.lock").exists()
    finally:
        p1.send_signal(signal.SIGKILL)
        p1.wait(timeout=10)
    p2 = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert _wait_for(log, "started (data folder", 2), "relaunch after kill was blocked"
    finally:
        p2.terminate()
        p2.wait(timeout=10)
