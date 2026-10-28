"""What a pull brings back from Tiled: the catalog, and the fits to run again.

``nrw tiled pull --context`` writes Tiled's runs, samples and experiment
context into this project's catalog. A record Tiled holds and the catalog
lacks is added; one that says something else in the catalog is replaced by
Tiled's; one only the catalog holds is left. The edits are held to the
catalog's own rules, and a record they refuse writes nothing at all.

``--fits`` writes each fit's model files -- its spec, its script and its
``model.md`` -- into its sample's ``models/``, and says how to run it again.
The run is this project's own fit, with results of its own, to look at
before going on, finalizing it or exporting it to ISAAC. A fit discarded or
deleted is listed, and not pulled.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
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
        script: Its script here, relative to the project root; ``None`` when
            it is not pulled.
        written: The files written for it; empty when they are here already.
        command: The command that runs it again.
        why_not: Why it is not pulled, when it is not.
    """

    fit_id: str
    sample: str
    label: str
    method: str
    chisq: Any
    script: str | None = None
    written: tuple[str, ...] = ()
    command: str = ""
    why_not: str = ""


def fits_of(remote: Remote) -> list[tuple[str, str, Mapping[str, Any]]]:
    """Each fit on Tiled: its sample, its id and its record.

    By sample; in each, the final fit first, so that it takes its model's own
    name, and then the others, oldest first.
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
    """Write each fit's model files into its sample's ``models/``.

    A model of the same name holding the same files is here already, and is
    left. One holding other files keeps them: the fit's are written as
    ``<model>-2``, ``<model>-3`` … instead, and so are a second fit's of the
    same model in this pull.

    Args:
        root: Project root.
        fits: From :func:`fits_of`.
        write: ``False`` says what would be written, and writes nothing.

    Returns:
        Each fit, as it is left here.
    """
    from nr_workbench.tiled.analysis import label_of

    layout = ProjectLayout(root=Path(root))
    given: dict[Path, dict[str, str]] = {}
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
        files = model_files(there)
        if label in SET_ASIDE:
            pulled.append(PulledFit(**common, why_not="not pulled"))
            continue
        if "model.py" not in files:
            pulled.append(PulledFit(**common, why_not="no script on Tiled"))
            continue
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
        script = (models / f"{name}.py").relative_to(layout.root).as_posix()
        pulled.append(
            PulledFit(
                **common,
                script=script,
                written=tuple(written),
                command=rerun_command(script, there, fit_id),
            )
        )
    return pulled


def model_files(there: Mapping[str, Any]) -> dict[str, str]:
    """A fit record's model files, by name, as text."""
    return {
        name: str(entry.get("text", ""))
        for name, entry in (there.get("files") or {}).items()
        if name in MODEL_FILES and isinstance(entry, Mapping)
    }


def model_state(root: Path, sample: str, fit_id: str, there: Mapping[str, Any]) -> str:
    """Whether a fit's model is here: ``pulled``; ``differs``, when another
    model of its name is, and a pull would write it beside that one; or
    ``absent``."""
    files = model_files(there)
    if "model.py" not in files:
        return "absent"
    model = _model_of(there, fit_id)
    models = ProjectLayout(root=Path(root)).sample(sample) / "models"
    name, here = _model_name(models, model, files, {})
    if here:
        return "pulled"
    return "absent" if name == model else "differs"


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


def rerun_command(script: str, there: Mapping[str, Any], fit_id: str) -> str:
    """``nrw fit run`` for *script*, with the settings the fit ran with.

    The fitter's own settings and the seed, as the fit's manifest records
    them; how many CPUs is this machine's to say.
    """
    from nr_workbench.fitting.settings import METHOD_SETTINGS

    params = (there.get("manifest") or {}).get("params") or {}
    method = str(params.get("method") or there.get("method") or "")
    words = ["nrw", "fit", "run", script]
    if method:
        words += ["--method", method]
    for name in (*METHOD_SETTINGS.get(method, ()), "seed"):
        if params.get(name) is not None:
            words += [f"--{name}", str(params[name])]
    words += ["--note", f"regenerated from {fit_id}, pulled from Tiled"]
    return " ".join(shlex.quote(word) for word in words)


def data_applied(root: Path, sample: str) -> bool:
    """Whether the sample's data is in ``data/steady/``, for its script to read."""
    steady = ProjectLayout(root=Path(root)).sample(sample) / "data" / "steady"
    return steady.is_dir() and any(steady.iterdir())
