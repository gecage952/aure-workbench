"""Where the experiment's reduced data comes from.

A :class:`DataSource` answers two questions and no others: *what files exist*
(:meth:`~DataSource.inventory`) and *what bytes does this file hold*
(:meth:`~DataSource.read_bytes`). Everything else -- when a run counts as
complete, whether it may be copied, how a copy is made safely -- is decided
once, outside, so that every source is held to the same rules.

Only the local folder exists today. A Tiled source implements the same three
methods; see ``docs/experiment-sources.md``. It has to hand back the *original*
bytes under the *original* name: the header carries the angle in radians and
the dQ convention, and the ``new_reduction`` dialect records the segment number
only in the filename.
"""

from __future__ import annotations

from typing import Any, Protocol

from nr_workbench.experiment.inventory import Inventory, SourceFile

#: Seconds anyone -- a page request, a folder check, the command line -- waits
#: for the data source before giving up on it. A mount that has gone away
#: blocks rather than failing, so without a deadline it would wait for good.
SOURCE_TIMEOUT = 15.0


class SourceUnavailableError(Exception):
    """The configured source cannot be used. The message says why and what to do."""


class SourceChangedError(Exception):
    """A file changed between being listed and being read."""


class DataSource(Protocol):
    """Where reduced data comes from.

    Attributes:
        kind: The name used in ``[experiment.source] kind``.
    """

    kind: str

    def describe(self) -> dict[str, Any]:
        """What the page shows about the source: kind, location, reachability."""
        ...

    def inventory(self) -> Inventory:
        """Every run the source can see now. Never raises for missing data."""
        ...

    def read_bytes(self, file: SourceFile) -> bytes:
        """The exact bytes of one listed file.

        Raises:
            SourceChangedError: If the file is no longer the version listed.
            OSError: If it cannot be read.
        """
        ...


def open_source(config: Any, *, ipts: str | None) -> DataSource:
    """Build the source ``[experiment.source]`` describes.

    Args:
        config: A :class:`~nr_workbench.experiment.config.SourceConfig`.
        ipts: The project's IPTS, to notice a file from another experiment.

    Returns:
        The source.

    Raises:
        SourceUnavailableError: For a kind that is planned but not built, or
            not known at all. Deliberately not a fallback to the local folder:
            that would quietly watch a path nobody chose.
    """
    from nr_workbench.project.experiment_schema import (
        PLANNED_SOURCE_KINDS,
        SOURCE_KINDS,
    )

    if config.kind == "local":
        from nr_workbench.experiment.sources.local import LocalDirectorySource

        return LocalDirectorySource(config.path, config.location, ipts=ipts)
    if config.kind in PLANNED_SOURCE_KINDS:
        raise SourceUnavailableError(
            f"[experiment.source] kind = {config.kind!r} is planned but not "
            "implemented yet. docs/experiment-sources.md describes how it will "
            'plug in; until then use kind = "local" on a machine with the data '
            "mount."
        )
    raise SourceUnavailableError(
        f"[experiment.source] kind = {config.kind!r} is not known. "
        f"Known: {', '.join(SOURCE_KINDS)}."
    )
