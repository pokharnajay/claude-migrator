from __future__ import annotations

from pathlib import Path

from claude_migrator import storage
from tests.conftest import NEW, NEW_ORG, OLD, OLD_ORG


def test_reads_current_account_from_config(storage_root: Path) -> None:
    assert storage.current_account_uuid(storage_root) == NEW


def test_missing_config_yields_no_current_account(tmp_path: Path) -> None:
    (tmp_path / "Claude").mkdir()
    assert storage.current_account_uuid(tmp_path / "Claude") is None


def test_discovers_both_accounts(storage_root: Path) -> None:
    accounts = storage.discover_accounts(storage_root)
    assert {a.uuid for a in accounts} == {OLD, NEW}


def test_current_account_is_flagged_and_sorted_first(storage_root: Path) -> None:
    accounts = storage.discover_accounts(storage_root)
    assert accounts[0].uuid == NEW
    assert accounts[0].is_current
    assert not accounts[1].is_current


def test_ignores_non_uuid_directories(storage_root: Path) -> None:
    # local-agent-mode-sessions/skills-plugin is a sibling, not an account.
    assert "skills-plugin" not in {a.uuid for a in storage.discover_accounts(storage_root)}


def test_counts_sessions_per_org(storage_root: Path) -> None:
    old = storage.find_account(storage_root, OLD)
    org = old.orgs[0]
    assert org.uuid == OLD_ORG
    assert org.code_sessions == 3
    assert org.agent_sessions == 1
    assert org.total_sessions == 4


def test_org_exposes_activity_window(storage_root: Path) -> None:
    org = storage.find_account(storage_root, OLD).orgs[0]
    assert org.first_activity is not None
    assert org.last_activity is not None
    assert org.first_activity <= org.last_activity


def test_active_org_is_the_most_recently_written(storage_root: Path) -> None:
    account = storage.find_account(storage_root, NEW)
    assert storage.active_org(account).uuid == NEW_ORG


def test_account_with_no_sessions_still_reports_zero(tmp_path: Path) -> None:
    app = tmp_path / "Claude"
    (app / "claude-code-sessions" / OLD / OLD_ORG).mkdir(parents=True)
    account = storage.find_account(app, OLD)
    assert account.total_sessions == 0
