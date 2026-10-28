"""What a pull brings back from Tiled: the catalog, and the fits to run again.

``nrw tiled pull --context`` writes Tiled's runs, samples and experiment
context into this project's catalog. A record Tiled holds and the catalog
lacks is added; one that says something else in the catalog is replaced by
Tiled's; one only the catalog holds is left. The edits are held to the
catalog's own rules, and a record they refuse writes nothing at all.

``--fits`` rebuilds each fit here as Tiled records it: its directory, the
reduced files it read, where it read them, its index line and its curation.
It is on the Fits page as it was, with its curves and its parameters;
``nrw fit run --from`` runs it again from its own problem file, for a
posterior of this project's own. Its model's spec, script and ``model.md`` go
into its sample's ``models/``, to go on from. A fit discarded or deleted is
listed, and not pulled.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from nr_workbench.experiment.model import (
    EXPERIMENT_FIELDS,
    RUN_FIELDS,
    RUN_SNAPSHOT_FIELDS,
    SAMPLE_FIELDS,
    STEADY,
    Catalog,
    CatalogValidationError,
    ExperimentChange,
    RunChange,
    RunKey,
    SampleChange,
    apply_changes,
)
from nr_workbench.project.layout import ProjectLayout
from nr_workbench.tiled.context import EXPERIMENT, RUNS, SAMPLES, Remote, run_key

#: How the experiment's own context is named in a pull's list.
EXPERIMENT_LABEL = "the experiment's context"

#: A fit's model files, by their names in its record, and the suffix each is
#: written under in ``models/``.
MODEL_FILES = {"spec.yaml": ".yaml", "model.py": ".py", "model.md": ".md"}

#: How a fit set aside stands on Tiled: it is listed, and not pulled.
SET_ASIDE = ("discarded", "deleted")


@dataclass(frozen=True)
class Pulled:
    """One record as a context pull finds it.

    Attributes:
        action: ``create``, ``replace`` or ``same``.
        path: ``runs/234277-steady``, ``samples/S1``, or the experiment's.
        changed: For ``replace``, the fields Tiled's copy changes.
    """

    action: str
    path: str
    changed: tuple[str, ...] = ()


@dataclass
class ContextPull:
    """What a pull of ``context/`` writes into the catalog.

    Attributes:
        records: Each record Tiled holds, as the pull finds it, but for those
            the catalog's rules refuse.
        runs: The edits to the catalog's runs.
        samples: The edits to its samples.
        experiment: The edit to the experiment's context, if any.
        only_here: The records the catalog holds and Tiled does not: left.
        refused: What the catalog's rules refuse, by record, and why.
    """

    records: list[Pulled] = field(default_factory=list)
    runs: list[RunChange] = field(default_factory=list)
    samples: list[SampleChange] = field(default_factory=list)
    experiment: ExperimentChange | None = None
    only_here: list[str] = field(default_factory=list)
    refused: list[tuple[str, str]] = field(default_factory=list)

    @property
    def writes(self) -> bool:
        """Whether the pull changes the catalog."""
        return bool(self.runs or self.samples or self.experiment)


def plan_context_pull(catalog: Catalog, remote: Remote) -> ContextPull:
    """What a pull of *remote*'s records into *catalog* would write.

    Each edit is held to the catalog's rules on its own, so that a refusal
    names its record, and then all of them together, as the store applies
    them. Samples are listed first, then runs, as a push writes them.
    """
    plan = ContextPull()
    tiled_samples: set[str] = set()
    tiled_runs: set[RunKey] = set()
    for (container, key), there in sorted(remote.records.items(), key=_samples_first):
        path = f"{container}/{key}"
        try:
            if container == SAMPLES:
                sample_id = str(there.get("sample_id") or key)
                tiled_samples.add(sample_id)
                local: Any = catalog.samples.get(sample_id)
                theirs = _fields(there, SAMPLE_FIELDS)
                change: Any = SampleChange(sample_id, local.rev if local else 0, theirs)
            elif container == RUNS:
                run = RunKey(there.get("run"), there.get("kind") or STEADY)
                tiled_runs.add(run)
                local = catalog.runs.get(run)
                theirs = _fields(there, RUN_FIELDS + RUN_SNAPSHOT_FIELDS)
                change = RunChange(run, local.rev if local else 0, theirs)
            else:
                continue
            changed = tuple(
                name
                for name, value in theirs.items()
                if local is None or getattr(local, name) != value
            )
            if local is not None and not changed:
                plan.records.append(Pulled("same", path))
                continue
            if container == SAMPLES:
                apply_changes(catalog, samples=[change], now="")
            else:
                apply_changes(catalog, runs=[change], now="")
        except CatalogValidationError as exc:
            plan.refused.append((path, str(exc)))
            continue
        (plan.samples if container == SAMPLES else plan.runs).append(change)
        action = "create" if local is None else "replace"
        plan.records.append(Pulled(action, path, () if local is None else changed))

    there = (
        dict(remote.node.metadata).get(EXPERIMENT) if remote.node is not None else None
    )
    if isinstance(there, Mapping):
        theirs = _fields(there, EXPERIMENT_FIELDS)
        changed = tuple(
            name
            for name, value in theirs.items()
            if getattr(catalog.experiment, name) != value
        )
        if changed:
            edit = ExperimentChange(catalog.experiment.rev, theirs)
            try:
                apply_changes(catalog, experiment=edit, now="")
            except CatalogValidationError as exc:
                plan.refused.append((EXPERIMENT_LABEL, str(exc)))
            else:
                plan.experiment = edit
                action = "replace" if catalog.experiment.rev else "create"
                plan.records.append(Pulled(action, EXPERIMENT_LABEL, changed))
        else:
            plan.records.append(Pulled("same", EXPERIMENT_LABEL))

    if plan.writes and not plan.refused:
        try:
            apply_changes(
                catalog,
                runs=plan.runs,
                samples=plan.samples,
                experiment=plan.experiment,
                now="",
            )
        except CatalogValidationError as exc:
            plan.refused.append(("these records together", str(exc)))

    plan.only_here = sorted(
        [f"{SAMPLES}/{s}" for s in catalog.samples if s not in tiled_samples]
        + [f"{RUNS}/{run_key(k)}" for k in catalog.runs if k not in tiled_runs]
    )
    return plan


def _samples_first(item: tuple[tuple[str, str], Any]) -> tuple[int, str]:
    (container, key), _ = item
    return (0 if container == SAMPLES else 1, key)


def _fields(there: Mapping[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    """The fields of *names* Tiled's copy holds: one it lacks is left alone."""
    return {name: there[name] for name in names if name in there}


