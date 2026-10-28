"""Builders and test doubles for the experiment package.

Two kinds of source are needed to test the seams honestly:

* **Real files** in a temporary folder -- the reference corpus (the old
  ``_partial.txt`` dialect, with its real headers and real reduction times) and
  synthetic ``new_reduction`` files shaped like the header in
  ``tests/test_header.py``, which is trimmed from run 234277.
* **In-memory doubles** for the source and the feed. A second implementation
  of each interface is what shows the interfaces are not secretly shaped like
  a directory -- and the doubles can do what a real folder cannot be made to
  do on demand: block, fail halfway, or report a clock from the future.
"""

from __future__ import annotations

import os
import shutil
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from nr_workbench.experiment.inventory import (
    Announcement,
    FeedUpdate,
    Inventory,
    SourceFile,
    SourceRun,
)
from nr_workbench.experiment.model import RunKey
from nr_workbench.experiment.sources import SourceChangedError

REFERENCE_STEADY = Path(__file__).parent / "data" / "reference" / "steady"

#: When the reference corpus's segments were actually reduced, as offsets in
#: seconds from the first. Read from their `# Reduction time:` lines: the three
#: segments of 218386 landed 15.5 and then 52.4 minutes apart.
REFERENCE_REDUCED_AT = {
    "REFL_218386_1_218386_partial.txt": 0,
    "REFL_218386_2_218387_partial.txt": 931,
    "REFL_218386_3_218388_partial.txt": 4073,
    "REFL_218393_1_218393_partial.txt": 13247,
    "REFL_218393_2_218394_partial.txt": 14141,
    "REFL_218393_3_218395_partial.txt": 17083,
}

_ANGLES = (0.45, 1.251, 3.5, 0.6)


def reduced_rows(n: int = 40, scale: float = 1.0) -> str:
    """Four plausible reduced columns: Q, R, dR, dQ."""
    q = np.linspace(0.01, 0.2, n)
    r = scale * 1e-3 * (0.01 / q) ** 4
    return "".join(
        f"{a:.6e} {b:.6e} {c:.6e} {d:.6e}\n"
        for a, b, c, d in zip(q, r, 0.05 * r, 0.02 * q, strict=True)
    )


def autoreduction_header(
    run: int, planned: int = 3, *, ipts: str = "IPTS-00001", stem: str = "S1_air"
) -> str:
    """A ``new_reduction`` header for *run*, sized for *planned* segments.

    Shaped like the one trimmed from run 234277 in tests/test_header.py: the
    per-segment arrays (``DB``, ``scale_factor``, ``ThetaShift``) come from
    the reduction template, and it carries no start time.
    """
    titles = ", ".join(f'"{stem}-{run}-{s}."' for s in range(1, planned + 1))
    angles = ", ".join(f"-{_ANGLES[s]:g}" for s in range(planned))
    db = ", ".join(f"'A{s}_Si.txt'" for s in range(1, planned + 1))
    zeros = ", ".join("0" for _ in range(planned))
    ones = ", ".join("1.0" for _ in range(planned))
    return (
        f'# Run Title: {{"title": [{titles}]}}\n'
        f"# DB = [{db}]\n"
        f'# Scaling factors = {{"scale_factor": [{ones}]}}\n'
        f'# Angles: {{"THS": [{angles}]}}\n'
        f'# Config: {{"experiment_id": "{ipts}", "ThetaShift": [{zeros}]}}\n'
        "# columns = Q, R, dR, dQ (sigma)\n"
    )


def write_autoreduced(
    folder: Path,
    run: int,
    segments: Iterable[int],
    *,
    planned: int = 3,
    ipts: str = "IPTS-00001",
    mtime: float | None = None,
    stem: str = "S1_air",
) -> list[Path]:
    """Write ``new_reduction`` segment files for one run."""
    from nr_workbench.instrument.reduced import AUTOREDUCTION_DIALECT, segment_filename

    folder.mkdir(parents=True, exist_ok=True)
    written = []
    header = autoreduction_header(run, planned, ipts=ipts, stem=stem)
    for segment in segments:
        path = folder / segment_filename(
            run, segment, run + segment - 1, AUTOREDUCTION_DIALECT
        )
        path.write_text(header + reduced_rows(scale=1 + segment / 10), encoding="utf-8")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        written.append(path)
    return written


def copy_reference(folder: Path, *, start: float | None = None) -> list[Path]:
    """Copy the reference partials into a flat *folder*.

    With *start*, each file's mtime is set to when it was really reduced,
    offset from *start*, so the real gaps between segments are reproduced.
    """
    folder.mkdir(parents=True, exist_ok=True)
    copied = []
    for source in sorted(REFERENCE_STEADY.glob("*.txt")):
        target = folder / source.name
        shutil.copy(source, target)
        if start is not None:
            stamp = start + REFERENCE_REDUCED_AT[source.name]
            os.utime(target, (stamp, stamp))
        copied.append(target)
    return copied


# ---------------------------------------------------------------------------
# In-memory doubles
# ---------------------------------------------------------------------------


@dataclass
class MemoryFile:
    """One file held by :class:`InMemorySource`."""

    data: bytes
    mtime: float
    version: int = 1


