"""The experiment's ``context/`` on Tiled: what a push writes there, and reads.

``context/`` is a container in the experiment's own node. It holds ``runs/``
and ``samples/``, with one container per record: nrw's fields are its
metadata, and one spec names what it is. Its own metadata is a small header,
and the experiment's own context under ``experiment``.

A push writes the project's catalog there, the last writer winning (the
person's choice, 2026-10-27): a record whose fields differ is replaced, one
that matches is left alone, and fields another program keeps on a record
stay. Nothing else is touched: a record only Tiled holds, another program's
node, anything outside ``context/``. Whatever is made takes its parent's
access tags, because a node made without them is its maker's alone, unseen
by everyone else on the experiment; a parent with none is refused.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from nr_workbench.experiment.model import Catalog, RunKey
from nr_workbench.tiled import server as server_module
from nr_workbench.tiled.server import TiledSyncError, tags_of

#: The containers a push writes, in the experiment's own node.
CONTEXT = "context"
RUNS = "runs"
SAMPLES = "samples"

#: Where in ``context/``'s own metadata the experiment's context is kept:
#: one key, so nrw's three fields sit apart from what other programs write
#: there.
EXPERIMENT = "experiment"

#: What each node says it is: a spec's name, and its fields' version.
CONTEXT_SPEC = ("context-experiment", "1")
RUN_SPEC = ("context-run", "1")
SAMPLE_SPEC = ("context-sample", "1")


@dataclass(frozen=True)
class Record:
    """One record, as a push writes it.

    Attributes:
        container: ``runs`` or ``samples``.
        key: Its name there: ``234277-steady``, or the sample's id.
        fields: nrw's fields of it, which are its metadata.
        spec: What it says it is.
    """

    container: str
    key: str
    fields: Mapping[str, Any]
    spec: tuple[str, str]

    @property
    def path(self) -> str:
        """``samples/S1``, as a person reads it."""
        return f"{self.container}/{self.key}"


@dataclass(frozen=True)
class Planned:
    """What a push does with one record: ``create``, ``replace`` or ``same``."""

    action: str
    record: Record


@dataclass
class Remote:
    """``context/`` as one read found it.

    Attributes:
        context: Its node; ``None`` when it is not made yet.
        containers: ``runs/`` and ``samples/``, each ``None`` when not made.
        records: Each record's metadata there, by container and key.
        others: What else ``context/`` holds, which nrw neither reads nor
            writes.
    """

    context: Any = None
    containers: dict[str, Any] = field(
        default_factory=lambda: {RUNS: None, SAMPLES: None}
    )
    records: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    others: list[str] = field(default_factory=list)


def run_key(key: RunKey) -> str:
    """A run's name on Tiled: ``234277-steady``. A key there holds no ``:``."""
    return f"{key.run}-{key.kind}"


def header(ipts: str) -> dict[str, Any]:
    """``context/``'s own metadata: whose it is, and who writes it."""
    return {"ipts": ipts, "written_by": "nr-workbench"}


@dataclass(frozen=True)
class ExperimentPlan:
    """What a push does with the experiment's own context.

    Attributes:
        action: ``create`` when Tiled has no copy of it, ``replace``,
            ``same``, or ``none`` when there is nothing of it to write.
        fields: The fields a push writes.
        changed: Those that differ from Tiled's copy.
    """

    action: str
    fields: Mapping[str, str] = field(default_factory=dict)
    changed: tuple[str, ...] = ()

    @property
    def writes(self) -> bool:
        """Whether a push writes anything for it."""
        return self.action in ("create", "replace")


def plan_experiment(catalog: Catalog, remote: Remote) -> ExperimentPlan:
    """What a push of *catalog*'s experiment context over *remote* would do."""
    from nr_workbench.experiment.model import EXPERIMENT_FIELDS

    if not catalog.experiment.rev:
        return ExperimentPlan("none")
    # A field cleared on the page is written as cleared.
    fields = {name: getattr(catalog.experiment, name) for name in EXPERIMENT_FIELDS}
    there = (
        dict(remote.context.metadata).get(EXPERIMENT)
        if remote.context is not None
        else None
    )
    if not isinstance(there, Mapping):
        return ExperimentPlan("create", fields, tuple(fields))
    changed = tuple(name for name, value in fields.items() if there.get(name) != value)
    return ExperimentPlan("replace" if changed else "same", fields, changed)


def records_of(catalog: Catalog) -> list[Record]:
    """The catalog's records as a push writes them: samples, then runs, so a
    reader never meets a run whose sample is not there yet."""
    from nr_workbench.experiment.store import run_fields, sample_fields

    # nrw's own fields alone: a column a newer nrw wrote is that nrw's to
    # send, and its type is not known to be JSON's.
    samples = [
        Record(SAMPLES, sample_id, sample_fields(entry), SAMPLE_SPEC)
        for sample_id, entry in sorted(catalog.samples.items())
    ]
    runs = [
        Record(RUNS, run_key(key), run_fields(entry), RUN_SPEC)
        for key, entry in sorted(catalog.runs.items())
    ]
    return samples + runs


