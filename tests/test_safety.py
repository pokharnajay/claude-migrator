"""The guarantees the tool rests on: never overwrite, never escape, never tear."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from claude_migrator import safety
from claude_migrator.safety import SafetyError


# ------------------------------------------------------------------ creation


def test_copy_creates_a_missing_file(tmp_path: Path) -> None:
    src = tmp_path / "a.json"
    src.write_text("payload")
    assert safety.copy_file_exclusive(src, tmp_path / "out" / "a.json") is True
    assert (tmp_path / "out" / "a.json").read_text() == "payload"


def test_copy_refuses_to_touch_an_existing_file(tmp_path: Path) -> None:
    src = tmp_path / "a.json"
    src.write_text("new")
    dst = tmp_path / "a-copy.json"
    dst.write_text("PRECIOUS")

    assert safety.copy_file_exclusive(src, dst) is False
    assert dst.read_text() == "PRECIOUS"


def test_copy_preserves_modification_time(tmp_path: Path) -> None:
    src = tmp_path / "a.json"
    src.write_text("x")
    os.utime(src, (1_700_000_000, 1_700_000_000))
    safety.copy_file_exclusive(src, tmp_path / "b.json")
    assert int((tmp_path / "b.json").stat().st_mtime) == 1_700_000_000


def test_a_failed_copy_leaves_no_partial_file(tmp_path: Path, monkeypatch) -> None:
    src = tmp_path / "a.json"
    src.write_text("payload")
    dst = tmp_path / "b.json"

    def boom(*args, **kwargs):
        raise OSError("disk died mid-copy")

    monkeypatch.setattr(safety.shutil, "copyfileobj", boom)
    with pytest.raises(OSError):
        safety.copy_file_exclusive(src, dst)
    assert not dst.exists()


def test_directory_copy_refuses_an_existing_destination(tmp_path: Path) -> None:
    src = tmp_path / "src"
    (src / "inner").mkdir(parents=True)
    (src / "inner" / "f.bin").write_bytes(b"data")
    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "keep.txt").write_text("keep me")

    assert safety.copy_tree_exclusive(src, dst) is False
    assert (dst / "keep.txt").read_text() == "keep me"


def test_directory_copy_moves_a_complete_tree_into_place(tmp_path: Path) -> None:
    src = tmp_path / "src"
    (src / "inner").mkdir(parents=True)
    (src / "inner" / "f.bin").write_bytes(b"data")

    assert safety.copy_tree_exclusive(src, tmp_path / "dst") is True
    assert (tmp_path / "dst" / "inner" / "f.bin").read_bytes() == b"data"


def test_directory_copy_cleans_up_its_staging_area(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "f").write_text("x")
    safety.copy_tree_exclusive(src, tmp_path / "dst")
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".migrator-")]
    assert leftovers == []


# -------------------------------------------------------------- atomic write


def test_atomic_write_replaces_content(tmp_path: Path) -> None:
    target = tmp_path / "idx"
    target.write_text("old")
    safety.write_atomic(target, "new")
    assert target.read_text() == "new"


def test_atomic_write_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    safety.write_atomic(tmp_path / "idx", "data")
    assert [p.name for p in tmp_path.iterdir()] == ["idx"]


def test_atomic_write_keeps_the_old_file_when_it_fails(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "idx"
    target.write_text("original")
    monkeypatch.setattr(safety.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("nope")))
    with pytest.raises(OSError):
        safety.write_atomic(target, "replacement")
    assert target.read_text() == "original"
    assert [p.name for p in tmp_path.iterdir()] == ["idx"]


# ---------------------------------------------------------------- containment


def test_a_path_inside_the_root_is_contained(tmp_path: Path) -> None:
    assert safety.is_contained(tmp_path / "a" / "b", tmp_path)


def test_a_parent_escape_is_not_contained(tmp_path: Path) -> None:
    assert not safety.is_contained(tmp_path / ".." / "elsewhere", tmp_path)


def test_a_symlink_pointing_outside_is_not_contained(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    link = root / "local_evil.json"
    link.symlink_to(outside)
    assert not safety.is_contained(link, root)


def test_require_contained_raises_for_an_escape(tmp_path: Path) -> None:
    with pytest.raises(SafetyError, match="resolves outside"):
        safety.require_contained(tmp_path.parent / "x", tmp_path, "entry")


# --------------------------------------------------------------------- space


def test_space_check_passes_for_a_small_copy(tmp_path: Path) -> None:
    safety.require_free_space(tmp_path, 1024)


def test_space_check_refuses_an_impossible_copy(tmp_path: Path) -> None:
    with pytest.raises(SafetyError, match="Not enough free space"):
        safety.require_free_space(tmp_path, 2**60)


def test_space_check_works_for_a_directory_not_yet_created(tmp_path: Path) -> None:
    safety.require_free_space(tmp_path / "does" / "not" / "exist", 1024)


# -------------------------------------------------------------- verification


def test_verification_accepts_a_complete_copy(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a").write_text("aaa")
    dst = tmp_path / "dst"
    safety.copy_tree_exclusive(src, dst)
    ok, _ = safety.verify_copy(src, dst)
    assert ok


def test_verification_rejects_a_truncated_copy(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a").write_text("aaa")
    (src / "b").write_text("bbb")
    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "a").write_text("aaa")

    ok, detail = safety.verify_copy(src, dst)
    assert not ok
    assert "1 of 2 files" in detail


def test_verification_rejects_a_missing_copy(tmp_path: Path) -> None:
    ok, detail = safety.verify_copy(tmp_path, tmp_path / "nope")
    assert not ok
    assert "missing" in detail


# ---------------------------------------------------------------------- lock


def test_lock_blocks_a_second_holder(tmp_path: Path) -> None:
    lock = tmp_path / "lock"
    with safety.SingleInstance(lock):
        with pytest.raises(SafetyError, match="already running"):
            safety.SingleInstance(lock).__enter__()


def test_lock_is_released_on_exit(tmp_path: Path) -> None:
    lock = tmp_path / "lock"
    with safety.SingleInstance(lock):
        pass
    with safety.SingleInstance(lock):
        pass  # acquiring twice in sequence must work


def test_a_lock_left_by_a_dead_process_is_reclaimed(tmp_path: Path) -> None:
    lock = tmp_path / "lock"
    lock.write_text("999999")  # a pid that cannot be running
    with safety.SingleInstance(lock):
        assert lock.read_text() == str(os.getpid())