@dataclass
class InMemorySource:
    """A :class:`~nr_workbench.experiment.sources.DataSource` with no disk.

    Runs are described directly: tests say "run 7 has segments 1 and 2 of a
    planned 3" and get exactly that, with hooks for the things a real folder
    cannot do on demand.

    Attributes:
        files: Name to contents.
        planned: Run number to planned segment count, or absent if unknown.
        problems_for: Run number to the problems its listing should carry.
        block: When set, :meth:`inventory` waits on it -- a dead NFS mount.
        entered: Set whenever :meth:`inventory` is entered, before any
            blocking, so a test can wait for a listing to be under way.
        fail_reads_after: Raise ``OSError`` on the read after this many.
        reachable: Whether the listing succeeds at all.
    """

    kind: str = "memory"
    files: dict[str, MemoryFile] = field(default_factory=dict)
    planned: dict[int, int] = field(default_factory=dict)
    problems_for: dict[int, tuple[str, ...]] = field(default_factory=dict)
    block: threading.Event | None = None
    entered: threading.Event = field(default_factory=threading.Event)
    fail_reads_after: int | None = None
    reachable: bool = True
    inventories: int = 0
    reads: int = 0

    def add_segments(
        self,
        run: int,
        segments: Iterable[int],
        *,
        planned: int | None = 3,
        mtime: float = 0.0,
    ) -> None:
        """Add ``new_reduction`` files for *run*, with real header bytes."""
        from nr_workbench.instrument.reduced import (
            AUTOREDUCTION_DIALECT,
            segment_filename,
        )

        header = autoreduction_header(run, planned or 3)
        for segment in segments:
            name = segment_filename(
                run, segment, run + segment - 1, AUTOREDUCTION_DIALECT
            )
            self.files[name] = MemoryFile((header + reduced_rows()).encode(), mtime)
        if planned is not None:
            self.planned[run] = planned
        else:
            self.planned.pop(run, None)

    def add_combined(self, run: int, *, mtime: float = 0.0) -> str:
        """Add *run*'s combined curve, every segment stitched into one file."""
        from nr_workbench.instrument.reduced import combined_filename

        name = combined_filename(run)
        self.files[name] = MemoryFile(reduced_rows().encode(), mtime)
        return name

    def touch(self, name: str, mtime: float) -> None:
        """Rewrite a file: new version, new mtime."""
        entry = self.files[name]
        entry.version += 1
        entry.mtime = mtime

    def describe(self) -> dict[str, Any]:
        return {"kind": self.kind, "location": "memory", "path": None}

    def inventory(self) -> Inventory:
        from nr_workbench.arrival import fingerprint_entries
        from nr_workbench.instrument.reduced import ReducedName, canonical_name

        self.inventories += 1
        self.entered.set()
        if self.block is not None:
            self.block.wait()
        if not self.reachable:
            from nr_workbench.problems import Problem

            return Inventory(reachable=False, problems=(Problem("source", "gone"),))

        grouped: dict[int, list[SourceFile]] = {}
        for name, entry in sorted(self.files.items()):
            parsed = canonical_name(name)
            if parsed is None:
                continue
            common = {
                "name": name,
                "size": len(entry.data),
                "mtime": entry.mtime,
                "version": f"v{entry.version}:{entry.mtime}",
            }
            if isinstance(parsed, ReducedName):
                file = SourceFile(
                    **common,
                    segment=parsed.segment,
                    subrun=parsed.subrun,
                    dialect=parsed.dialect,
                )
                run = parsed.run
            else:
                # A combined curve, as the local folder lists one.
                file, run = SourceFile(**common), parsed
            grouped.setdefault(run, []).append(file)
        runs = {}
        for run, files in grouped.items():
            # Segments in order, then any combined curve: as the folder does.
            files.sort(key=lambda f: (f.segment is None, f.segment or 0))
            runs[RunKey(run)] = SourceRun(
                key=RunKey(run),
                files=tuple(files),
                title=f"S1_air-{run}-1.",
                n_segments=self.planned.get(run),
                changed_at=max(f.mtime for f in files),
                fingerprint=fingerprint_entries(
                    [(f.name, f.size, int(f.mtime * 1e9)) for f in files]
                ),
                problems=self.problems_for.get(run, ()),
            )
        return Inventory(runs=runs)

    def read_bytes(self, file: SourceFile) -> bytes:
        self.reads += 1
        if self.fail_reads_after is not None and self.reads > self.fail_reads_after:
            raise OSError("simulated failure mid-copy")
        entry = self.files.get(file.name)
        if entry is None or f"v{entry.version}:{entry.mtime}" != file.version:
            raise SourceChangedError(f"{file.name} changed")
        return entry.data


@dataclass
class InMemoryFeed:
    """A :class:`~nr_workbench.experiment.feeds.RunFeed` that says what it is told.

    Stands in for the web monitor: it can announce a run whose data does not
    exist yet.
    """

    kind: str = "memory"
    announced: list[Announcement] = field(default_factory=list)
    polls: int = 0

    def announce(self, *runs: int, state: str = "") -> None:
        for run in runs:
            self.announced.append(Announcement(run=run, state=state))

    def describe(self) -> dict[str, Any]:
        return {"kind": self.kind}

    def poll(self, inventory: Inventory) -> FeedUpdate:
        del inventory  # a remote feed knows what it knows
        self.polls += 1
        return FeedUpdate(announcements=tuple(self.announced))


def scan_threads() -> set[threading.Thread]:
    """The background pollers running now, known by the name their thread has."""
    return {
        t
        for t in threading.enumerate()
        if t.name == "nrw-experiment-scan" and t.is_alive()
    }