# --------------------------------------------------------------------------
# Fits
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PulledFit:
    """One fit on Tiled, as a fits pull leaves it here.

    Attributes:
        fit_id: The fit on Tiled.
        sample: Its sample.
        label: How it stands there: ``final``, ``starred`` …
        method: The fitter it ran with.
        chisq: Its chi-squared.
        here: Whether the index here holds it already, so it is left.
        written: What is written for it, relative to the project root: its
            directory, the reduced files it read, then its model's files in
            ``models/``.
        held: The reduced files it read whose paths here hold another file,
            which is left: the fit's own copy is in its directory.
        script: Its model's script in ``models/``; ``None`` when it is not
            pulled, or its record holds none.
        kept: Each label it holds on Tiled that another fit holds here, and
            keeps, with that fit's id.
        command: The command that runs it again.
        why_not: Why it is not pulled, when it is not.
    """

    fit_id: str
    sample: str
    label: str
    method: str
    chisq: Any
    here: bool = False
    written: tuple[str, ...] = ()
    held: tuple[str, ...] = ()
    script: str | None = None
    kept: tuple[tuple[str, str], ...] = ()
    command: str = ""
    why_not: str = ""


def fits_of(remote: Remote) -> list[tuple[str, str, Mapping[str, Any]]]:
    """Each fit on Tiled: its sample, its id and its record.

    By sample; in each, the final fit first, so that it takes its model's own
    name and its label, and then the others, oldest first.
    """
    from nr_workbench.tiled.analysis import label_of

    found = [
        (sample, fit_id, there) for (sample, fit_id), there in remote.records.items()
    ]
    return sorted(
        found,
        key=lambda f: (
            f[0],
            label_of(dict(f[2])) != "final",
            str(f[2].get("started_at") or ""),
            f[1],
        ),
    )


