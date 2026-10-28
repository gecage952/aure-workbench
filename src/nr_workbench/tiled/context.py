"""The trees nrw writes in the experiment's node on Tiled, and the push.

``context/`` holds the catalog: ``runs/`` and ``samples/``, with one container
per record, whose metadata is nrw's fields and whose one spec names what it
is. Its own metadata is a small header, and the experiment's own context under
``experiment``. ``analysis/`` holds the fits, one container per sample with a
container per fit in it (:mod:`nr_workbench.tiled.analysis`).

A push writes a tree's records, the last writer winning: a record whose fields
differ is replaced, one that matches is left alone, and fields another program
keeps on a record stay. Nothing else is touched: a record only Tiled holds,
another program's node, anything outside the tree. Whatever is made takes its
parent's access tags, because a node made without them is its maker's alone,
unseen by everyone else on the experiment; a parent with none is refused.
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
class Tree:
    """One tree a push writes in the experiment's node.

    Attributes:
        name: Its container there.
        spec: What its container says it is.
        fixed: The containers made in it by any push that finds them missing.
            Any other a record is in is made with that record, and takes the
            spec ``part_spec`` and the metadata ``{part_key: its name}``.
        part_spec: What such a container says it is. A container in the tree
            that says otherwise is another program's, and is left alone.
        part_key: The metadata field that names such a container.
        ignore: The fields left out when a record is compared with Tiled's
            copy: who wrote it, and when, rather than what it says.
    """

    name: str
    spec: tuple[str, str]
    fixed: tuple[str, ...] = ()
    part_spec: tuple[str, str] | None = None
    part_key: str | None = None
    ignore: tuple[str, ...] = ()


#: ``context/``: the catalog's runs and samples. A record's revision is this
#: project's own count of its edits, so a record pulled from Tiled matches it
#: when what it says does.
CONTEXT_TREE = Tree(
    CONTEXT, CONTEXT_SPEC, fixed=(RUNS, SAMPLES), ignore=("rev", "updated_at")
)


@dataclass(frozen=True)
class Record:
    """One record, as a push writes it.

    Attributes:
        container: The container it is in: ``runs``, ``samples``, or a
            sample's in ``analysis/``.
        key: Its name there: ``234277-steady``, a sample's id, a fit's id.
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
    """What a push does with one record: ``create``, ``replace`` or ``same``.

    Attributes:
        action: What it does.
        record: The record.
        changed: For ``replace``, the fields that differ from Tiled's copy.
    """

    action: str
    record: Record
    changed: tuple[str, ...] = ()


@dataclass
class Remote:
    """A tree as one read found it.

    Attributes:
        node: Its container; ``None`` when it is not made yet.
        containers: The containers in it, by name.
        records: Each record's metadata there, by container and key.
        others: What else the tree holds, which nrw neither reads nor writes.
    """

    node: Any = None
    containers: dict[str, Any] = field(default_factory=dict)
    records: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    others: list[str] = field(default_factory=list)


def run_key(key: RunKey) -> str:
    """A run's name on Tiled: ``234277-steady``. A key there holds no ``:``."""
    return f"{key.run}-{key.kind}"


def header(ipts: str) -> dict[str, Any]:
    """A tree's own metadata: whose it is, and who writes it."""
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
        dict(remote.node.metadata).get(EXPERIMENT) if remote.node is not None else None
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


def read_remote(experiment: Any, tree: Tree = CONTEXT_TREE) -> Remote:
    """*tree* in the experiment's node *experiment*, read once."""
    try:
        node = experiment[tree.name]
    except KeyError:
        return Remote()
    remote = Remote(node=node)
    for name, container in node.items():
        if (tree.fixed and name not in tree.fixed) or (
            tree.part_spec and not _says(container, tree.part_spec)
        ):
            remote.others.append(name)
            continue
        remote.containers[name] = container
        for key, child in container.items():
            remote.records[(name, key)] = dict(child.metadata)
    remote.others.sort()
    return remote


def plan_push(
    records: list[Record], remote: Remote, tree: Tree = CONTEXT_TREE
) -> list[Planned]:
    """What a push of *records* over *remote* would do, record by record."""
    planned = []
    for record in records:
        there = remote.records.get((record.container, record.key))
        if there is None:
            planned.append(Planned("create", record))
            continue
        changed = tuple(
            name
            for name, value in record.fields.items()
            if name not in tree.ignore and there.get(name, _ABSENT) != value
        )
        planned.append(Planned("replace" if changed else "same", record, changed))
    return planned


_ABSENT = object()


def push(
    experiment: Any,
    remote: Remote,
    planned: list[Planned],
    *,
    ipts: str,
    tree: Tree = CONTEXT_TREE,
    context_plan: ExperimentPlan | None = None,
) -> list[Planned]:
    """Write what *planned* changes, in its order; make what it is written in.

    The experiment's own context is written first, into ``context/``'s own
    metadata: with the header when ``context/`` is made, or merged over what
    is there, so another program's keys stay.

    Args:
        experiment: The experiment's own node.
        remote: *tree* as it was read for *planned*.
        planned: From :func:`plan_push`.
        ipts: The experiment's IPTS, for the tree's header.
        tree: The tree written.
        context_plan: From :func:`plan_experiment`, for ``context/`` alone;
            nothing is written for the experiment's context without one.

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
    where = f"{tree.name}/"
    try:
        writes = context_plan is not None and context_plan.writes
        own = dict(context_plan.fields) if writes else None
        node, node_tags = remote.node, None
        if node is None:
            node_tags = _given(experiment, "The experiment's own node")
            node = experiment.create_container(
                tree.name,
                metadata={**header(ipts), **({EXPERIMENT: own} if writes else {})},
                specs=[Spec(*tree.spec)],
                access_tags=node_tags,
            )
        elif writes:
            metadata = dict(node.metadata)
            there = metadata.get(EXPERIMENT)
            there = dict(there) if isinstance(there, Mapping) else {}
            # nrw's fields over Tiled's; a key another program keeps there stays.
            node.replace_metadata(metadata={**metadata, EXPERIMENT: {**there, **own}})
        containers: dict[str, tuple[Any, list[str] | None]] = {
            name: (container, None) for name, container in remote.containers.items()
        }

        def container(name: str) -> tuple[Any, list[str] | None]:
            nonlocal where
            if name not in containers:
                # Made with the tree's tags, as given or as read.
                tags = node_tags or _given(node, f"{tree.name}/")
                where = f"{tree.name}/{name}/"
                part = name not in tree.fixed and tree.part_spec is not None
                made = node.create_container(
                    name,
                    metadata={tree.part_key: name} if part and tree.part_key else None,
                    specs=[Spec(*tree.part_spec)] if part and tree.part_spec else None,
                    access_tags=tags,
                )
                containers[name] = (made, tags)
            return containers[name]

        for name in tree.fixed:
            container(name)
        for item in planned:
            if item.action == "same":
                continue
            record = item.record
            parent, tags = container(record.container)
            where = record.path
            if item.action == "create":
                parent.create_container(
                    record.key,
                    metadata=dict(record.fields),
                    specs=[Spec(*record.spec)],
                    access_tags=tags or _given(parent, f"{record.container}/"),
                )
            else:
                there = parent[record.key]
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


def _says(node: Any, spec: tuple[str, str]) -> bool:
    """Whether *node*'s specs name *spec*."""
    return any(
        (getattr(own, "name", None), getattr(own, "version", None)) == spec
        for own in getattr(node, "specs", None) or ()
    )


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
