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
