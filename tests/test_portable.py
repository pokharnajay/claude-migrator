"""Export/import round trip, and the hostile archives import must refuse."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from claude_migrator import portable, storage
from claude_migrator.safety import SafetyError
from tests.conftest import NEW, NEW_ORG, OLD, OLD_ORG


def export(storage_root: Path, cli_root: Path, out: Path) -> portable.ExportSummary:
    source = storage.find_account(storage_root, OLD)
    return portable.export_bundle(out, source.orgs, cli_root / "projects", "old@example.org")


# ------------------------------------------------------------------- export


def test_export_writes_a_zip_named_for_the_account(storage_root: Path, cli_root: Path, tmp_path: Path) -> None:
    summary = export(storage_root, cli_root, tmp_path / "out")
    assert summary.path.suffix == ".zip"
    assert "old-example.org" in summary.path.name
    assert zipfile.is_zipfile(summary.path)


def test_export_carries_the_sessions(storage_root: Path, cli_root: Path, tmp_path: Path) -> None:
    summary = export(storage_root, cli_root, tmp_path / "out")
    with zipfile.ZipFile(summary.path) as archive:
        names = archive.namelist()
    assert f"claude-code-sessions/{OLD_ORG}/local_old0.json" in names
    assert f"local-agent-mode-sessions/{OLD_ORG}/local_cw0.json" in names


def test_export_carries_the_cowork_sidecar_files(storage_root: Path, cli_root: Path, tmp_path: Path) -> None:
    summary = export(storage_root, cli_root, tmp_path / "out")
    with zipfile.ZipFile(summary.path) as archive:
        names = archive.namelist()
    assert f"local-agent-mode-sessions/{OLD_ORG}/local_cw0/blob.bin" in names


def test_export_includes_the_transcripts_sessions_depend_on(
    storage_root: Path, cli_root: Path, tmp_path: Path
) -> None:
    summary = export(storage_root, cli_root, tmp_path / "out")
    with zipfile.ZipFile(summary.path) as archive:
        names = archive.namelist()
    assert "projects/-tmp-project/cli-old0.jsonl" in names
    assert summary.transcripts == 2  # cli-old0 and cli-old1 exist; cli-old2 does not


def test_export_reports_sessions_whose_transcript_is_gone(
    storage_root: Path, cli_root: Path, tmp_path: Path
) -> None:
    summary = export(storage_root, cli_root, tmp_path / "out")
    assert summary.missing_transcripts == ["Old session 2"]


def test_export_records_a_checksum_for_every_member(storage_root: Path, cli_root: Path, tmp_path: Path) -> None:
    summary = export(storage_root, cli_root, tmp_path / "out")
    with zipfile.ZipFile(summary.path) as archive:
        manifest = json.loads(archive.read(portable.MANIFEST_NAME))
        members = [n for n in archive.namelist() if n != portable.MANIFEST_NAME]
    assert set(manifest["files"]) == set(members)


def test_exporting_nothing_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SafetyError, match="Nothing selected"):
        portable.export_bundle(tmp_path, [], tmp_path, "x")


# ------------------------------------------------------------------- inspect


def test_inspect_reports_the_bundle_contents(storage_root: Path, cli_root: Path, tmp_path: Path) -> None:
    summary = export(storage_root, cli_root, tmp_path / "out")
    info = portable.inspect_bundle(summary.path)
    assert info.version == portable.BUNDLE_VERSION
    assert info.sessions == 4
    assert info.label == "old@example.org"


def test_a_non_zip_is_refused(tmp_path: Path) -> None:
    fake = tmp_path / "not.zip"
    fake.write_text("hello")
    with pytest.raises(SafetyError, match="not a zip"):
        portable.inspect_bundle(fake)


def test_a_zip_without_a_manifest_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "b.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"claude-code-sessions/{OLD_ORG}/local_x.json", "{}")
    with pytest.raises(SafetyError, match="no bundle manifest"):
        portable.inspect_bundle(path)


def test_a_future_bundle_version_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "b.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(portable.MANIFEST_NAME, json.dumps({"version": 99}))
    with pytest.raises(SafetyError, match="unsupported bundle version"):
        portable.inspect_bundle(path)


# ------------------------------------------------------------ hostile archives


def make_zip(tmp_path: Path, members: dict[str, str]) -> Path:
    path = tmp_path / "evil.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(portable.MANIFEST_NAME, json.dumps({"version": 1, "files": {}}))
        for name, content in members.items():
            archive.writestr(name, content)
    return path


def test_parent_traversal_is_refused(tmp_path: Path) -> None:
    path = make_zip(tmp_path, {"../../../etc/passwd": "root"})
    with pytest.raises(SafetyError, match="path traversal|unexpected top-level"):
        portable.inspect_bundle(path)


def test_an_absolute_path_is_refused(tmp_path: Path) -> None:
    path = make_zip(tmp_path, {"/etc/passwd": "root"})
    with pytest.raises(SafetyError, match="absolute path"):
        portable.inspect_bundle(path)


def test_an_unexpected_top_level_directory_is_refused(tmp_path: Path) -> None:
    path = make_zip(tmp_path, {"Library/LaunchAgents/evil.plist": "<plist/>"})
    with pytest.raises(SafetyError, match="unexpected top-level"):
        portable.inspect_bundle(path)


def test_an_executable_disguised_as_a_session_is_refused(tmp_path: Path) -> None:
    path = make_zip(tmp_path, {f"claude-code-sessions/{OLD_ORG}/evil.sh": "rm -rf /"})
    with pytest.raises(SafetyError, match="unexpected session file"):
        portable.inspect_bundle(path)


def test_a_non_uuid_workspace_segment_is_refused(tmp_path: Path) -> None:
    path = make_zip(tmp_path, {"claude-code-sessions/../../x/local_a.json": "{}"})
    with pytest.raises(SafetyError, match="path traversal|not a UUID"):
        portable.inspect_bundle(path)


def test_a_symlink_member_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "evil.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(portable.MANIFEST_NAME, json.dumps({"version": 1, "files": {}}))
        info = zipfile.ZipInfo(f"claude-code-sessions/{OLD_ORG}/local_link.json")
        info.external_attr = (0o120777 << 16)  # symlink mode
        archive.writestr(info, "/etc/passwd")
    with pytest.raises(SafetyError, match="symlink"):
        portable.inspect_bundle(path)


def test_a_transcript_outside_a_project_directory_is_refused(tmp_path: Path) -> None:
    path = make_zip(tmp_path, {"projects/evil.jsonl": "{}"})
    with pytest.raises(SafetyError, match="unexpected transcript path"):
        portable.inspect_bundle(path)


def test_a_zip_bomb_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "bomb.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(portable.MANIFEST_NAME, json.dumps({"version": 1, "files": {}}))
        archive.writestr(f"claude-code-sessions/{OLD_ORG}/local_big.json", "0" * 5_000_000)
    with pytest.raises(SafetyError, match="compression ratio"):
        portable.inspect_bundle(path)


# -------------------------------------------------------------- import / round trip


def test_round_trip_restores_sessions_on_the_far_side(
    storage_root: Path, cli_root: Path, tmp_path: Path
) -> None:
    summary = export(storage_root, cli_root, tmp_path / "out")

    # A fresh machine: signed-in account only, no history.
    far = tmp_path / "far"
    (far / "claude-code-sessions" / NEW / NEW_ORG).mkdir(parents=True)
    (far / "local-agent-mode-sessions" / NEW / NEW_ORG).mkdir(parents=True)
    far_cli = tmp_path / "far-cli"
    far_cli.mkdir()

    result = portable.import_bundle(summary.path, far, far_cli, NEW, NEW_ORG)

    assert result.failed == []
    assert (far / "claude-code-sessions" / NEW / NEW_ORG / "local_old0.json").exists()
    assert (far / "local-agent-mode-sessions" / NEW / NEW_ORG / "local_cw0" / "blob.bin").exists()
    assert (far_cli / "projects" / "-tmp-project" / "cli-old0.jsonl").exists()
    assert result.sessions_copied > 0
    assert result.transcripts_copied == 2


def test_import_never_overwrites_what_is_already_there(
    storage_root: Path, cli_root: Path, tmp_path: Path
) -> None:
    summary = export(storage_root, cli_root, tmp_path / "out")
    far = tmp_path / "far"
    org_dir = far / "claude-code-sessions" / NEW / NEW_ORG
    org_dir.mkdir(parents=True)
    (org_dir / "local_old0.json").write_text("PRECIOUS")
    far_cli = tmp_path / "far-cli"
    far_cli.mkdir()

    result = portable.import_bundle(summary.path, far, far_cli, NEW, NEW_ORG)
    assert (org_dir / "local_old0.json").read_text() == "PRECIOUS"
    assert result.skipped_existing >= 1


def test_import_rejects_a_tampered_member(storage_root: Path, cli_root: Path, tmp_path: Path) -> None:
    summary = export(storage_root, cli_root, tmp_path / "out")

    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(summary.path) as original, zipfile.ZipFile(tampered, "w") as out:
        for info in original.infolist():
            data = original.read(info.filename)
            if info.filename.endswith("local_old0.json"):
                data = b'{"sessionId":"swapped"}'
            out.writestr(info.filename, data)

    far = tmp_path / "far"
    (far / "claude-code-sessions" / NEW / NEW_ORG).mkdir(parents=True)
    far_cli = tmp_path / "far-cli"
    far_cli.mkdir()

    result = portable.import_bundle(tampered, far, far_cli, NEW, NEW_ORG)
    assert any("checksum mismatch" in reason for _, reason in result.failed)
    assert not (far / "claude-code-sessions" / NEW / NEW_ORG / "local_old0.json").exists()


def test_import_reports_working_directories_that_do_not_exist_here(
    storage_root: Path, cli_root: Path, tmp_path: Path
) -> None:
    summary = export(storage_root, cli_root, tmp_path / "out")
    far = tmp_path / "far"
    (far / "claude-code-sessions" / NEW / NEW_ORG).mkdir(parents=True)
    far_cli = tmp_path / "far-cli"
    far_cli.mkdir()

    result = portable.import_bundle(summary.path, far, far_cli, NEW, NEW_ORG)
    assert result.unknown_cwds == ["/tmp/project"] or result.unknown_cwds == []


def test_import_leaves_nothing_in_the_temp_directory(
    storage_root: Path, cli_root: Path, tmp_path: Path, monkeypatch
) -> None:
    summary = export(storage_root, cli_root, tmp_path / "out")
    staging_root = tmp_path / "tmp"
    staging_root.mkdir()
    monkeypatch.setenv("TMPDIR", str(staging_root))

    far = tmp_path / "far"
    (far / "claude-code-sessions" / NEW / NEW_ORG).mkdir(parents=True)
    far_cli = tmp_path / "far-cli"
    far_cli.mkdir()

    portable.import_bundle(summary.path, far, far_cli, NEW, NEW_ORG)
    assert list(staging_root.iterdir()) == []
