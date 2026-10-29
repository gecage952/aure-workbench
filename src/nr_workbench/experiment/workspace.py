"""One project's experiment, wired together: configuration, catalog, source, feed.

The CLI and the web page both start here, so they read the same configuration
and report the same problems in the same words. Nothing here raises for a
missing or misconfigured piece: an experiment whose data mount is absent still
has a catalog worth showing, and one whose ``nrw.toml`` names an unbuilt
source still has samples worth applying from what was copied earlier.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nr_workbench.experiment.config import ExperimentConfig, experiment_config
from nr_workbench.experiment.inventory import FeedUpdate, Inventory
from nr_workbench.experiment.live import LiveInventory, Snapshot
from nr_workbench.problems import Problem


@dataclass
class _Unavailable:
    """Stands in for a source or feed that could not be built, and says why."""

    kind: str
    problem: Problem
    extra: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> dict[str, Any]:
        return {"kind": self.kind, "unavailable": self.problem.message, **self.extra}

    def inventory(self) -> Inventory:
        return Inventory(reachable=False, problems=(self.problem,))

    def poll(self, inventory: Inventory) -> FeedUpdate:
        del inventory
        return FeedUpdate(problems=(self.problem,))

    def read_bytes(self, file: Any) -> bytes:
        del file
        raise OSError(self.problem.message)


class _UnavailableStore:
    """Stands in for a catalog store that could not be opened.

    Every read and write raises, so the page shows the catalog as unreadable
    and refuses edits, rather than editing somewhere nobody else will look.
    """

    def __init__(self, root: Path, kind: str, reason: str) -> None:
        from nr_workbench.project.layout import ProjectLayout

        self.kind = kind
        self.reason = reason
        self.directory = ProjectLayout(root=root).experiment_dir

    def describe(self) -> dict[str, Any]:
        return {"kind": self.kind, "exists": False, "unavailable": self.reason}

    def exists(self) -> bool:
        return False

    def _refuse(self) -> Any:
        from nr_workbench.experiment.store import CatalogUnavailableError

        raise CatalogUnavailableError(self.reason)

    def load(self) -> Any:
        return self._refuse()

    def update(self, **kwargs: Any) -> Any:
        del kwargs
        return self._refuse()


class Workspace:
    """The experiment of one project.

    Args:
        root: Project root.
    """

    def __init__(self, root: Path) -> None:
        from nr_workbench.experiment.feeds import FeedUnavailableError, open_feed
        from nr_workbench.experiment.sources import SourceUnavailableError, open_source
        from nr_workbench.experiment.store import CatalogError, open_store
        from nr_workbench.project.config import ProjectConfigError, load_config

        self.root = Path(root).resolve()
        problems: list[Problem] = []
        try:
            project = load_config(self.root)
        except ProjectConfigError as exc:
            project = None
            problems.append(Problem("config", str(exc)))
        self.config: ExperimentConfig = experiment_config(project)
        problems.extend(self.config.problems)

        try:
            self.store: Any = open_store(self.root, self.config.catalog_kind)
        except CatalogError as exc:
            self.store = _UnavailableStore(
                self.root, self.config.catalog_kind, str(exc)
            )
            problems.append(Problem("catalog", str(exc)))

        try:
            self.source: Any = open_source(self.config.source, ipts=self.config.ipts)
        except SourceUnavailableError as exc:
            self.source = _Unavailable(
                self.config.source.kind, Problem("source", str(exc))
            )
        try:
            self.feed: Any = open_feed(self.config.feed)
        except FeedUnavailableError as exc:
            self.feed = _Unavailable(self.config.feed.kind, Problem("feed", str(exc)))

        self.problems = tuple(problems)

    def live(self, **kwargs: Any) -> LiveInventory:
        """A live inventory over this experiment's source and feed."""
        return LiveInventory(
            self.source,
            self.feed,
            settle_seconds=self.config.source.settle_seconds,
            poll_seconds=self.config.feed.poll_seconds,
            **kwargs,
        )

    def observe(self) -> Snapshot:
        """Poll once, now, and return what was seen. For the command line.

        A single poll can still judge settling correctly: quiet time is read
        from the files' own modification times, not from earlier polls.
        """
        return self.live(clock=time.time, autostart=False).scan_once()

    def render_context(self) -> Any:
        """The project's render context, for planning sample files."""
        from nr_workbench.experiment.render import project_context

        return project_context(self.root)


