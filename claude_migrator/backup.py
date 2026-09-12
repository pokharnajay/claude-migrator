"""Snapshots the whole Claude storage footprint into ~/Downloads.

Copies go through `cp -c`, which asks APFS for a clone: the copy shares blocks
with the original until one of them changes, so backing up gigabytes of CLI
transcripts takes about a second and costs almost no disk. On a non-APFS
volume this falls back to a normal recursive copy.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import safety, storage

ProgressFn = Callable[[int, int, str], None]

DOWNLOADS = Path.home() / "Downloads"


@dataclass(frozen=True)
class BackupItem:
    label: str
    src: Path
    rel: str  # where it lands inside the backup directory


def backup_items(
    app_support: Path = storage.APP_SUPPORT,
    cli_home: Path = storage.CLI_HOME,
    include_transcripts: bool = True,
) -> list[BackupItem]:
    """Everything worth preserving, in the order it should be copied."""
    candidates: list[BackupItem] = [
        BackupItem("Claude Code sessions", app_support / storage.CODE_TREE, storage.CODE_TREE),
        BackupItem("Cowork sessions", app_support / storage.AGENT_TREE, storage.AGENT_TREE),
        BackupItem("Account config", app_support / "config.json", "config.json"),
        BackupItem(
            "Desktop MCP config",
            app_support / "claude_desktop_config.json",
            "claude_desktop_config.json",
        ),
        BackupItem("CLI settings", cli_home / "settings.json", "settings.json"),
        BackupItem("CLI local settings", cli_home / "settings.local.json", "settings.local.json"),
        BackupItem("CLAUDE.md", cli_home / "CLAUDE.md", "CLAUDE.md"),
        BackupItem("CLI state", cli_home.parent / ".claude.json", "claude.json"),
    ]
    if include_transcripts:
        candidates.insert(
            2, BackupItem("CLI transcripts", cli_home / "projects", "projects")
        )
    return [item for item in candidates if item.src.exists()]


def total_size(items: list[BackupItem]) -> int:
    total = 0
    for item in items:
        if item.src.is_file():
            total += item.src.stat().st_size
        else:
            total += sum(p.stat().st_size for p in item.src.rglob("*") if p.is_file())
    return total


def _clone(src: Path, dst: Path) -> None:
    """Clone on APFS, plain copy anywhere else."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["cp", "-c", "-R", str(src), str(dst)], capture_output=True, text=True
    )
    if result.returncode == 0:
        return
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True, symlinks=True)
    else:
        shutil.copy2(src, dst)


def _unique_destination(parent: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = parent / f"claude-backup-{stamp}"
    suffix = 2
    while dest.exists():
        dest = parent / f"claude-backup-{stamp}-{suffix}"
        suffix += 1
    return dest


@dataclass
class BackupResult:
    path: Path
    verified: bool
    findings: list[str]


def run_backup(
    parent: Path = DOWNLOADS,
    items: list[BackupItem] | None = None,
    progress: ProgressFn | None = None,
    current_account: str | None = None,
) -> Path:
    """Copy `items` into a fresh timestamped folder under `parent`."""
    return run_backup_verified(parent, items, progress, current_account).path


def run_backup_verified(
    parent: Path = DOWNLOADS,
    items: list[BackupItem] | None = None,
    progress: ProgressFn | None = None,
    current_account: str | None = None,
) -> BackupResult:
    """Copy `items`, then confirm every one of them arrived intact.

    Verification is the point: the sync step is only worth running once there is
    a backup that is known good, so the file counts and byte totals of each item
    are compared against the source before the result is reported as verified.
    """
    if items is None:
        items = backup_items()
    if not items:
        raise safety.SafetyError("Nothing to back up — no Claude storage found.")

    parent.mkdir(parents=True, exist_ok=True)
    safety.require_free_space(parent, total_size(items))

    dest = _unique_destination(parent)
    dest.mkdir()

    for index, item in enumerate(items, start=1):
        if progress:
            progress(index, len(items), item.label)
        _clone(item.src, dest / item.rel)

    findings: list[str] = []
    verified = True
    for item in items:
        ok, detail = safety.verify_copy(item.src, dest / item.rel)
        findings.append(("OK   " if ok else "FAIL ") + detail)
        verified = verified and ok

    manifest = {
        "createdAt": datetime.now().astimezone().isoformat(),
        "currentAccount": current_account,
        "contents": [item.rel for item in items],
        "verified": verified,
        "verification": findings,
        "sourceRoots": {
            "appSupport": str(storage.APP_SUPPORT),
            "cliHome": str(storage.CLI_HOME),
        },
    }
    safety.write_atomic(dest / "manifest.json", json.dumps(manifest, indent=2))
    return BackupResult(path=dest, verified=verified, findings=findings)
