"""Reduced files copied in from Tiled, when the data mount is out of reach.

The experiment's ``autoreduction/`` on Tiled holds the files the reduction
wrote into ``new_reduction/``, one ``bytes`` node per file, named as the file
(another process registers them). A pull copies each one the data folder
lacks into that folder, and nothing else: a file the folder holds already is
left as it is, since a size or a time would not tell a re-reduction from the
same file. The folder watcher does the rest, as for any file that appears.

Only names the reduction writes are copied. Each is written under a hidden
name and renamed whole, so the watcher never reads half of one. Nothing is
written on Tiled.

Each file is fetched by the route the client's own ``raw_export`` takes, but
not through it: in tiled 0.2.18 that logs a failed download as a line of
output, rather than raising it, and draws a progress bar for every file.
"""

from __future__ import annotations

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


def autoreduction_of(experiment: Any) -> Any | None:
    """The experiment node's ``autoreduction/``; ``None`` when it has none, or
    none shared with this login: the server says the same for both."""
    try:
        return experiment[AUTOREDUCTION]
    except KeyError:
        return None


def plan_pull(
    autoreduction: Any, folder: Path | None, *, runs: Iterable[int] = ()
) -> tuple[list[Missing], int]:
    """What a pull would copy from *autoreduction* into *folder*.

    Args:
        autoreduction: The experiment's ``autoreduction/`` node.
        folder: The data folder; ``None`` when it cannot be worked out, and
            so holds nothing.
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
        if folder is not None and (folder / name).exists():
            held += 1
        else:
            missing.append(Missing(name, node))
    return sorted(missing, key=lambda m: m.name), held


def content_of(node: Any) -> bytes | None:
    """The one file a ``bytes`` node holds; ``None`` when it holds other than
    one file.

    Raises:
        httpx.HTTPError: The download failed, or the server refused it.
    """
    assets = [asset for source in node.data_sources() or () for asset in source.assets]
    if len(assets) != 1 or assets[0].is_directory:
        return None
    link = node.item["links"]["self"].replace("/metadata", "/asset/bytes", 1)
    response = node.context.http_client.get(
        link, params={"id": assets[0].id}, headers={"Accept-Encoding": "identity"}
    )
    response.raise_for_status()
    return response.content


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
        partial = folder / f".{item.name}.nrw-pull"
        try:
            data = content_of(item.node)
            if data is None:
                raise TiledSyncError(
                    f"{item.name} on Tiled is not one file: nothing of it was "
                    f"copied. {_before(written)}"
                )
            partial.write_bytes(data)
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
            partial.unlink(missing_ok=True)
        written.append(target)
    return written


def _before(written: list[Path]) -> str:
    return f"{len(written)} file(s) were copied before it; pull again to copy the rest."
