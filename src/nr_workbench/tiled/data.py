"""Reduced files copied in from Tiled, when the data mount is out of reach.

The experiment's ``autoreduction/`` on Tiled holds the files the reduction
wrote into ``new_reduction/``, one ``bytes`` node per file, named as the file
(another process registers them). A pull copies each one the data folder
lacks, or holds at another size, into that folder; the folder watcher does
the rest, as for any file that appears there.

Only names the reduction writes are copied, so nothing a node is called can
become a path here; each lands under a hidden name first and is renamed
whole, so the watcher never reads half a file. Nothing is written on Tiled.
"""

from __future__ import annotations

import io
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nr_workbench.tiled.server import TiledSyncError, said

#: Where the reduced files are, in the experiment's own node.
AUTOREDUCTION = "autoreduction"


@dataclass(frozen=True)
class Missing:
    """A file on Tiled the folder lacks, or holds at another size.

    Attributes:
        name: Its name, there and here.
        node: Its node on Tiled.
        again: Whether the folder holds it already, at another size: reduced
            again since it was copied.
    """

    name: str
    node: Any
    again: bool = False


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
) -> list[Missing]:
    """What a pull would copy from *autoreduction* into *folder*.

    Args:
        autoreduction: The experiment's ``autoreduction/`` node.
        folder: The data folder.
        runs: Only these runs; all of them when empty.

    Raises:
        TiledSyncError: It could not be listed.
    """
    import httpx

    wanted = set(runs)
    missing = []
    try:
        for name, node in autoreduction.items():
            run = run_of(name)
            if run is None or (wanted and run not in wanted):
                continue
            here = folder / name
            if not here.is_file():
                missing.append(Missing(name, node))
                continue
            size = _size(node)
            if size is not None and size != here.stat().st_size:
                missing.append(Missing(name, node, again=True))
    except httpx.HTTPError as exc:
        raise TiledSyncError(
            f"autoreduction/ could not be listed: {said(exc)}"
        ) from None
    return sorted(missing, key=lambda m: m.name)


def _size(node: Any) -> int | None:
    """A file node's size as Tiled registered it; ``None`` when it says none."""
    try:
        sources = node.item["attributes"].get("data_sources") or []
        sizes = [asset.get("size") for s in sources for asset in s.get("assets", [])]
    except (AttributeError, KeyError, TypeError):
        return None
    if len(sizes) == 1 and isinstance(sizes[0], int):
        return sizes[0]
    return None


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
        partial = folder / f".nrw-pull-{item.name}"
        try:
            buffers: dict[str, io.BytesIO] = {}
            item.node.raw_export(destination=buffers)
            if len(buffers) != 1:
                raise TiledSyncError(
                    f"{item.name} on Tiled is {len(buffers)} files, not one: "
                    f"nothing of it was copied. {_before(written)}"
                )
            (data,) = buffers.values()
            partial.write_bytes(data.getvalue())
            os.replace(partial, target)
        except httpx.HTTPError as exc:
            partial.unlink(missing_ok=True)
            raise TiledSyncError(
                f"{item.name} could not be downloaded: {said(exc)}. {_before(written)}"
            ) from None
        except OSError as exc:
            partial.unlink(missing_ok=True)
            raise TiledSyncError(
                f"{target} could not be written: {exc}. {_before(written)}"
            ) from None
        written.append(target)
    return written


def _before(written: list[Path]) -> str:
    return f"{len(written)} file(s) were copied before it; pull again to copy the rest."
