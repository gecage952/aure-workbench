"""Reduced files copied in from Tiled, when the data mount is out of reach.

The experiment's ``autoreduction/`` on Tiled holds the files the reduction
wrote into ``new_reduction/``, one ``bytes`` node per file, named as the file
(another process registers them). A pull copies each one the data folder
lacks into that folder, and nothing else: a file the folder holds already is
left as it is, since a size or a time would not tell a re-reduction from the
same file. The folder watcher does the rest, as for any file that appears.

Only names the reduction writes are copied, so nothing a node is called can
become a path here. Each lands in a file of a random name, made new, and is
renamed whole: the watcher never reads half of one, and nothing planted in
the folder is written through. Nothing is written on Tiled.
"""

from __future__ import annotations

import io
import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nr_workbench.tiled.server import TiledSyncError, said

#: Where the reduced files are, in the experiment's own node.
AUTOREDUCTION = "autoreduction"


@dataclass(frozen=True)
class Missing:
    """A file on Tiled the folder lacks: its name, there and here, and node."""

    name: str
    node: Any


def run_of(name: str) -> int | None:
    """The run a reduced file's *name* belongs to; ``None`` when it is no name
    the reduction writes."""
    from nr_workbench.instrument.reduced import ReducedName, canonical_name

    parsed = canonical_name(name)
    if isinstance(parsed, ReducedName):
        return parsed.run
    return parsed


def plan_pull(
    autoreduction: Any, folder: Path, *, runs: Iterable[int] = ()
) -> tuple[list[Missing], int]:
    """What a pull would copy from *autoreduction* into *folder*.

    Args:
        autoreduction: The experiment's ``autoreduction/`` node.
        folder: The data folder.
        runs: Only these runs; all of them when empty.

    Returns:
        The files to copy, by name; and how many the folder holds already.
    """
    wanted = set(runs)
    missing, held = [], 0
    for name, node in autoreduction.items():
        run = run_of(name)
        if run is None or (wanted and run not in wanted):
            continue
        if (folder / name).exists():
            held += 1
        else:
            missing.append(Missing(name, node))
    return sorted(missing, key=lambda m: m.name), held


def pull(missing: Iterable[Missing], folder: Path) -> list[Path]:
    """Copy each of *missing* into *folder*, whole.

    Returns:
        The files written, in order.

    Raises:
        TiledSyncError: *folder* is not a directory nrw may write, or a file
            could not be downloaded. The message names what was copied
            before it: a pull run again copies only what is still missing.
    """
    import httpx

    if not folder.is_dir() or not os.access(folder, os.W_OK | os.X_OK):
        raise TiledSyncError(
            f"{folder} is not a folder nrw can write: create it, or point "
            "[experiment.source] location at one of yours."
        )
    written: list[Path] = []
    for item in missing:
        target = folder / item.name
        partial: Path | None = None
        try:
            buffers: dict[str, io.BytesIO] = {}
            item.node.raw_export(destination=buffers)
            if len(buffers) != 1:
                raise TiledSyncError(
                    f"{item.name} on Tiled is {len(buffers)} files, not one: "
                    f"nothing of it was copied. {_before(written)}"
                )
            (data,) = buffers.values()
            # Made new, under a name no one could have planted: written
            # through nothing, then renamed over the target whole.
            handle, name = tempfile.mkstemp(dir=folder, prefix=".nrw-pull-")
            partial = Path(name)
            with os.fdopen(handle, "wb") as out:
                out.write(data.getvalue())
            umask = os.umask(0)
            os.umask(umask)
            partial.chmod(0o666 & ~umask)
            os.replace(partial, target)
        except httpx.HTTPError as exc:
            raise TiledSyncError(
                f"{item.name} could not be downloaded: {said(exc)}. {_before(written)}"
            ) from None
        except OSError as exc:
            raise TiledSyncError(
                f"{target} could not be written: {exc}. {_before(written)}"
            ) from None
        finally:
            if partial is not None:
                partial.unlink(missing_ok=True)
        written.append(target)
    return written


def _before(written: list[Path]) -> str:
    return f"{len(written)} file(s) were copied before it; pull again to copy the rest."
