"""Guard rails shared by the backup and sync steps.

Every write this tool performs goes through here. The rules are:

* A file is only ever *created*. Creation uses O_EXCL so that the check and the
  write are one atomic step — a plain `exists()` test followed by a copy leaves
  a window in which the desktop app could write the same path and have it
  silently clobbered.
* A file that must be rewritten (the archive index) is written to a temporary
  file and moved into place, so a reader never observes a half-written file.
* Nothing is copied from or to a path that escapes the directory it belongs to.
"""

from __future__ import annotations

import errno
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path


class SafetyError(Exception):
    """A precondition for writing was not met. Nothing was written."""


# --------------------------------------------------------------------- paths


def is_contained(path: Path, root: Path) -> bool:
    """True when `path` resolves to somewhere inside `root`.

    Guards against a symlinked or `..`-laden entry pulling a copy outside the
    directory being migrated.
    """
    try:
        resolved_root = root.resolve(strict=False)
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    return resolved == resolved_root or resolved_root in resolved.parents


def require_contained(path: Path, root: Path, what: str) -> None:
    if not is_contained(path, root):
        raise SafetyError(f"{what} resolves outside {root}: {path}")


# --------------------------------------------------------------------- space


def free_bytes(path: Path) -> int:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    usage = shutil.disk_usage(probe)
    return usage.free


def require_free_space(destination: Path, needed: int, margin: float = 1.15) -> None:
    """Refuse to start a copy that cannot finish.

    APFS clones make the real cost near zero, but the fallback path is a true
    copy, so the requirement is checked against the uncloned size.
    """
    available = free_bytes(destination)
    required = int(needed * margin)
    if available < required:
        raise SafetyError(
            f"Not enough free space: {required // 1_000_000} MB needed, "
            f"{available // 1_000_000} MB available."
        )


# --------------------------------------------------------------------- writes


def copy_file_exclusive(src: Path, dst: Path) -> bool:
    """Copy `src` to `dst` only if `dst` does not exist. Returns False if it did.

    The O_EXCL open is what makes this safe to run while Claude is writing to
    the same directory: the kernel, not this process, decides who wins, and an
    existing file is never truncated.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            return False
        raise
    try:
        with os.fdopen(fd, "wb") as out, open(src, "rb") as source:
            shutil.copyfileobj(source, out)
    except BaseException:
        # Never leave a half-written file behind for the app to read.
        dst.unlink(missing_ok=True)
        raise
    shutil.copystat(src, dst)
    return True


def copy_tree_exclusive(src: Path, dst: Path) -> bool:
    """Copy a directory only if the destination does not exist.

    Built in a sibling temporary directory and moved into place, so a reader
    never sees a partially populated session folder.
    """
    if dst.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".migrator-", dir=dst.parent))
    target = staging / dst.name
    try:
        shutil.copytree(src, target, symlinks=True)
        try:
            os.rename(target, dst)
        except OSError as exc:
            if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                return False  # the app created it first; leave its copy alone
            raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return True


def write_atomic(path: Path, payload: str) -> None:
    """Replace a file's contents in one step."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


# --------------------------------------------------------------- verification


@dataclass(frozen=True)
class TreeStats:
    files: int
    total_bytes: int


def measure(path: Path) -> TreeStats:
    if path.is_file():
        return TreeStats(files=1, total_bytes=path.stat().st_size)
    files = 0
    total = 0
    for item in path.rglob("*"):
        if item.is_file() and not item.is_symlink():
            files += 1
            try:
                total += item.stat().st_size
            except OSError:
                pass
    return TreeStats(files=files, total_bytes=total)


def verify_copy(src: Path, dst: Path) -> tuple[bool, str]:
    """Confirm a backup landed intact before it is relied on."""
    if not dst.exists():
        return False, f"{dst.name} is missing from the backup"
    source = measure(src)
    copied = measure(dst)
    if copied.files < source.files:
        return False, (
            f"{dst.name}: {copied.files} of {source.files} files copied"
        )
    if copied.total_bytes < source.total_bytes:
        return False, (
            f"{dst.name}: {copied.total_bytes} of {source.total_bytes} bytes copied"
        )
    return True, f"{dst.name}: {copied.files} files, {copied.total_bytes} bytes"


# ---------------------------------------------------------------------- lock


class SingleInstance:
    """Stops two copies of the tool from writing at the same time."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def __enter__(self) -> SingleInstance:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            os.write(self._fd, str(os.getpid()).encode())
        except FileExistsError:
            if self._stale():
                self.path.unlink(missing_ok=True)
                return self.__enter__()
            raise SafetyError(
                "Another copy of Claude Migrator is already running."
            ) from None
        return self

    def _stale(self) -> bool:
        try:
            pid = int(self.path.read_text().strip())
        except (OSError, ValueError):
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        return False

    def __exit__(self, *exc_info: object) -> None:
        if self._fd is not None:
            os.close(self._fd)
        self.path.unlink(missing_ok=True)
