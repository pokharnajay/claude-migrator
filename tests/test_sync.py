from __future__ import annotations

import json
from pathlib import Path

from claude_migrator import storage, sync
from tests.conftest import NEW, NEW_ORG, OLD, OLD_ORG


def make_plan(root: Path) -> sync.SyncPlan:
    target = storage.find_account(root, NEW)
    source = storage.find_account(root, OLD)
    return sync.plan_sync(root, target, storage.active_org(target), source.orgs)


def test_plan_copies_every_missing_session(storage_root: Path) -> None:
    plan = make_plan(storage_root)
    names = {op.dst.name for op in plan.to_copy}
    assert {"local_old0.json", "local_old1.json", "local_old2.json"} <= names


def test_plan_includes_cowork_session_sidecar_directory(storage_root: Path) -> None:
    plan = make_plan(storage_root)
    sidecars = [op for op in plan.to_copy if op.is_dir]
    assert [op.dst.name for op in sidecars] == ["local_cw0"]


def test_plan_excludes_account_scoped_state(storage_root: Path) -> None:
    plan = make_plan(storage_root)
    copied = {op.src.name for op in plan.to_copy}
    assert "scheduled-tasks.json" not in copied
    assert "cowork-gb-cache.json" not in copied
    assert "rpm" not in copied


def test_plan_carries_artifacts_index(storage_root: Path) -> None:
    plan = make_plan(storage_root)
    assert "artifacts.json" in {op.src.name for op in plan.to_copy}


def test_plan_never_targets_an_existing_file(storage_root: Path) -> None:
    plan = make_plan(storage_root)
    assert all(not op.dst.exists() for op in plan.to_copy)


def test_existing_files_are_reported_as_skipped(storage_root: Path) -> None:
    target_org = storage_root / "claude-code-sessions" / NEW / NEW_ORG
    (target_org / "local_old0.json").write_text("{}")
    plan = make_plan(storage_root)
    assert [p.name for p in plan.already_present] == ["local_old0.json"]
    assert "local_old0.json" not in {op.dst.name for op in plan.to_copy}


def test_archived_index_is_unioned_not_replaced(storage_root: Path) -> None:
    target_org = storage_root / "claude-code-sessions" / NEW / NEW_ORG
    (target_org / "archived-sessions.idx").write_text(
        json.dumps({"v": 1, "archived": ["local_current"]})
    )
    plan = make_plan(storage_root)
    sync.execute(plan)
    merged = json.loads((target_org / "archived-sessions.idx").read_text())
    assert sorted(merged["archived"]) == ["local_current", "local_old0"]


def test_archived_index_is_created_when_target_has_none(storage_root: Path) -> None:
    plan = make_plan(storage_root)
    sync.execute(plan)
    idx = storage_root / "claude-code-sessions" / NEW / NEW_ORG / "archived-sessions.idx"
    assert json.loads(idx.read_text())["archived"] == ["local_old0"]


def test_execute_copies_sessions_into_target(storage_root: Path) -> None:
    plan = make_plan(storage_root)
    result = sync.execute(plan)
    target_org = storage_root / "claude-code-sessions" / NEW / NEW_ORG
    assert (target_org / "local_old2.json").exists()
    assert result.copied == len(plan.to_copy)
    assert result.failed == []


def test_execute_copies_sidecar_directory_contents(storage_root: Path) -> None:
    sync.execute(make_plan(storage_root))
    blob = (
        storage_root
        / "local-agent-mode-sessions"
        / NEW
        / NEW_ORG
        / "local_cw0"
        / "blob.bin"
    )
    assert blob.read_bytes() == b"payload"


def test_execute_leaves_the_source_untouched(storage_root: Path) -> None:
    before = sorted(p.name for p in (storage_root / "claude-code-sessions" / OLD / OLD_ORG).iterdir())
    sync.execute(make_plan(storage_root))
    after = sorted(p.name for p in (storage_root / "claude-code-sessions" / OLD / OLD_ORG).iterdir())
    assert before == after


def test_execute_never_overwrites_the_current_session(storage_root: Path) -> None:
    target_org = storage_root / "claude-code-sessions" / NEW / NEW_ORG
    original = (target_org / "local_current.json").read_text()
    sync.execute(make_plan(storage_root))
    assert (target_org / "local_current.json").read_text() == original


def test_running_twice_is_a_no_op(storage_root: Path) -> None:
    sync.execute(make_plan(storage_root))
    second = make_plan(storage_root)
    assert second.to_copy == []


def test_syncing_an_account_onto_itself_is_rejected(storage_root: Path) -> None:
    target = storage.find_account(storage_root, NEW)
    try:
        sync.plan_sync(storage_root, target, storage.active_org(target), target.orgs)
    except ValueError as exc:
        assert "itself" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_missing_transcripts_are_detected(storage_root: Path, cli_root: Path) -> None:
    sync.execute(make_plan(storage_root))
    target = storage.find_account(storage_root, NEW)
    missing = sync.missing_transcripts(storage.active_org(target), cli_root / "projects")
    assert [m.title for m in missing] == ["Old session 2"]


def test_cowork_sessions_are_not_counted_as_missing_transcripts(
    storage_root: Path, cli_root: Path
) -> None:
    """Cowork history lives in a sidecar dir, never in ~/.claude/projects."""
    sync.execute(make_plan(storage_root))
    # Remove the one transcript a cowork session happens to share a name with.
    (cli_root / "projects" / "-tmp-project" / "cli-cw0.jsonl").unlink()

    target = storage.find_account(storage_root, NEW)
    missing = sync.missing_transcripts(storage.active_org(target), cli_root / "projects")
    assert [m.title for m in missing] == ["Old session 2"]
