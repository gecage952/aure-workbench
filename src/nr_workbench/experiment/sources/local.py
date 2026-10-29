"""The data source for a machine with the data mount: one folder, polled.

Polling, not filesystem events: the folder is on NFS, where inotify is a
suggestion, and the reduction takes minutes anyway -- the same reasoning, and
the same conclusion, as ``nrw agent watch``.

Two things this is careful about:

* **One listing per poll.** Every file's size and times come from that single
  ``scandir``; nothing is stat'ed twice, and headers are re-read only when a
  file's identity changes.
* **Only canonical names count.** A name the reduction would not have written
  exactly is reported, not used -- see
  :func:`nr_workbench.instrument.reduced.canonical_name`.
"""

from __future__ import annotations

import contextlib
import os
import threading
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nr_workbench.arrival import fingerprint_entries, segment_problems
from nr_workbench.experiment.inventory import Inventory, Probe, SourceFile, SourceRun
from nr_workbench.experiment.model import RunKey, clean_title_snapshot
from nr_workbench.experiment.sources import SourceChangedError
from nr_workbench.instrument.reduced import (
    STEADY_SUFFIXES,
    ReducedName,
    canonical_name,
    parse_combined_name,
    parse_segment_name,
)
from nr_workbench.problems import Problem
from nr_workbench.project.experiment_schema import normalize_ipts

#: How many file headers to remember between polls. A beamtime folder holds a
#: few thousand files; this keeps every one of them without growing forever.
HEADER_CACHE_SIZE = 8192


@dataclass
class _Listing:
    """One listing of the folder, sorted: each run's files, and the rest.

    Attributes:
        reachable: Whether the folder could be listed at all.
        files: Each run's files, with the stat the listing gave them.
        unrecognized: Data files that match no reduced-file name.
        other: How many other files there are: plots, JSON, XML.
        problems: What is wrong, run by run or with the folder.
    """

    reachable: bool
    files: dict[int, list[tuple[SourceFile, os.stat_result]]] = field(
        default_factory=dict
    )
    unrecognized: list[str] = field(default_factory=list)
    other: int = 0
    problems: list[Problem] = field(default_factory=list)


