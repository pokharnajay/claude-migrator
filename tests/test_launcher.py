"""The `python3 app.py` entry point."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def load_launcher():
    spec = importlib.util.spec_from_file_location("_launcher", ROOT / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


launcher = load_launcher()


def test_the_launcher_sits_at_the_repository_root() -> None:
    """The documented command is `python3 app.py` from the clone."""
    assert (ROOT / "app.py").is_file()


def test_it_refuses_a_non_mac(monkeypatch) -> None:
    monkeypatch.setattr(launcher.sys, "platform", "linux")
    with pytest.raises(SystemExit):
        launcher.check_platform()


def test_it_refuses_an_old_python(monkeypatch) -> None:
    monkeypatch.setattr(launcher.sys, "platform", "darwin")
    monkeypatch.setattr(launcher.sys, "version_info", (3, 8, 0))
    with pytest.raises(SystemExit):
        launcher.check_platform()


def test_it_accepts_this_machine(monkeypatch) -> None:
    monkeypatch.setattr(launcher.sys, "platform", "darwin")
    launcher.check_platform()


def test_the_venv_lives_beside_the_launcher_not_in_the_system(monkeypatch) -> None:
    assert launcher.VENV.parent == ROOT
    assert launcher.VENV.name == ".venv"


def test_pyside_detection_reports_reality() -> None:
    assert launcher.have_pyside() is ("PySide6" in sys.modules or _importable())


def _importable() -> bool:
    try:
        import PySide6  # noqa: F401
    except ImportError:
        return False
    return True


def test_a_relaunch_that_still_cannot_import_gives_up(monkeypatch) -> None:
    """Without this guard a broken venv would re-exec forever."""
    monkeypatch.setattr(launcher.sys, "platform", "darwin")
    monkeypatch.setattr(launcher, "have_pyside", lambda: False)
    monkeypatch.setenv(launcher.RELAUNCH_FLAG, "1")

    def must_not_run() -> None:
        raise AssertionError("relaunched despite the guard")

    monkeypatch.setattr(launcher, "relaunch_in_venv", must_not_run)
    with pytest.raises(SystemExit):
        launcher.main()