def read_remote(experiment: Any) -> Remote:
    """``context/`` in the experiment's node *experiment*, read once."""
    try:
        context = experiment[CONTEXT]
    except KeyError:
        return Remote()
    remote = Remote(context=context)
    for name in (RUNS, SAMPLES):
        try:
            container = context[name]
        except KeyError:
            continue
        remote.containers[name] = container
        for key, child in container.items():
            remote.records[(name, key)] = dict(child.metadata)
    remote.others = sorted(k for k in context if k not in (RUNS, SAMPLES))
    return remote


def plan_push(catalog: Catalog, remote: Remote) -> list[Planned]:
    """What a push of *catalog* over *remote* would do, record by record."""
    planned = []
    for record in records_of(catalog):
        there = remote.records.get((record.container, record.key))
        if there is None:
            action = "create"
        elif all(there.get(k, _ABSENT) == v for k, v in record.fields.items()):
            action = "same"
        else:
            action = "replace"
        planned.append(Planned(action, record))
    return planned


_ABSENT = object()


def push(
    experiment: Any,
    remote: Remote,
    planned: list[Planned],
    *,
    ipts: str,
    context_plan: ExperimentPlan | None = None,
) -> list[Planned]:
    """Write what *planned* changes, in its order; make what it is written in.

    The experiment's own context is written first, into ``context/``'s own
    metadata: with the header when ``context/`` is made, or merged over what
    is there, so another program's keys stay.

    Args:
        experiment: The experiment's own node.
        remote: ``context/`` as it was read for *planned*.
        planned: From :func:`plan_push`.
        ipts: The experiment's IPTS, for ``context/``'s header.
        context_plan: From :func:`plan_experiment`; nothing is written for the
            experiment's context without one.

    Returns:
        What was written, in order.

    Raises:
        TiledSyncError: A node to make something in has no access tags, or a
            write failed. The message names what was written before it: a
            push run again writes only what is still different.
    """
    import httpx

    Spec = server_module.library().Spec  # noqa: N806 - the client's class
    written: list[Planned] = []
    where = "context/"
    try:
        writes = context_plan is not None and context_plan.writes
        own = dict(context_plan.fields) if writes else None
        context, context_tags = remote.context, None
        if context is None:
            context_tags = _given(experiment, "The experiment's own node")
            context = experiment.create_container(
                CONTEXT,
                metadata={**header(ipts), **({EXPERIMENT: own} if writes else {})},
                specs=[Spec(*CONTEXT_SPEC)],
                access_tags=context_tags,
            )
        elif writes:
            metadata = dict(context.metadata)
            there = metadata.get(EXPERIMENT)
            there = dict(there) if isinstance(there, Mapping) else {}
            # nrw's fields over Tiled's; a key another program keeps there stays.
            context.replace_metadata(
                metadata={**metadata, EXPERIMENT: {**there, **own}}
            )
        containers: dict[str, tuple[Any, list[str] | None]] = {}
        for name in (RUNS, SAMPLES):
            node = remote.containers.get(name)
            if node is None:
                # Made with context/'s tags, as given or as read.
                tags = context_tags or _given(context, "context/")
                where = f"{name}/"
                node = context.create_container(name, access_tags=tags)
                containers[name] = (node, tags)
            else:
                containers[name] = (node, None)
        for item in planned:
            if item.action == "same":
                continue
            record = item.record
            where = record.path
            node, tags = containers[record.container]
            if item.action == "create":
                node.create_container(
                    record.key,
                    metadata=dict(record.fields),
                    specs=[Spec(*record.spec)],
                    access_tags=tags or _given(node, f"{record.container}/"),
                )
            else:
                there = node[record.key]
                # Fields another program keeps on it stay; nrw's are written
                # over theirs. Its specs and its tags are left as they are.
                there.replace_metadata(
                    metadata={**dict(there.metadata), **record.fields}
                )
            written.append(item)
    except httpx.HTTPError as exc:
        raise TiledSyncError(
            f"{where} could not be written: {exc}. {len(written)} record(s) "
            "were written before it; push again to write the rest."
        ) from None
    return written


def _given(node: Any, what: str) -> list[str]:
    """The tags what is made in *node* is given: its own.

    Raises:
        TiledSyncError: It has none, and what nrw made in it would be its
            maker's alone.
    """
    tags = tags_of(node)
    if not tags:
        raise TiledSyncError(
            f"{what} on Tiled has no access tags: what nrw made in it would be "
            "its maker's alone, unseen by everyone else on the experiment. "
            "Nothing more was made."
        )
    return tags
