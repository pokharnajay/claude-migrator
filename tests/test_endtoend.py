"""Backup then sync, the way the app runs them, against a synthetic tree."""

from __future__ import annotations

import json
from pathlib import Path

from claude_migrator import backup, storage, sync
from tests.conftest import NEW, NEW_ORG, OLD


def test_backup_then_sync_restores_history(storage_root: Path, cli_root: Path, tmp_path: Path) -> None:
    target = storage.find_account(storage_root, NEW)
    source = storage.find_account(storage_root, OLD)
    assert target.total_sessions == 1
    assert source.total_sessions == 4

    snapshot = backup.run_backup(
        tmp_path / "downloads",
        backup.backup_items(storage_root, cli_root),
        current_account=NEW,
    )

    plan = sync.plan_sync(storage_root, target, storage.active_org(target), source.orgs)
    result = sync.execute(plan)
    assert result.failed == []

    restored = storage.find_account(storage_root, NEW)
    assert restored.total_sessions == 5  # 1 existing + 4 restored

    # The backup still reflects the pre-sync state, so it is a real rollback point.
    backed_up = snapshot / "claude-code-sessions" / NEW / NEW_ORG
    assert len(list(backed_up.glob("local_*.json"))) == 1

    # And the source account is untouched.
    assert storage.find_account(storage_root, OLD).total_sessions == 4


def test_second_run_changes_nothing(storage_root: Path) -> None:
    target = storage.find_account(storage_root, NEW)
    source = storage.find_account(storage_root, OLD)
    sync.execute(sync.plan_sync(storage_root, target, storage.active_org(target), source.orgs))

    after_first = {
        p.name: p.read_bytes()
        for p in (storage_root / "claude-code-sessions" / NEW / NEW_ORG).iterdir()
        if p.is_file()
    }

    target = storage.find_account(storage_root, NEW)
    source = storage.find_account(storage_root, OLD)
    second = sync.plan_sync(storage_root, target, storage.active_org(target), source.orgs)
    sync.execute(second)

    after_second = {
        p.name: p.read_bytes()
        for p in (storage_root / "claude-code-sessions" / NEW / NEW_ORG).iterdir()
        if p.is_file()
    }
    assert after_first == after_second


def test_claude_running_check_returns_a_bool() -> None:
    from claude_migrator.ui import claude_is_running

    assert isinstance(claude_is_running(), bool)


def test_the_whole_flow_through_the_window(storage_root, cli_root, tmp_path, monkeypatch) -> None:
    """Back Up then Restore Sessions, driven exactly as the buttons drive them."""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from claude_migrator import backup as backup_module
    from claude_migrator import ui as ui_module
    from tests.conftest import NEW, NEW_ORG
    from tests.test_app import pump

    qt_app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui_module, "claude_is_running", lambda: False)
    monkeypatch.setattr(backup_module, "DOWNLOADS", tmp_path / "Downloads")

    window = ui_module.MigratorWindow(storage_root, cli_root)
    assert window.step == ui_module.STEP_BACKUP

    window._run_backup()
    assert pump(qt_app, lambda: window._thread is None, 30), "backup never finished"
    assert window.backup_verified, window.log.toPlainText()
    assert window.step == ui_module.STEP_SYNC
    assert window.backup_path is not None and window.backup_path.is_dir()

    window._run_sync()
    assert pump(qt_app, lambda: window._thread is None, 30), "restore never finished"
    assert window.step == ui_module.STEP_DONE

    restored = storage_root / "claude-code-sessions" / NEW / NEW_ORG
    assert (restored / "local_old0.json").exists()
    assert "FAILED" not in window.log.toPlainText()
    window.deleteLater()
