"""Move sessions between Macs as a single zip file.

Export gathers the selected sessions *and* the CLI transcripts they depend on —
without those the sessions arrive and open empty — plus a manifest recording a
SHA-256 for every member.

Import treats the archive as hostile input, because it arrived from somewhere
else. Every member name is validated before anything is extracted: no absolute
paths, no parent traversal, no symlinks, no unexpected file names, and a cap on
both member count and uncompressed size. Extraction goes to a staging directory
first and only reaches the real storage through the same create-only copy the
local restore uses.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import safety, storage, sync
from .safety import SafetyError
from .storage import AGENT_TREE, CODE_TREE, Org

ProgressFn = Callable[[int, int, str], None]

BUNDLE_VERSION = 1
MANIFEST_NAME = "bundle.json"
PROJECTS_PREFIX = "projects"

# An honest bundle is sessions and transcripts. Anything else is refused.
ALLOWED_PREFIXES = (CODE_TREE, AGENT_TREE, PROJECTS_PREFIX)
MAX_MEMBERS = 200_000
MAX_UNCOMPRESSED = 20 * 1024**3  # 20 GB
MAX_COMPRESSION_RATIO = 200  # a bomb inflates far harder than session JSON does


# --------------------------------------------------------------------- export


@dataclass
class ExportSummary:
    path: Path
    sessions: int
    transcripts: int
    missing_transcripts: list[str] = field(default_factory=list)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _transcript_index(projects_dir: Path) -> dict[str, Path]:
    """cliSessionId -> transcript path, across every project directory."""
    if not projects_dir.is_dir():
        return {}
    return {path.stem: path for path in projects_dir.rglob("*.jsonl")}


def export_bundle(
    destination_dir: Path,
    source_orgs: list[Org],
    projects_dir: Path,
    label: str,
    progress: ProgressFn | None = None,
) -> ExportSummary:
    """Write a portable zip of `source_orgs` into `destination_dir`."""
    if not source_orgs:
        raise SafetyError("Nothing selected to export.")

    transcripts = _transcript_index(projects_dir)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_label = "".join(c if c.isalnum() or c in "-_." else "-" for c in label)[:40]
    archive_path = destination_dir / f"claude-sessions-{safe_label}-{stamp}.zip"
    destination_dir.mkdir(parents=True, exist_ok=True)

    members: list[tuple[Path, str]] = []
    session_count = 0
    wanted_transcripts: dict[str, Path] = {}
    missing: list[str] = []

    for org in source_orgs:
        for tree in (CODE_TREE, AGENT_TREE):
            directory = org.dir_for(tree)
            if directory is None or not directory.is_dir():
                continue
            for entry in sorted(directory.iterdir()):
                keep = entry.name.startswith("local_") or entry.name in (
                    "artifacts.json",
                    sync.ARCHIVE_INDEX,
                )
                if not keep:
                    continue
                safety.require_contained(entry, directory, "export entry")
                base = f"{tree}/{org.uuid}"
                if entry.is_dir():
                    for inner in sorted(entry.rglob("*")):
                        if inner.is_file() and not inner.is_symlink():
                            members.append(
                                (inner, f"{base}/{entry.name}/{inner.relative_to(entry)}")
                            )
                elif entry.is_file() and not entry.is_symlink():
                    members.append((entry, f"{base}/{entry.name}"))

        # Pull in the transcript behind each Claude Code session.
        for ref in sync.read_sessions(org, trees=(CODE_TREE,)):
            session_count += 1
            if not ref.cli_session_id:
                continue
            found = transcripts.get(ref.cli_session_id)
            if found is None:
                missing.append(ref.title)
            else:
                wanted_transcripts[ref.cli_session_id] = found
        session_count += len(sync.read_sessions(org, trees=(AGENT_TREE,)))

    for path in wanted_transcripts.values():
        members.append((path, f"{PROJECTS_PREFIX}/{path.parent.name}/{path.name}"))

    total = sum(src.stat().st_size for src, _ in members)
    safety.require_free_space(destination_dir, total)

    manifest = {
        "version": BUNDLE_VERSION,
        "exportedAt": datetime.now().astimezone().isoformat(),
        "label": label,
        "orgs": [o.uuid for o in source_orgs],
        "sessions": session_count,
        "transcripts": len(wanted_transcripts),
        "files": {},
    }

    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for index, (src, arcname) in enumerate(members, start=1):
            if progress:
                progress(index, len(members), Path(arcname).name)
            archive.write(src, arcname)
            manifest["files"][arcname] = _sha256(src)
        archive.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2))

    return ExportSummary(
        path=archive_path,
        sessions=session_count,
        transcripts=len(wanted_transcripts),
        missing_transcripts=missing,
    )


# --------------------------------------------------------------------- import


@dataclass
class BundleInfo:
    path: Path
    version: int
    exported_at: str
    label: str
    sessions: int
    transcripts: int
    members: list[str]


@dataclass
class ImportResult:
    sessions_copied: int = 0
    transcripts_copied: int = 0
    skipped_existing: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    unknown_cwds: list[str] = field(default_factory=list)


def _reject(reason: str) -> None:
    raise SafetyError(f"Refusing this bundle: {reason}")


def _validate_member(info: zipfile.ZipInfo) -> None:
    name = info.filename

    if info.is_dir():
        return
    if name == MANIFEST_NAME:
        return

    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        _reject(f"absolute path in archive ({name})")
    parts = Path(name).parts
    if ".." in parts or any(part.startswith("/") for part in parts):
        _reject(f"path traversal in archive ({name})")
    if "\\" in name or "\x00" in name:
        _reject(f"illegal characters in archive member ({name})")

    # Unix mode is in the top 16 bits; 0o120000 marks a symlink.
    if (info.external_attr >> 16) & 0o170000 == 0o120000:
        _reject(f"symlink in archive ({name})")

    if parts[0] not in ALLOWED_PREFIXES:
        _reject(f"unexpected top-level entry ({parts[0]})")

    if parts[0] == PROJECTS_PREFIX:
        if len(parts) != 3 or not name.endswith(".jsonl"):
            _reject(f"unexpected transcript path ({name})")
    else:
        if len(parts) < 3:
            _reject(f"unexpected session path ({name})")
        if not storage.is_uuid(parts[1]):
            _reject(f"workspace segment is not a UUID ({parts[1]})")
        leaf_root = parts[2]
        if not (leaf_root.startswith("local_") or leaf_root in ("artifacts.json", sync.ARCHIVE_INDEX)):
            _reject(f"unexpected session file ({leaf_root})")

    if info.file_size > MAX_UNCOMPRESSED:
        _reject(f"member too large ({name})")
    if info.compress_size and info.file_size / max(info.compress_size, 1) > MAX_COMPRESSION_RATIO:
        _reject(f"implausible compression ratio ({name})")


def inspect_bundle(path: Path) -> BundleInfo:
    """Validate an archive end to end. Extracts nothing."""
    if not zipfile.is_zipfile(path):
        _reject("not a zip archive")

    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_MEMBERS:
            _reject(f"too many entries ({len(infos)})")
        if sum(i.file_size for i in infos) > MAX_UNCOMPRESSED:
            _reject("uncompressed size exceeds the limit")

        for info in infos:
            _validate_member(info)

        if MANIFEST_NAME not in archive.namelist():
            _reject("no bundle manifest — this was not made by Claude Migrator")
        try:
            manifest = json.loads(archive.read(MANIFEST_NAME))
        except (json.JSONDecodeError, UnicodeDecodeError):
            _reject("manifest is unreadable")

    if not isinstance(manifest, dict):
        _reject("manifest is not an object")
    version = manifest.get("version")
    if version != BUNDLE_VERSION:
        _reject(f"unsupported bundle version ({version})")

    return BundleInfo(
        path=path,
        version=version,
        exported_at=str(manifest.get("exportedAt", "unknown")),
        label=str(manifest.get("label", "unknown")),
        sessions=int(manifest.get("sessions", 0) or 0),
        transcripts=int(manifest.get("transcripts", 0) or 0),
        members=[n for n in manifest.get("files", {})],
    )


def import_bundle(
    path: Path,
    app_support: Path,
    cli_home: Path,
    target_account: str,
    target_org: str,
    progress: ProgressFn | None = None,
) -> ImportResult:
    """Merge a validated bundle into the signed-in account on this Mac."""
    info = inspect_bundle(path)
    result = ImportResult()

    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read(MANIFEST_NAME))
        checksums = manifest.get("files", {})
        staging = Path(tempfile.mkdtemp(prefix="claude-migrator-import-"))
        try:
            members = [i for i in archive.infolist() if not i.is_dir() and i.filename != MANIFEST_NAME]
            safety.require_free_space(app_support, sum(i.file_size for i in members))

            for index, member in enumerate(members, start=1):
                if progress:
                    progress(index, len(members), Path(member.filename).name)

                archive.extract(member, staging)
                extracted = staging / member.filename
                # Belt and braces: confirm where the member actually landed.
                safety.require_contained(extracted, staging, "extracted member")

                expected = checksums.get(member.filename)
                if expected and _sha256(extracted) != expected:
                    result.failed.append((member.filename, "checksum mismatch"))
                    continue

                destination = _destination_for(
                    member.filename, app_support, cli_home, target_account, target_org
                )
                if destination is None:
                    result.failed.append((member.filename, "no destination"))
                    continue

                try:
                    created = safety.copy_file_exclusive(extracted, destination)
                except (OSError, SafetyError) as exc:
                    result.failed.append((member.filename, str(exc)))
                    continue

                if not created:
                    result.skipped_existing += 1
                elif member.filename.startswith(PROJECTS_PREFIX):
                    result.transcripts_copied += 1
                else:
                    result.sessions_copied += 1
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    result.unknown_cwds = _unreachable_cwds(
        app_support / CODE_TREE / target_account / target_org
    )
    return result


def _destination_for(
    arcname: str,
    app_support: Path,
    cli_home: Path,
    target_account: str,
    target_org: str,
) -> Path | None:
    """Map an archive member onto this machine's storage."""
    parts = Path(arcname).parts
    if parts[0] == PROJECTS_PREFIX:
        # Keep the original project directory name so the transcript stays
        # paired with the cwd recorded in the session.
        return cli_home / "projects" / parts[1] / parts[2]
    if parts[0] in (CODE_TREE, AGENT_TREE):
        # parts[1] is the *source* workspace; sessions land in this Mac's own.
        return app_support.joinpath(parts[0], target_account, target_org, *parts[2:])
    return None


def _unreachable_cwds(org_dir: Path) -> list[str]:
    """Working directories a restored session points at that do not exist here."""
    if not org_dir.is_dir():
        return []
    missing: set[str] = set()
    for path in org_dir.glob("local_*.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        cwd = data.get("cwd")
        if isinstance(cwd, str) and cwd and not Path(cwd).is_dir():
            missing.add(cwd)
    return sorted(missing)
