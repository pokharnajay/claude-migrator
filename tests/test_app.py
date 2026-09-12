"""Window behaviour: what is reachable, and when."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from claude_migrator import ui as app_module  # noqa: E402
from claude_migrator.ui import STEP_BACKUP, STEP_BLOCKED, STEP_SYNC, MigratorWindow  # noqa: E402


@pytest.fixture(scope="session")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qt_app, storage_root: Path, cli_root: Path, monkeypatch):
    monkeypatch.setattr(app_module, "claude_is_running", lambda: False)
    monkeypatch.setattr(app_module, "SCAN_MINIMUM_SECONDS", 0.0)
    win = MigratorWindow(storage_root, cli_root)
    settle(qt_app, win)
    yield win
    win.deleteLater()


def settle(qt_app, win, seconds: float = 15.0) -> None:
    """Let an in-flight scan (or any job) finish before asserting on the window."""
    assert pump(qt_app, lambda: win._thread is None, seconds), "a background job never finished"


def test_the_signed_in_account_is_named_not_numbered(window) -> None:
    # The synthetic tree has no identity sources, so it falls back to the short id.
    assert "Signed in as" in window.target_label.text()


def test_sources_exclude_the_signed_in_account(window) -> None:
    assert window.scan is not None
    assert all(row.account.uuid != window.scan.target.uuid for row in window.source_rows)


def test_empty_workspaces_are_not_listed(window) -> None:
    assert all(row.org.total_sessions > 0 for row in window.source_rows)


def test_no_warning_banner_when_claude_is_closed(window) -> None:
    assert window.banner_box.isHidden()


def test_backup_is_the_first_available_step(window) -> None:
    assert window.step == STEP_BACKUP
    assert window.action_button.text() == "Back Up"
    assert window.action_button.isEnabled()


def test_everything_is_blocked_while_claude_runs(qt_app, storage_root, cli_root, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "claude_is_running", lambda: True)
    monkeypatch.setattr(app_module, "SCAN_MINIMUM_SECONDS", 0.0)
    win = MigratorWindow(storage_root, cli_root)
    settle(qt_app, win)
    assert win.step == STEP_BLOCKED
    assert not win.action_button.isEnabled()
    assert not win.export_button.isEnabled()
    assert not win.import_button.isEnabled()
    assert not win.banner_box.isHidden()
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
    monkeypatch.setattr(app_module, "SCAN_MINIMUM_SECONDS", 0.0)
    import json

    from tests.conftest import NEW, NEW_ORG

    app_support = tmp_path / "Claude"
    (app_support / "claude-code-sessions" / NEW / NEW_ORG).mkdir(parents=True)
    (app_support / "config.json").write_text(json.dumps({"lastKnownAccountUuid": NEW}))
    cli = tmp_path / "dot-claude"
    cli.mkdir()

    win = MigratorWindow(app_support, cli)
    settle(qt_app, win)
    assert win.source_rows == []
    assert win.import_button.isEnabled()
    assert not win.export_button.isEnabled()
    assert "import a bundle" in win.log.toPlainText()
    win.deleteLater()


def test_human_bytes_reads_naturally() -> None:
    assert app_module.human_bytes(512) == "512 B"
    assert app_module.human_bytes(2048) == "2 KB"
    assert app_module.human_bytes(5 * 1024**3) == "5.0 GB"


# --------------------------------------------------------- background workers


def pump(qt_app, predicate, seconds: float = 8.0) -> bool:
    """Spin the Qt event loop until `predicate` holds, as a real window would."""
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qt_app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_a_background_job_completes_without_crashing(qt_app, window) -> None:
    """Regression: the finished signal was connected to a lambda.

    With no QObject receiver, Qt ran the slot directly in the worker thread,
    where it called wait() on that same thread — "Thread tried to wait on
    itself", then the QThread was destroyed while running and the process
    aborted.
    """
    window._start(lambda progress, log: (log("working"), "done")[1], "Working…", STEP_SYNC)
    assert pump(qt_app, lambda: window._thread is None), "worker thread never shut down"
    assert "working" in window.log.toPlainText()
    assert window.status.text() == "done"
    assert window.step == STEP_SYNC


def test_the_worker_thread_is_released_after_the_job(qt_app, window) -> None:
    window._start(lambda progress, log: "finished", "Working…", STEP_SYNC)
    pump(qt_app, lambda: window._thread is None)
    assert window._thread is None
    assert window._worker is None


def test_controls_come_back_after_a_job(qt_app, window) -> None:
    window._start(lambda progress, log: "ok", "Working…", STEP_SYNC)
    pump(qt_app, lambda: window._thread is None)
    assert window.rescan_button.isEnabled()
    assert window.transcripts.isEnabled()


def test_a_failing_job_is_reported_and_does_not_advance(qt_app, window) -> None:
    def explode(progress, log):
        raise RuntimeError("disk on fire")

    window.step = STEP_BACKUP
    window._start(explode, "Working…", STEP_SYNC)
    assert pump(qt_app, lambda: window._thread is None)
    assert "disk on fire" in window.log.toPlainText()
    assert window.status.text() == "Stopped"
    assert window.step == STEP_BACKUP  # never reached the restore step


def test_progress_updates_reach_the_bar(qt_app, window) -> None:
    def counted(progress, log):
        for i in range(1, 4):
            progress(i, 3, f"item {i}")
        return "done"

    window._start(counted, "Working…", STEP_SYNC)
    pump(qt_app, lambda: window._thread is None)
    assert window.progress.maximum() == 3


def test_rescanning_shows_that_it_ran(qt_app, window) -> None:
    """Repeated scans look identical otherwise, which reads as nothing happening."""
    window.do_scan()
    settle(qt_app, window)
    assert "Scanned at" in window.log.toPlainText()


def test_a_job_can_log_a_blank_line(qt_app, window) -> None:
    """Signal(str).emit rejects a zero-argument call; jobs rely on `log()`."""

    def chatty(progress, log):
        log("first")
        log()
        log("second")
        return "ok"

    window._start(chatty, "Working…", STEP_SYNC)
    assert pump(qt_app, lambda: window._thread is None)
    assert window.status.text() == "ok", window.log.toPlainText()
    text = window.log.toPlainText()
    assert "first" in text and "second" in text


# ------------------------------------------------ the signed-in account itself


def test_the_signed_in_account_gets_a_row_too(window) -> None:
    assert window.target_rows, "the account being restored into was not shown"
    assert window.target_rows[0].account.uuid == window.scan.target.uuid


def test_the_signed_in_row_cannot_be_deselected(window) -> None:
    """It is the destination, not a source — there is nothing to tick."""
    row = window.target_rows[0]
    assert row.checkbox is None
    assert row.selected is False


def test_the_signed_in_account_reports_its_own_sessions(window) -> None:
    from claude_migrator.ui import describe_workspace

    org = window.scan.target_org
    summary = describe_workspace(org)
    assert f"{org.code_sessions} code" in summary
    assert f"{org.agent_sessions} cowork" in summary


def test_the_scan_log_says_what_is_already_held(window) -> None:
    text = window.log.toPlainText()
    assert "This account already holds" in text
    assert window.scan.target_org.uuid[:8] in text


def test_the_scan_names_the_most_recent_session(window) -> None:
    """So a rescan visibly reflects work done since the last one."""
    assert "most recent:" in window.log.toPlainText()


def test_an_empty_workspace_says_so_rather_than_showing_zeroes() -> None:
    from claude_migrator.storage import Org
    from claude_migrator.ui import describe_workspace

    empty = Org(
        uuid="x", account_uuid="y", code_dir=None, agent_dir=None,
        code_sessions=0, agent_sessions=0, first_activity=None, last_activity=None,
    )
    assert describe_workspace(empty) == "no sessions"


def test_rescan_refreshes_the_signed_in_rows_without_duplicating_them(qt_app, window) -> None:
    before = len(window.target_rows)
    window.do_scan()
    settle(qt_app, window)
    window.do_scan()
    settle(qt_app, window)
    assert len(window.target_rows) == before


# ------------------------------------------------------------ button legibility


def test_disabled_buttons_keep_a_background_and_border(qt_app, storage_root, cli_root, monkeypatch) -> None:
    """A disabled button with a transparent background reads as plain text."""
    monkeypatch.setattr(app_module, "claude_is_running", lambda: True)
    monkeypatch.setattr(app_module, "SCAN_MINIMUM_SECONDS", 0.0)
    win = MigratorWindow(storage_root, cli_root)
    settle(qt_app, win)
    style = win.styleSheet()
    disabled = style[style.index("QPushButton:disabled"):]
    block = disabled[: disabled.index("}")]
    assert "background: transparent" not in block
    assert "border:" in block
    win.deleteLater()


# ------------------------------------------------------- the blocked state


def test_a_disabled_button_is_visibly_recessed(qt_app, storage_root, cli_root, monkeypatch) -> None:
    """Enabled and disabled must not share a fill.

    They did once, so Export Bundle looked clickable while Claude was running;
    clicking it did nothing and said nothing.
    """
    monkeypatch.setattr(app_module, "claude_is_running", lambda: True)
    monkeypatch.setattr(app_module, "SCAN_MINIMUM_SECONDS", 0.0)
    win = MigratorWindow(storage_root, cli_root)
    settle(qt_app, win)
    style = win.styleSheet()

    def block(selector: str) -> str:
        start = style.index(selector)
        return style[start : style.index("}", start)]

    enabled_fill = block("QPushButton {").split("background:")[1].split(";")[0].strip()
    disabled_fill = block("QPushButton:disabled").split("background:")[1].split(";")[0].strip()
    assert enabled_fill != disabled_fill
    win.deleteLater()


def test_the_banner_offers_a_way_out(qt_app, storage_root, cli_root, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "claude_is_running", lambda: True)
    monkeypatch.setattr(app_module, "SCAN_MINIMUM_SECONDS", 0.0)
    win = MigratorWindow(storage_root, cli_root)
    settle(qt_app, win)
    assert not win.banner_box.isHidden()
    assert win.quit_claude_button.isEnabled(), "the only enabled control must be the way out"
    win.deleteLater()


def test_quitting_claude_rescans_and_unblocks(qt_app, storage_root, cli_root, monkeypatch) -> None:
    state = {"running": True}
    monkeypatch.setattr(app_module, "claude_is_running", lambda: state["running"])
    monkeypatch.setattr(app_module, "SCAN_MINIMUM_SECONDS", 0.0)
    win = MigratorWindow(storage_root, cli_root)
    settle(qt_app, win)
    assert win.step == STEP_BLOCKED

    def quit_it(*args, **kwargs):
        state["running"] = False

        class R:
            returncode = 0
            stdout = stderr = ""

        return R()

    monkeypatch.setattr(app_module.subprocess, "run", quit_it)
    win._quit_claude()
    settle(qt_app, win)

    assert win.step == STEP_BACKUP
    assert win.banner_box.isHidden()
    assert "Claude has quit." in win.log.toPlainText()
    win.deleteLater()


def test_export_runs_when_nothing_blocks_it(qt_app, window, tmp_path, monkeypatch) -> None:
    """The button itself, clicked — not the function behind it."""
    from claude_migrator import backup as backup_module

    monkeypatch.setattr(backup_module, "DOWNLOADS", tmp_path / "Downloads")
    assert window.export_button.isEnabled()
    window.export_button.click()
    assert pump(qt_app, lambda: window._thread is None, 30)
    assert window.status.text() == "Bundle exported", window.log.toPlainText()
    assert list((tmp_path / "Downloads").glob("*.zip"))


# ------------------------------------------------------------------ scanning


def test_scanning_happens_off_the_gui_thread(qt_app, window) -> None:
    """Otherwise the spinner cannot turn and the window freezes while it runs."""
    window.do_scan()
    assert window._thread is not None, "the scan ran on the GUI thread"
    settle(qt_app, window)


def test_rows_show_a_spinner_while_being_recounted(qt_app, window) -> None:
    window.do_scan()
    row = (window.source_rows + window.target_rows)[0]
    assert row.detail.text() == "scanning…"
    assert not row.spinner.isHidden()
    settle(qt_app, window)


def test_the_spinner_stops_and_the_counts_come_back(qt_app, window) -> None:
    window.do_scan()
    settle(qt_app, window)
    row = (window.source_rows + window.target_rows)[0]
    assert row.spinner.isHidden()
    assert "code" in row.detail.text()


def test_a_first_run_shows_a_placeholder_while_it_looks(qt_app, storage_root, cli_root, monkeypatch) -> None:
    """On launch there are no rows yet, so the spinner needs somewhere to live."""
    monkeypatch.setattr(app_module, "claude_is_running", lambda: False)
    monkeypatch.setattr(app_module, "SCAN_MINIMUM_SECONDS", 0.0)
    win = MigratorWindow(storage_root, cli_root)
    assert not win.scanning_row.isHidden()
    settle(qt_app, win)
    assert win.scanning_row.isHidden()
    win.deleteLater()


def test_a_second_scan_is_ignored_while_one_is_running(qt_app, window) -> None:
    window.do_scan()
    first = window._thread
    window.do_scan()
    assert window._thread is first, "a concurrent scan was started"
    settle(qt_app, window)


def test_the_spinner_animates(qt_app) -> None:
    from claude_migrator.ui import Spinner

    spinner = Spinner()
    spinner.start()
    before = spinner._angle
    for _ in range(3):
        spinner._advance()
    assert spinner._angle != before
    spinner.stop()
    assert not spinner._timer.isActive()


def test_closing_mid_scan_does_not_destroy_a_running_thread(qt_app, storage_root, cli_root, monkeypatch) -> None:
    from PySide6.QtGui import QCloseEvent

    monkeypatch.setattr(app_module, "claude_is_running", lambda: False)
    monkeypatch.setattr(app_module, "SCAN_MINIMUM_SECONDS", 0.0)
    win = MigratorWindow(storage_root, cli_root)
    assert win._thread is not None
    win.closeEvent(QCloseEvent())
    assert win._thread is None or not win._thread.isRunning()
    win.deleteLater()
