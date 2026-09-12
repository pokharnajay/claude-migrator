"""Merges sessions from other accounts into the signed-in one.

The only thing this module is allowed to do is create files that do not yet
exist in the target. There is no overwrite path and no delete path, so a sync
can be run repeatedly and can never cost you work done under the new account.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import safety, storage
from .safety import SafetyError
from .storage import CODE_TREE, SESSION_TREES, Account, Org

ProgressFn = Callable[[int, int, str], None]

ARCHIVE_INDEX = "archived-sessions.idx"

# Copied alongside the sessions themselves. Everything else in an org directory
# (scheduled-tasks.json, rpm/, cowork-*-cache.json, debug/, agent/) is state
# belonging to the account or the machine, not to the conversation history.
EXTRA_FILES = ("artifacts.json",)


@dataclass(frozen=True)
class FileOp:
    src: Path
    dst: Path
    is_dir: bool
    tree: str

    @property
    def size(self) -> int:
        if self.is_dir:
            return sum(p.stat().st_size for p in self.src.rglob("*") if p.is_file())
        return self.src.stat().st_size


@dataclass
class SessionRef:
    session_file: str
    cli_session_id: str | None
    title: str


@dataclass
class SyncPlan:
    target: Account
    target_org: Org
    source_orgs: list[Org]
    to_copy: list[FileOp] = field(default_factory=list)
    already_present: list[Path] = field(default_factory=list)
    archived_additions: set[str] = field(default_factory=set)

    @property
    def total_bytes(self) -> int:
        return sum(op.size for op in self.to_copy)

    @property
    def session_count(self) -> int:
        return sum(1 for op in self.to_copy if not op.is_dir and op.dst.name.endswith(".json"))


@dataclass
class SyncResult:
    copied: int = 0
    skipped_existing: int = 0
    failed: list[tuple[Path, str]] = field(default_factory=list)
    archived_merged: int = 0


def _is_session_entry(path: Path) -> bool:
    return path.name.startswith("local_")


def _read_archive_index(path: Path) -> set[str]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return set()
    archived = data.get("archived")
    return set(archived) if isinstance(archived, list) else set()


def plan_sync(
    root: Path,
    target: Account,
    target_org: Org,
    source_orgs: list[Org],
) -> SyncPlan:
    """Work out exactly which files a sync would create. Writes nothing."""
    if any(org.account_uuid == target.uuid for org in source_orgs):
        raise ValueError("Cannot sync an account into itself — pick a different source.")

    plan = SyncPlan(target=target, target_org=target_org, source_orgs=list(source_orgs))

    for tree in SESSION_TREES:
        dst_dir = root / tree / target.uuid / target_org.uuid
        for source_org in source_orgs:
            src_dir = source_org.dir_for(tree)
            if src_dir is None or not src_dir.is_dir():
                continue

            for entry in sorted(src_dir.iterdir()):
                if entry.name == ARCHIVE_INDEX:
                    plan.archived_additions |= _read_archive_index(entry)
                    continue
                if not (_is_session_entry(entry) or entry.name in EXTRA_FILES):
                    continue

                # A symlinked or oddly named entry must not drag the copy out
                # of the directory pair being migrated.
                safety.require_contained(entry, src_dir, "source entry")
                destination = dst_dir / entry.name
                safety.require_contained(destination, dst_dir, "destination")

                if destination.exists():
                    plan.already_present.append(destination)
                else:
                    plan.to_copy.append(
                        FileOp(src=entry, dst=destination, is_dir=entry.is_dir(), tree=tree)
                    )

    existing_index = root / CODE_TREE / target.uuid / target_org.uuid / ARCHIVE_INDEX
    plan.archived_additions -= _read_archive_index(existing_index)
    return plan


def execute(plan: SyncPlan, progress: ProgressFn | None = None) -> SyncResult:
    """Carry out the plan.

    Creates files and nothing else. Each copy is an atomic create-if-absent, so
    a session the desktop app writes while this runs is left exactly as the app
    wrote it rather than being overwritten or truncated.
    """
    result = SyncResult()

    destination_root = plan.to_copy[0].dst.parent if plan.to_copy else None
    if destination_root is not None:
        safety.require_free_space(destination_root, plan.total_bytes)

    for index, op in enumerate(plan.to_copy, start=1):
        if progress:
            progress(index, len(plan.to_copy), op.dst.name)
        try:
            if op.is_dir:
                created = safety.copy_tree_exclusive(op.src, op.dst)
            else:
                created = safety.copy_file_exclusive(op.src, op.dst)
            if created:
                result.copied += 1
            else:
                result.skipped_existing += 1
        except (OSError, SafetyError) as exc:
            result.failed.append((op.src, str(exc)))

    if plan.archived_additions:
        index_path = _archive_index_path(plan)
        # Re-read immediately before writing: the app may have archived
        # something else since the plan was drawn up, and a union must not
        # drop it.
        merged = _read_archive_index(index_path) | plan.archived_additions
        try:
            safety.write_atomic(
                index_path, json.dumps({"v": 1, "archived": sorted(merged)})
            )
            result.archived_merged = len(plan.archived_additions)
        except OSError as exc:
            result.failed.append((index_path, str(exc)))

    return result


def _archive_index_path(plan: SyncPlan) -> Path:
    base = plan.target_org.code_dir
    if base is None:  # org exists only in the agent tree; derive the sibling path
        agent_dir = plan.target_org.agent_dir
        assert agent_dir is not None
        base = agent_dir.parent.parent.parent / CODE_TREE / plan.target.uuid / plan.target_org.uuid
    return base / ARCHIVE_INDEX


def read_sessions(org: Org, trees: tuple[str, ...] = SESSION_TREES) -> list[SessionRef]:
    """Session records in an org, across the requested trees."""
    refs: list[SessionRef] = []
    for tree in trees:
        directory = org.dir_for(tree)
        if directory is None or not directory.is_dir():
            continue
        for path in sorted(directory.glob("local_*.json")):
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            refs.append(
                SessionRef(
                    session_file=path.name,
                    cli_session_id=data.get("cliSessionId"),
                    title=data.get("title") or "Untitled session",
                )
            )
    return refs


def missing_transcripts(org: Org, projects_dir: Path) -> list[SessionRef]:
    """Code sessions whose CLI transcript is gone, so they restore but open empty.

    Only the Claude Code tree is checked. Cowork sessions keep their history in a
    `local_<id>/` sidecar directory that travels with the session file, so they
    have no counterpart in `~/.claude/projects` and would all look "missing".
    """
    available = (
        {path.stem for path in projects_dir.rglob("*.jsonl")}
        if projects_dir.is_dir()
        else set()
    )
    return [
        ref
        for ref in read_sessions(org, trees=(CODE_TREE,))
        if ref.cli_session_id and ref.cli_session_id not in available
    ]
