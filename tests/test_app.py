"""Window behaviour: what is reachable, and when."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from claude_migrator import app as app_module  # noqa: E402
from claude_migrator.app import STEP_BACKUP, STEP_BLOCKED, STEP_SYNC, MigratorWindow  # noqa: E402


@pytest.fixture(scope="session")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qt_app, storage_root: Path, cli_root: Path, monkeypatch):
    monkeypatch.setattr(app_module, "claude_is_running", lambda: False)
    win = MigratorWindow(storage_root, cli_root)
    yield win
    win.deleteLater()


def test_the_signed_in_account_is_named_not_numbered(window) -> None:
    # The synthetic tree has no identity sources, so it falls back to the short id.
    assert "Signed in as" in window.target_label.text()


def test_sources_exclude_the_signed_in_account(window) -> None:
    assert window.scan is not None
    assert all(row.account.uuid != window.scan.target.uuid for row in window.source_rows)


def test_empty_workspaces_are_not_listed(window) -> None:
    assert all(row.org.total_sessions > 0 for row in window.source_rows)


def test_no_warning_banner_when_claude_is_closed(window) -> None:
    assert window.banner.isHidden()


def test_backup_is_the_first_available_step(window) -> None:
    assert window.step == STEP_BACKUP
    assert window.action_button.text() == "Back Up"
    assert window.action_button.isEnabled()


def test_everything_is_blocked_while_claude_runs(qt_app, storage_root, cli_root, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "claude_is_running", lambda: True)
    win = MigratorWindow(storage_root, cli_root)
    assert win.step == STEP_BLOCKED
    assert not win.action_button.isEnabled()
    assert not win.export_button.isEnabled()
    assert not win.import_button.isEnabled()
    assert not win.banner.isHidden()
    win.deleteLater()


def test_restore_is_refused_without_a_verified_backup(window) -> None:
    window.step = STEP_SYNC
    window.backup_verified = False
    window._run_sync()
    assert "verified backup" in window.log.toPlainText()


def test_restore_is_refused_if_claude_starts_mid_session(window, monkeypatch) -> None:
    window.step = STEP_SYNC
    window.backup_verified = True
    monkeypatch.setattr(app_module, "claude_is_running", lambda: True)
    window._run_sync()
    assert "still running" in window.log.toPlainText()


def test_export_is_refused_while_claude_runs(window, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "claude_is_running", lambda: True)
    window._run_export()
    assert "still running" in window.log.toPlainText()


def test_deselecting_every_source_blocks_the_backup(window) -> None:
    for row in window.source_rows:
        row.checkbox.setChecked(False)
    window._run_backup()
    assert "Select at least one account" in window.log.toPlainText()


def test_a_machine_with_no_other_account_still_offers_import(
    qt_app, tmp_path, monkeypatch
) -> None:
    """Import is the whole point on a fresh Mac — it must not need a local source."""
    monkeypatch.setattr(app_module, "claude_is_running", lambda: False)
    import json

    from tests.conftest import NEW, NEW_ORG

    app_support = tmp_path / "Claude"
    (app_support / "claude-code-sessions" / NEW / NEW_ORG).mkdir(parents=True)
    (app_support / "config.json").write_text(json.dumps({"lastKnownAccountUuid": NEW}))
    cli = tmp_path / "dot-claude"
    cli.mkdir()

    win = MigratorWindow(app_support, cli)
    assert win.source_rows == []
    assert win.import_button.isEnabled()
    assert not win.export_button.isEnabled()
    assert "import a bundle" in win.log.toPlainText()
    win.deleteLater()


def test_human_bytes_reads_naturally() -> None:
    assert app_module.human_bytes(512) == "512 B"
    assert app_module.human_bytes(2048) == "2 KB"
    assert app_module.human_bytes(5 * 1024**3) == "5.0 GB"
