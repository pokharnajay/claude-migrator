from __future__ import annotations

import json
from pathlib import Path

from claude_migrator import backup
from tests.conftest import NEW, NEW_ORG, OLD, OLD_ORG


def items(storage_root: Path, cli_root: Path, transcripts: bool = True):
    return backup.backup_items(storage_root, cli_root, include_transcripts=transcripts)


def test_backup_includes_both_session_trees(storage_root: Path, cli_root: Path) -> None:
    labels = {i.rel for i in items(storage_root, cli_root)}
    assert "claude-code-sessions" in labels
    assert "local-agent-mode-sessions" in labels


def test_transcripts_can_be_excluded(storage_root: Path, cli_root: Path) -> None:
    with_them = {i.rel for i in items(storage_root, cli_root, transcripts=True)}
    without = {i.rel for i in items(storage_root, cli_root, transcripts=False)}
    assert "projects" in with_them
    assert "projects" not in without


def test_absent_sources_are_dropped_from_the_item_list(tmp_path: Path) -> None:
    app = tmp_path / "Claude"
    app.mkdir()
    assert items(app, tmp_path / "nope") == []


def test_run_backup_reproduces_the_tree(storage_root: Path, cli_root: Path, tmp_path: Path) -> None:
    dest = backup.run_backup(tmp_path / "out", items(storage_root, cli_root))
    copied = dest / "claude-code-sessions" / OLD / OLD_ORG / "local_old0.json"
    assert json.loads(copied.read_text())["title"] == "Old session 0"
    assert (dest / "projects" / "-tmp-project" / "cli-old0.jsonl").exists()


def test_backup_destination_is_timestamped_and_unique(
    storage_root: Path, cli_root: Path, tmp_path: Path
) -> None:
    first = backup.run_backup(tmp_path / "out", items(storage_root, cli_root))
    second = backup.run_backup(tmp_path / "out", items(storage_root, cli_root))
    assert first != second
    assert first.name.startswith("claude-backup-")


def test_backup_writes_a_manifest(storage_root: Path, cli_root: Path, tmp_path: Path) -> None:
    dest = backup.run_backup(
        tmp_path / "out", items(storage_root, cli_root), current_account=NEW
    )
    manifest = json.loads((dest / "manifest.json").read_text())
    assert manifest["currentAccount"] == NEW
    assert "claude-code-sessions" in manifest["contents"]


def test_progress_is_reported_per_item(storage_root: Path, cli_root: Path, tmp_path: Path) -> None:
    seen: list[str] = []
    backup.run_backup(
        tmp_path / "out", items(storage_root, cli_root), progress=lambda i, n, label: seen.append(label)
    )
    assert len(seen) == len(items(storage_root, cli_root))


def test_backup_is_readable_after_copy(storage_root: Path, cli_root: Path, tmp_path: Path) -> None:
    dest = backup.run_backup(tmp_path / "out", items(storage_root, cli_root))
    target = dest / "claude-code-sessions" / NEW / NEW_ORG / "local_current.json"
    assert target.is_file() and target.stat().st_size > 0