def pull_fits(
    root: Path,
    fits: list[tuple[str, str, Mapping[str, Any]]],
    *,
    write: bool = True,
) -> list[PulledFit]:
    """Rebuild each fit here as Tiled records it.

    A fit becomes ``samples/<sample>/results/<fit_id>/``, its files as they
    were recorded, with its own copy of each reduced file it read (see
    :func:`nr_workbench.tiled.analysis.data_copy`). Each of those files is
    also written where the fit read it, when nothing is there, so the fit's
    inputs are this project's. Then its index line is appended, then its star
    and its labels (:func:`nr_workbench.provenance.curation.adopt`). Nothing
    is overwritten: a file there already is left, and a fit the index holds
    already is left whole.

    Its model's spec, script and ``model.md`` go into the sample's
    ``models/``, to go on from. A model of the same name holding the same
    files is here already, and is left. One holding other files keeps them:
    the fit's are written as ``<model>-2``, ``<model>-3`` … instead, and so
    are a second fit's of the same model in this pull.

    Args:
        root: Project root.
        fits: From :func:`fits_of`.
        write: ``False`` says what would be written, and writes nothing.

    Returns:
        Each fit, as it is left here.
    """
    from nr_workbench.fsutil import atomic_write_bytes
    from nr_workbench.provenance.curation import adopt, label_holders
    from nr_workbench.provenance.index import FitIndex
    from nr_workbench.tiled.analysis import INDEX_FIELDS, label_of

    layout = ProjectLayout(root=Path(root))
    index = FitIndex(layout.index_file)
    known = {str(entry.get("fit_id")) for entry in index.fits()}
    holders = label_holders(index)
    given: dict[Path, dict[str, str]] = {}
    placed: dict[str, bytes] = {}
    pulled = []
    for sample, fit_id, there in fits:
        label = label_of(dict(there))
        common: dict[str, Any] = {
            "fit_id": fit_id,
            "sample": sample,
            "label": label,
            "method": str(there.get("method") or ""),
            "chisq": there.get("chisq"),
        }
        if label in SET_ASIDE:
            pulled.append(PulledFit(**common, why_not="not pulled"))
            continue
        stray = _stray(sample, fit_id, there)
        if stray:
            why = (
                f"its record names {stray!r}, which would be written outside its place"
            )
            pulled.append(PulledFit(**common, why_not=why))
            continue
        if fit_id in known:
            pulled.append(
                PulledFit(**common, here=True, command=_command(fit_id, there, None))
            )
            continue
        directory = layout.sample(sample) / "results" / fit_id
        if write:
            for relative, data in directory_files(there).items():
                if not (directory / relative).exists():
                    atomic_write_bytes(directory / relative, data)
        data, held = _place_data(layout, there, placed, write=write)
        script, models = _pull_model(layout, sample, fit_id, there, given, write=write)
        # A fit line, for the fit where the record sits on Tiled, whatever the
        # record's own line says.
        entry = {
            key: value
            for key, value in (
                there.get("index") or {k: there.get(k) for k in INDEX_FIELDS}
            ).items()
            if key != "event"
        }
        entry.update(fit_id=fit_id, sample=sample)
        if write:
            index.append(entry)
        curation = there.get("curation") or {}
        kept = adopt(
            index,
            entry,
            starred_by=(
                {"who": there.get("pushed_by"), "at": there.get("pushed_at")}
                if curation.get("starred")
                else None
            ),
            promotions=dict(curation.get("promotions") or {}),
            holders=holders,
            write=write,
        )
        known.add(fit_id)
        pulled.append(
            PulledFit(
                **common,
                written=(
                    f"{directory.relative_to(layout.root).as_posix()}/",
                    *data,
                    *models,
                ),
                held=tuple(held),
                script=script,
                kept=tuple(kept),
                command=_command(fit_id, there, script),
            )
        )
    return pulled


def _stray(sample: str, fit_id: str, there: Mapping[str, Any]) -> str:
    """The first name or path in a fit's record that would put a file outside
    its place; ``""`` when none would.

    Its sample, its id, its model and each of its files must each be one
    name; each reduced file it read, a path inside the project.
    """
    from nr_workbench.tiled.analysis import CURVES

    names = [sample, fit_id, _model_of(there, fit_id)]
    if there.get("problem"):
        names.append(there["problem"].get("name"))
    names.extend(there.get("fit") or {})
    for segment in there.get("curves") or ():
        names.extend(segment[part].get("name") for part in CURVES if part in segment)
    for name in names:
        if not _one_name(name):
            return str(name)
    for recorded in there.get("data") or {}:
        if not _in_project(recorded):
            return str(recorded)
    return ""


def _one_name(name: Any) -> bool:
    """Whether *name* names one file or folder, on any system nrw runs on."""
    return (
        isinstance(name, str)
        and name not in ("", ".", "..")
        and "/" not in name
        and "\\" not in name
        and not PureWindowsPath(name).drive
    )


def _in_project(recorded: Any) -> bool:
    """Whether *recorded*, a path as ``inputs.json`` records it, stays inside
    the project: relative, with no ``..``."""
    if not isinstance(recorded, str) or not recorded or "\\" in recorded:
        return False
    path = PurePosixPath(recorded)
    return (
        not path.is_absolute()
        and ".." not in path.parts
        and not PureWindowsPath(recorded).drive
    )


def _place_data(
    layout: ProjectLayout,
    there: Mapping[str, Any],
    placed: dict[str, bytes],
    *,
    write: bool,
) -> tuple[list[str], list[str]]:
    """Write each reduced file a fit read where it read it, when nothing is
    there: the paths written, and the paths holding another file, left.

    *placed* holds what this pull wrote, by path, so that a dry run says what
    a pull would.
    """
    from nr_workbench.fsutil import atomic_write_bytes

    written, held = [], []
    for recorded, entry in (there.get("data") or {}).items():
        if "text" not in entry:
            continue
        data = _bytes(entry)
        target = layout.root / recorded
        here = placed.get(recorded)
        if here is None and target.exists():
            here = target.read_bytes() if target.is_file() else b""
        if here is not None:
            if here != data:
                held.append(recorded)
            continue
        placed[recorded] = data
        written.append(recorded)
        if write:
            atomic_write_bytes(target, data)
    return written, held


