"""The shared lock and atomic write.

The scaffold lock, the fit index, the experiment catalog and each sample's copy
record all go through these, from request threads and terminals at once, so
what matters is what a reader or a second writer can observe.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from nr_workbench import fsutil
from nr_workbench.fsutil import advisory_lock, atomic_write_bytes


def test_atomic_write_bytes_replaces_and_leaves_nothing_behind(tmp_path: Path) -> None:
    target = tmp_path / "scaffold.lock.json"
    target.write_bytes(b"old\n")

    atomic_write_bytes(target, b"new\n")

    assert target.read_bytes() == b"new\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["scaffold.lock.json"]


def test_atomic_write_bytes_that_fails_keeps_the_old_file_whole(
    tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "sources.json"
    target.write_bytes(b"old\n")

    def failing(source, destination):
        raise OSError("disk full")

    monkeypatch.setattr(fsutil.os, "replace", failing)

    with pytest.raises(OSError, match="disk full"):
        atomic_write_bytes(target, b"new\n")

    assert target.read_bytes() == b"old\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["sources.json"]


def test_atomic_write_bytes_puts_its_temp_file_in_scratch(
    tmp_path: Path, monkeypatch
) -> None:
    """For a committed directory, where a stray temp file would be committed too."""
    committed, scratch = tmp_path / "experiment", tmp_path / "cache"
    committed.mkdir()
    seen: list[Path] = []
    real = os.replace

    def watching(source, destination):
        seen.append(Path(source))
        real(source, destination)

    monkeypatch.setattr(fsutil.os, "replace", watching)

    atomic_write_bytes(committed / "runs.parquet", b"x", scratch=scratch)

    assert [p.parent for p in seen] == [scratch]
    assert [p.name for p in committed.iterdir()] == ["runs.parquet"]


def test_two_writers_never_share_a_temp_file(tmp_path: Path, monkeypatch) -> None:
    """One shared ".tmp" name let two writers rename each other's half-written file."""
    names: list[str] = []
    real = os.replace

    def recording(source, destination):
        names.append(Path(source).name)
        real(source, destination)

    monkeypatch.setattr(fsutil.os, "replace", recording)
    target = tmp_path / "scaffold.lock.json"

    atomic_write_bytes(target, b"a")
    atomic_write_bytes(target, b"b")

    assert len(set(names)) == 2


@pytest.mark.skipif(os.name == "nt", reason="no fcntl on Windows")
def test_advisory_lock_excludes_a_second_holder(tmp_path: Path) -> None:
    held, attempted, acquired = threading.Event(), threading.Event(), []
    guarded = tmp_path / "catalog"

    def second() -> None:
        attempted.set()
        with advisory_lock(guarded):
            acquired.append(held.is_set())

    with advisory_lock(guarded):
        other = threading.Thread(target=second)
        other.start()
        assert attempted.wait(timeout=5)
        other.join(timeout=0.2)
        assert other.is_alive() and acquired == []
        held.set()
    other.join(timeout=5)

    assert acquired == [True]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_atomic_write_bytes_keeps_the_files_mode(tmp_path: Path) -> None:
    """A group-writable file in a shared project stays group-writable."""
    target = tmp_path / "nrw.toml"
    target.write_bytes(b"old\n")
    target.chmod(0o664)
    old_umask = os.umask(0o077)
    try:
        atomic_write_bytes(target, b"new\n")
    finally:
        os.umask(old_umask)

    assert target.stat().st_mode & 0o777 == 0o664


# --------------------------------------------------------------------------
# A project another account can write: names nrw creates are never followed
# --------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_atomic_write_bytes_sets_permissions_on_the_open_file(
    tmp_path: Path, monkeypatch
) -> None:
    """By descriptor: a mode set by name acts on whatever the name is by then."""
    target = tmp_path / "nrw.toml"
    target.write_bytes(b"old\n")
    target.chmod(0o664)

    def by_name(*args, **kwargs):
        raise AssertionError("permissions set by name")

    monkeypatch.setattr(fsutil.os, "chmod", by_name)

    atomic_write_bytes(target, b"new\n")

    assert target.stat().st_mode & 0o777 == 0o664


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_a_mode_given_is_the_temp_files_before_it_holds_anything(
    tmp_path: Path, monkeypatch
) -> None:
    """A .env's keys are never, even for an instant, readable by others: the
    temp file is born with the mode, and the file is never looked up by name."""
    target = tmp_path / ".env"
    target.write_bytes(b"old\n")
    target.chmod(0o644)  # not the mode asked for: the one given wins
    monkeypatch.setattr(os, "umask", os.umask)  # restored whatever happens
    os.umask(0o022)
    seen: list[tuple[int, int]] = []
    real_fchmod = os.fchmod

    def fchmod(fd: int, mode: int) -> None:
        info = os.fstat(fd)
        seen.append((info.st_mode & 0o777, info.st_size))
        real_fchmod(fd, mode)

    real_stat = os.stat

    def stat(path, *args, **kwargs):
        if isinstance(path, (str, os.PathLike)) and os.fspath(path) == str(target):
            raise AssertionError("the target looked up by name")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(fsutil.os, "fchmod", fchmod)
    monkeypatch.setattr(fsutil.os, "stat", stat)

    atomic_write_bytes(target, b"LLM_API_KEY=x\n", mode=0o600)

    assert seen == [(0o600, 0)]  # born 0600, and set before any byte is in it
    assert real_stat(target).st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_atomic_write_bytes_keeps_no_setuid_setgid_or_sticky_bit(
    tmp_path: Path,
) -> None:
    target = tmp_path / "nrw.toml"
    target.write_bytes(b"old\n")
    target.chmod(0o4775)
    if not target.stat().st_mode & 0o4000:
        pytest.skip("this filesystem does not keep the setuid bit")

    atomic_write_bytes(target, b"new\n")

    assert target.stat().st_mode & 0o7777 == 0o775