def candidate_source(
    root: Path, changes: Mapping[str, Any]
) -> tuple[Any, ExperimentConfig, list[str]]:
    """The data source a settings change would configure, built as the watcher would.

    For "Check folder": the change is checked, made to a copy of what
    ``nrw.toml`` says, and resolved through the same configuration and the
    same source registry the running server uses -- so a check reads exactly
    the folder the watcher would, and no other.

    Args:
        root: Project root.
        changes: Setting name to value, as the Settings page sends them.

    Returns:
        The source, the configuration it came from, and warnings.

    Raises:
        SettingsError: A value is not allowed, or the location cannot be
            worked out (a ``{ipts}`` with no IPTS).
        SourceUnavailableError: The source kind is not built yet.
        ProjectConfigError: ``nrw.toml`` cannot be read.
    """
    import dataclasses

    from nr_workbench.experiment.sources import open_source
    from nr_workbench.project.config import load_config
    from nr_workbench.project.settings import SettingsError, validate
    from nr_workbench.project.tomlfile import applied

    edits, warnings = validate(Path(root), changes)
    project = load_config(Path(root))
    raw = applied(project.raw, edits)
    beamtime = raw.get("beamtime") if isinstance(raw.get("beamtime"), dict) else {}
    config = experiment_config(
        dataclasses.replace(project, ipts=beamtime.get("ipts") or None, raw=raw)
    )
    if config.source.path is None:
        if "{ipts}" in config.source.location and not config.ipts:
            raise SettingsError(
                "This location is filled in from the IPTS; set the IPTS to check it."
            )
        why = next(
            (p.message for p in config.problems if p.scope == "source"),
            "The data location cannot be worked out.",
        )
        raise SettingsError(why)
    return open_source(config.source, ipts=config.ipts), config, warnings


def check_source(
    root: Path, changes: Mapping[str, Any], *, run: Callable[..., Any]
) -> dict[str, Any]:
    """What the folder a settings change names -- or the current one -- holds.

    The one check behind the Settings page's "Check folder" and ``nrw
    experiment settings --check``. A setting the change leaves out keeps the
    value ``nrw.toml`` gives it; only the IPTS, the source's kind and its
    location matter.

    Args:
        root: Project root.
        changes: Setting name to value, as :func:`candidate_source` takes them.
        run: Runs the probe, as ``run(probe)``. The caller gives it a deadline:
            the probe lists a folder that may be on a mount that stopped
            answering.

    Raises:
        SettingsError: The location is not one that could be saved or cannot
            be worked out, ``nrw.toml`` cannot be read, or the source kind
            cannot be checked yet.
    """
    from nr_workbench.experiment.sources import SourceUnavailableError
    from nr_workbench.project.config import ProjectConfigError
    from nr_workbench.project.settings import SettingsError

    wanted = {
        k: v
        for k, v in changes.items()
        if k in ("ipts", "source.kind", "source.location")
    }
    try:
        source, config, warnings = candidate_source(root, wanted)
    except (ProjectConfigError, SourceUnavailableError) as exc:
        raise SettingsError(str(exc)) from exc
    probe = getattr(source, "probe", None)
    if probe is None:
        raise SettingsError(f"A {source.kind} source cannot be checked yet.")
    found = run(probe)
    return {
        "location": config.source.location,
        "path": str(config.source.path),
        **found.as_dict(),
        "warnings": warnings,
    }