class LocalDirectorySource:
    """Reduced files in one directory on this machine.

    Args:
        path: The folder, resolved. ``None`` when the configuration could not
            produce one; the inventory then says why.
        location: The location as configured, for display.
        ipts: The project's IPTS, to notice a file from another experiment.
    """

    kind = "local"

    def __init__(self, path: Path | None, location: str, *, ipts: str | None) -> None:
        self.path = Path(path) if path is not None else None
        self.location = location
        self.ipts = normalize_ipts(ipts)
        self._headers: OrderedDict[tuple[Any, ...], Any] = OrderedDict()
        self._lock = threading.Lock()

    def describe(self) -> dict[str, Any]:
        """Kind and location, for the page."""
        return {
            "kind": self.kind,
            "location": self.location,
            "path": str(self.path) if self.path is not None else None,
        }

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    def inventory(self) -> Inventory:
        """Every run in the folder, judged from one listing."""
        listed = self._list()
        if not listed.reachable:
            return Inventory(reachable=False, problems=tuple(listed.problems))
        runs = {
            RunKey(run): self._run(run, members)
            for run, members in sorted(listed.files.items())
        }
        return Inventory(
            runs=runs,
            unrecognized=tuple(listed.unrecognized),
            other_files=listed.other,
            reachable=True,
            problems=tuple(listed.problems),
        )

    def probe(self, *, header_runs: int = 5) -> Probe:
        """A quick look at the folder before choosing it: one listing, few headers.

        :meth:`inventory` reads the head of every file, which on a real
        beamtime folder over NFS is tens of megabytes -- a poller's job, done
        once and cached, not a button's. This lists the folder once and reads
        only the newest runs' headers, enough to show what is there and to
        notice a folder that belongs to another experiment.

        Args:
            header_runs: How many of the newest runs to read in full.
        """
        listed = self._list()
        if not listed.reachable:
            return Probe(reachable=False, problems=tuple(listed.problems))
        files, problems = listed.files, listed.problems
        numbers = sorted(files)
        newest = numbers[-header_runs:] if header_runs > 0 else []
        read = []
        for run in newest:
            members = sorted(files[run], key=lambda m: m[0].name)
            read.append(self._run(run, members))
        return Probe(
            reachable=True,
            runs=len(numbers),
            first=numbers[0] if numbers else None,
            last=numbers[-1] if numbers else None,
            newest=tuple(read),
            unrecognized=len(listed.unrecognized),
            other_files=listed.other,
            problems=tuple(problems),
        )

    def _list(self) -> _Listing:
        """One listing of the folder, sorted into runs, with what is wrong."""
        if self.path is None:
            return self._unreachable(
                "No data location is configured; see the configuration problems above."
            )
        try:
            with os.scandir(self.path) as listing:
                entries = list(listing)
        except FileNotFoundError:
            return self._unreachable(
                f"{self.path} does not exist. Is the data mount available on "
                "this machine? The location is set by [experiment.source] "
                "location in nrw.toml."
            )
        except NotADirectoryError:
            return self._unreachable(f"{self.path} is not a directory.")
        except PermissionError:
            return self._unreachable(f"{self.path} cannot be read by this account.")
        except OSError as exc:
            return self._unreachable(f"{self.path} cannot be listed: {exc}")

        listed = _Listing(reachable=True)
        noncanonical: list[str] = []
        problems = listed.problems

        for entry in sorted(entries, key=lambda e: e.name):
            name = entry.name
            data_like = Path(name).suffix in STEADY_SUFFIXES
            try:
                if not entry.is_file():
                    continue
                info = entry.stat()
            except OSError as exc:
                problems.append(Problem(f"source:{name}", f"cannot be read: {exc}"))
                continue
            if not data_like:
                listed.other += 1
                continue

            parsed = canonical_name(name)
            if parsed is None:
                loose = parse_segment_name(name) or parse_combined_name(name)
                (noncanonical if loose is not None else listed.unrecognized).append(
                    name
                )
                continue
            source_file = _source_file(name, info, parsed)
            run = parsed.run if isinstance(parsed, ReducedName) else parsed
            listed.files.setdefault(run, []).append((source_file, info))

        if noncanonical:
            problems.append(
                Problem(
                    "source",
                    f"{len(noncanonical)} file(s) are named almost, but not "
                    f"exactly, like reduced data (e.g. {noncanonical[0]!r}) and "
                    "are not used.",
                )
            )
        if listed.unrecognized:
            problems.append(
                Problem(
                    "source",
                    f"{len(listed.unrecognized)} data file(s) match no known "
                    f"reduced-file name, e.g. {listed.unrecognized[0]!r}. If this "
                    "is a new reduction format, instrument/reduced.py is where "
                    "nrw learns it.",
                )
            )
        return listed

    def _unreachable(self, message: str) -> _Listing:
        return _Listing(reachable=False, problems=[Problem("source", message)])

    def _run(
        self, run: int, members: list[tuple[SourceFile, os.stat_result]]
    ) -> SourceRun:
        """Assemble one run and say what is wrong with it, if anything."""
        from nr_workbench.project.scan import SteadyMeasurement

        members.sort(key=lambda m: (m[0].segment is None, m[0].segment or 0, m[0].name))
        problems: list[str] = []

        by_segment: dict[int, list[SourceFile]] = {}
        for source_file, _ in members:
            if source_file.segment is not None:
                by_segment.setdefault(source_file.segment, []).append(source_file)
        for segment, copies in sorted(by_segment.items()):
            if len(copies) > 1:
                problems.append(
                    f"segment {segment} is here twice "
                    f"({' and '.join(c.name for c in copies)}): the run was "
                    "reduced again by the other pipeline, and which one to use "
                    "is a question for a person."
                )
        if not problems and by_segment:
            measurement = SteadyMeasurement(
                run=run, partials={s: c[0].name for s, c in by_segment.items()}
            )
            held = segment_problems(measurement)
            if held:
                problems.append(held)

        headers = [(f, self._header(f)) for f, _ in members]
        readable = [(f, h) for f, h in headers if not isinstance(h, str)]
        for source_file, header in headers:
            if isinstance(header, str):
                problems.append(f"{source_file.name}: {header}")

        title = next((h.run_title for _, h in readable if h.run_title), "") or ""
        start = next((h.start_time for _, h in readable if h.start_time), "") or ""
        planned = sorted(
            {h.n_segments for _, h in readable if h.n_segments is not None}
        )
        n_segments = planned[0] if len(planned) == 1 else None
        if len(planned) > 1:
            problems.append(
                "the headers disagree about how many segments were planned "
                f"({', '.join(map(str, planned))})."
            )
        if n_segments is not None and by_segment and max(by_segment) > n_segments:
            problems.append(
                f"segment {max(by_segment)} is present, but the header plans "
                f"only {n_segments}."
            )

        experiments = {
            normalize_ipts(h.experiment) for _, h in readable if h.experiment
        }
        experiments.discard(None)
        if self.ipts and experiments and experiments != {self.ipts}:
            problems.append(
                f"the file header names {', '.join(sorted(experiments))}, but "
                f"this project is {self.ipts}. It may belong to another "
                "experiment."
            )

        # One per file, None where unknown: positions must line up with files.
        thetas = tuple(
            None if isinstance(h, str) or f.segment is None else h.theta
            for f, h in headers
        )
        return SourceRun(
            key=RunKey(run),
            files=tuple(f for f, _ in members),
            title=clean_title_snapshot(title),
            start_time=clean_title_snapshot(start),
            experiment=next(iter(sorted(experiments)), None),
            thetas=thetas,
            n_segments=n_segments,
            changed_at=max(info.st_mtime for _, info in members),
            fingerprint=fingerprint_entries(
                [(f.name, info.st_size, info.st_mtime_ns) for f, info in members]
            ),
            problems=tuple(problems),
        )

    def _header(self, source_file: SourceFile) -> Any:
        """The file's header, or the reason it could not be read. Cached.

        Opened the way :meth:`read_bytes` opens files, checking it is still
        the file listed.
        """
        from nr_workbench.instrument.header import (
            MAX_HEADER_BYTES,
            HeaderError,
            read_header_bytes,
        )

        key = (source_file.name, source_file.version)
        with self._lock:
            if key in self._headers:
                self._headers.move_to_end(key)
                return self._headers[key]
        assert self.path is not None
        path = self.path / source_file.name
        try:
            data = self._open_listed(source_file, MAX_HEADER_BYTES)
            value: Any = read_header_bytes(data, path)
        except HeaderError as exc:
            value = str(exc).split(": ", 1)[-1]
        except SourceChangedError as exc:
            value = str(exc)
        except OSError as exc:
            value = f"header cannot be read ({exc})"
        with self._lock:
            self._headers[key] = value
            while len(self._headers) > HEADER_CACHE_SIZE:
                self._headers.popitem(last=False)
        return value

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def read_bytes(self, file: SourceFile) -> bytes:
        """The exact bytes of a listed file, if it is still the same file.

        Raises:
            SourceChangedError: The file was rewritten, replaced or moved since
                it was listed, or while it was being read.
            OSError: It cannot be opened.
        """
        with self._opened(file) as (descriptor, before):
            # One byte more than listed, so a file still growing is noticed.
            data = _read_up_to(descriptor, before.st_size + 1)
            after = os.fstat(descriptor)
        if _version(after) != file.version or len(data) != before.st_size:
            raise SourceChangedError(f"{file.name} changed while it was being read.")
        return data

    def _open_listed(self, file: SourceFile, limit: int) -> bytes:
        """The first *limit* bytes of a listed file, if it is still that file."""
        with self._opened(file) as (descriptor, _):
            return _read_up_to(descriptor, limit)

    @contextlib.contextmanager
    def _opened(self, file: SourceFile) -> Iterator[tuple[int, os.stat_result]]:
        """Open a listed file, if it is still that file.

        Raises:
            SourceChangedError: It is not the version listed.
            OSError: It cannot be opened.
        """
        if self.path is None:
            raise SourceChangedError(f"{file.name} is not a file this source listed")
        descriptor = os.open(self.path / file.name, os.O_RDONLY)
        try:
            info = os.fstat(descriptor)
            if _version(info) != file.version:
                raise SourceChangedError(
                    f"{file.name} changed after it was listed; it is probably "
                    "being rewritten by the reduction."
                )
            yield (descriptor, info)
        finally:
            os.close(descriptor)


def _read_up_to(descriptor: int, limit: int) -> bytes:
    """Read at most *limit* bytes from an open descriptor."""
    chunks = []
    remaining = limit
    while remaining > 0:
        chunk = os.read(descriptor, min(65536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _version(info: os.stat_result) -> str:
    """Identity of one version of a file: which file, how big, when written."""
    return f"{info.st_ino}:{info.st_size}:{info.st_mtime_ns}:{info.st_ctime_ns}"


def _source_file(
    name: str, info: os.stat_result, parsed: ReducedName | int
) -> SourceFile:
    if isinstance(parsed, ReducedName):
        return SourceFile(
            name=name,
            size=info.st_size,
            mtime=info.st_mtime,
            version=_version(info),
            segment=parsed.segment,
            subrun=parsed.subrun,
            dialect=parsed.dialect,
        )
    return SourceFile(
        name=name, size=info.st_size, mtime=info.st_mtime, version=_version(info)
    )