def _command(fit_id: str, there: Mapping[str, Any], script: str | None) -> str:
    """The command that runs a pulled fit again: from its problem file, else
    its model's script in ``models/``; ``""`` when it has neither."""
    if there.get("problem"):
        return rerun_command(fit_id)
    return f"nrw fit run {shlex.quote(script)}" if script else ""


def directory_files(there: Mapping[str, Any]) -> dict[str, bytes]:
    """A fit's directory as its record holds it: each file's path in it, and
    its bytes."""
    from nr_workbench.tiled.analysis import CURVES, FILES, JSON_FILES, data_copy

    found: dict[str, bytes] = {}
    for part, name in JSON_FILES.items():
        if there.get(part) is not None:
            found[name] = (json.dumps(there[part], indent=2) + "\n").encode("utf-8")
    for name, entry in (there.get("files") or {}).items():
        if name in FILES:
            found[name] = _bytes(entry)
    if there.get("problem"):
        found[f"fit/{there['problem']['name']}"] = _bytes(there["problem"])
    for name, entry in (there.get("fit") or {}).items():
        found[f"fit/{name}"] = _bytes(entry)
    for segment in there.get("curves") or ():
        for part in CURVES:
            if part in segment:
                found[f"fit/{segment[part]['name']}"] = _bytes(segment[part])
    for recorded, entry in (there.get("data") or {}).items():
        if "text" in entry:
            found[data_copy(recorded)] = _bytes(entry)
    return found


def _bytes(entry: Mapping[str, Any]) -> bytes:
    """A file a record carries, as the bytes it was read from."""
    return str(entry.get("text", "")).encode("utf-8")


def _pull_model(
    layout: ProjectLayout,
    sample: str,
    fit_id: str,
    there: Mapping[str, Any],
    given: dict[Path, dict[str, str]],
    *,
    write: bool,
) -> tuple[str | None, list[str]]:
    """Write a fit's model files into its sample's ``models/``: its script
    there, and the files written, relative to the project root."""
    files = model_files(there)
    if "model.py" not in files:
        return None, []
    models = layout.sample(sample) / "models"
    name, here = _model_name(models, _model_of(there, fit_id), files, given)
    given[models / name] = files
    written: list[str] = []
    if not here:
        for source, suffix in MODEL_FILES.items():
            if source not in files:
                continue
            target = models / f"{name}{suffix}"
            written.append(target.relative_to(layout.root).as_posix())
            if write:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(files[source].encode("utf-8"))
    return (models / f"{name}.py").relative_to(layout.root).as_posix(), written


def model_files(there: Mapping[str, Any]) -> dict[str, str]:
    """A fit record's model files, by name, as text."""
    return {
        name: str(entry.get("text", ""))
        for name, entry in (there.get("files") or {}).items()
        if name in MODEL_FILES and isinstance(entry, Mapping)
    }


def _model_of(there: Mapping[str, Any], fit_id: str) -> str:
    """The name a fit's model is written under: its model's, else its id."""
    return str(there.get("model") or fit_id)


def _model_name(
    models: Path,
    model: str,
    files: dict[str, str],
    given: dict[Path, dict[str, str]],
) -> tuple[str, bool]:
    """The name the fit's model is written under, and whether it is here
    already: ``model``, else ``model-2``, ``model-3`` … A name *given* to
    another fit in this pull is that fit's."""
    n = 1
    while True:
        name = model if n == 1 else f"{model}-{n}"
        if models / name in given:
            if given[models / name] == files:
                return name, True
            n += 1
            continue
        held = {
            source: models / f"{name}{suffix}"
            for source, suffix in MODEL_FILES.items()
            if (models / f"{name}{suffix}").exists()
        }
        if not held:
            return name, False
        if all(
            source in held and held[source].read_bytes() == text.encode("utf-8")
            for source, text in files.items()
        ):
            return name, True
        n += 1


def rerun_command(fit_id: str) -> str:
    """``nrw fit run --from``, which runs the fit again from its own problem
    file, with its own fitter and settings."""
    return f"nrw fit run --from {shlex.quote(fit_id)}"


def data_applied(root: Path, sample: str) -> bool:
    """Whether the sample's data is in ``data/steady/``, for its script to read."""
    steady = ProjectLayout(root=Path(root)).sample(sample) / "data" / "steady"
    return steady.is_dir() and any(steady.iterdir())
