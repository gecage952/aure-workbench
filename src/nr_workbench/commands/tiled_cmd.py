"""``nrw tiled``: log in, say what is on Tiled for this experiment, push the
catalog and the starred and final fits to it, and pull them back with its
reduced files.

Each command is a person's: it acts with their login, and the guard refuses
them all to an agent. Every way the client fails is caught in one place,
:func:`nr_workbench.tiled.server.talking`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import click


def _start(root: str | None) -> Path | None:
    return Path(root).resolve() if root else None


def _project(root: str | None) -> Path:
    from nr_workbench.project.layout import ProjectLayout, ProjectNotFoundError

    try:
        layout = (
            ProjectLayout(root=Path(root).resolve())
            if root
            else ProjectLayout.discover()
        )
    except ProjectNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc
    return layout.root


def _ipts(project: Path) -> str:
    from nr_workbench.project.config import ProjectConfigError, load_config

    try:
        ipts = load_config(project).ipts
    except ProjectConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    if not ipts:
        raise click.ClickException(
            "nrw.toml names no IPTS: set it first, as `nrw experiment settings "
            "--ipts IPTS-34347 --write` does."
        )
    return ipts


def _catalog(project: Path) -> Any:
    from nr_workbench.experiment.store import CatalogError, ParquetCatalogStore

    try:
        return ParquetCatalogStore.for_project(project).load()
    except CatalogError as exc:
        raise click.ClickException(str(exc)) from exc


@contextmanager
def _asking(start: Path | None) -> Iterator[tuple[Any, Any]]:
    """The person's server and the client's parts, with every failure of the
    client said as a command's error."""
    from nr_workbench.tiled.server import TiledSyncError, library, server, talking

    try:
        chosen = server(start)
        tiled = library()
        with talking(chosen.url, tiled):
            yield chosen, tiled
    except TiledSyncError as exc:
        raise click.ClickException(str(exc)) from exc


def run_login(*, root: str | None = None) -> None:
    """Log in to the person's server, through Globus, as the client does."""
    from nr_workbench.tiled.server import connect, whoami

    start = _start(root)
    with _asking(start) as (chosen, _):
        if chosen.key is not None:
            raise click.ClickException(
                f"TILED_API_KEY, set in {chosen.key_source}, is sent to "
                f"{chosen.url} instead of a login: remove it to log in."
            )
        who = whoami(connect(start, login=True))
    click.echo(
        f"Logged in to {chosen.url} as {who}. The Tiled client keeps the login, "
        "and renews it; `nrw tiled logout` ends it."
    )


def run_logout(*, root: str | None = None) -> None:
    """End the login the client keeps for the person's server."""
    from nr_workbench.tiled.server import logout

    start = _start(root)
    with _asking(start):
        said = logout(start)
    click.echo(said)


def status(project: Path) -> dict[str, Any]:
    """What the person's server holds for this project's experiment.

    Raises:
        TiledSyncError: Not logged in, or the client failed (said as nrw's
            only inside :func:`_asking`).
    """
    from nr_workbench.tiled.context import (
        plan_experiment,
        plan_push,
        read_remote,
        records_of,
    )
    from nr_workbench.tiled.server import (
        EXPERIMENT_PATH,
        connect,
        experiment_node,
        server,
        tags_of,
        whoami,
    )

    ipts = _ipts(project)
    catalog = _catalog(project)
    chosen = server(project)
    client = connect(project)
    node = experiment_node(client, ipts)
    remote = read_remote(node) if node is not None else None
    planned = plan_push(records_of(catalog), remote) if remote is not None else []
    context_plan = plan_experiment(catalog, remote) if remote is not None else None
    records, only_on_tiled = _compared(planned, remote)
    return {
        "server": chosen.url,
        "server_set_in": chosen.source,
        "login": (
            f"TILED_API_KEY, set in {chosen.key_source}"
            if chosen.key is not None
            else "the login the Tiled client keeps"
        ),
        "who": whoami(client),
        "ipts": ipts,
        "experiment": EXPERIMENT_PATH.format(ipts=ipts),
        "experiment_found": node is not None,
        "tags": tags_of(node) if node is not None else [],
        "context_found": remote is not None and remote.node is not None,
        # Each of this project's records against Tiled's copy.
        "records": records,
        "only_on_tiled": only_on_tiled,
        "others": list(remote.others) if remote is not None else [],
        # The experiment's own context against Tiled's copy, in context/'s
        # own metadata.
        "experiment_context": (
            {
                "state": context_plan.action,
                "changed": list(context_plan.changed),
            }
            if context_plan is not None
            else None
        ),
        # The fits there against this project's: what a push writes, and
        # what a pull would bring.
        "analysis": _analysis_status(node, project) if node is not None else None,
        # The reduced files there against the data folder: what a pull copies.
        "data": _data_status(node, project) if node is not None else None,
    }


def _compared(
    planned: list[Any], remote: Any
) -> tuple[dict[str, list[str]], list[str]]:
    """This project's records against Tiled's copy, by what a push does with
    each, and the records Tiled alone holds."""
    ours = {(p.record.container, p.record.key) for p in planned}
    theirs = remote.records if remote is not None else {}
    by_action = {
        action: sorted(p.record.path for p in planned if p.action == action)
        for action in ("same", "replace", "create")
    }
    alone = sorted(f"{c}/{k}" for c, k in theirs if (c, k) not in ours)
    return by_action, alone


def _analysis_status(experiment: Any, project: Path) -> dict[str, Any]:
    """``analysis/`` on Tiled against this project's fits."""
    from nr_workbench.project.layout import ProjectLayout
    from nr_workbench.provenance.index import FitIndex
    from nr_workbench.tiled.analysis import ANALYSIS_TREE, fit_records, label_of
    from nr_workbench.tiled.context import plan_push, read_remote
    from nr_workbench.tiled.pull import fits_of

    remote = read_remote(experiment, ANALYSIS_TREE)
    fits, left = fit_records(project, remote)
    records, only_on_tiled = _compared(plan_push(fits, remote, ANALYSIS_TREE), remote)
    index = FitIndex(ProjectLayout(root=project).index_file)
    known = {str(entry.get("fit_id")) for entry in index.fits()}
    return {
        "found": remote.node is not None,
        "records": records,
        "only_on_tiled": only_on_tiled,
        "left": [{"fit_id": fit.fit_id, "why": fit.why} for fit in left],
        "fits": [
            {
                "fit_id": fit_id,
                "sample": sample,
                "model": there.get("model"),
                "method": there.get("method"),
                "chisq": there.get("chisq"),
                "curation": label_of(dict(there)),
                "here": fit_id in known,
            }
            for sample, fit_id, there in fits_of(remote)
        ],
    }


def _data_status(experiment: Any, project: Path) -> dict[str, Any]:
    """The reduced files in *experiment* on Tiled against the data folder
    nrw.toml names: what `nrw tiled pull --data` would copy into it."""
    from nr_workbench.tiled.data import autoreduction_of, plan_pull, run_of

    folder = _folder_of(project)
    autoreduction = autoreduction_of(experiment)
    missing, held = (
        plan_pull(autoreduction, folder) if autoreduction is not None else ([], 0)
    )
    return {
        "on_tiled": autoreduction is not None,
        "folder": str(folder) if folder is not None else None,
        "files": len(missing) + held,
        "held": held,
        "missing": [item.name for item in missing],
        "missing_runs": sorted({run_of(item.name) for item in missing}),
    }


def run_status(*, root: str | None = None, as_json: bool = False) -> None:
    """Print :func:`status`."""
    project = _project(root)
    with _asking(project):
        found = status(project)
    if as_json:
        click.echo(json.dumps(found, indent=2))
        return
    click.echo(f"Tiled       {found['server']}  ({found['server_set_in']})")
    click.echo(f"login       {found['login']}: {found['who']}")
    if not found["experiment_found"]:
        click.secho(
            f"{found['ipts']:<11} {found['experiment']} is not there, or not "
            "shared with this login: the experiment may not be on Tiled yet.",
            fg="yellow",
        )
        return
    tags = ", ".join(found["tags"]) or "none"
    click.echo(f"{found['ipts']:<11} {found['experiment']}, tagged {tags}")
    _echo_context(found)
    _echo_analysis(found["analysis"])
    _echo_data(found["data"])


def _echo_context(found: dict[str, Any]) -> None:
    """``context/`` on Tiled against this project's catalog, for a person."""
    if not found["context_found"]:
        click.echo("context/    not made yet: `nrw tiled push` makes it")
        return
    records = found["records"]
    click.echo(
        f"context/    {len(records['same'])} record(s) as this project has them, "
        f"{len(records['replace'])} different, {len(records['create'])} not "
        f"there yet, {len(found['only_on_tiled'])} on Tiled alone"
    )
    context = found["experiment_context"]
    shown = {
        "same": "as this project has it",
        "replace": f"different: {', '.join(context['changed'])}" if context else "",
        "create": "not there yet",
    }
    if context and context["state"] in shown:
        click.echo(f"            the experiment's context {shown[context['state']]}")
    for path in records["replace"]:
        click.echo(f"  ~ {path}")
    for path in records["create"]:
        click.echo(f"  + {path}")
    for path in found["only_on_tiled"]:
        click.echo(f"  = {path}  (Tiled's alone: a push leaves it)")
    if found["others"]:
        click.echo(f"  others: {', '.join(found['others'])}  (not nrw's)")


def _echo_analysis(analysis: dict[str, Any]) -> None:
    """``analysis/`` on Tiled against this project's fits, for a person."""
    records = analysis["records"]
    if not analysis["found"]:
        click.echo(
            "analysis/   not made yet: `nrw tiled push` makes it once a fit is "
            "starred or final"
        )
    else:
        click.echo(
            f"analysis/   {len(records['same'])} fit(s) as this project has them, "
            f"{len(records['replace'])} different, {len(records['create'])} not "
            f"there yet, {len(analysis['only_on_tiled'])} on Tiled alone"
        )
    for path in records["replace"]:
        click.echo(f"  ~ {path}")
    for path in records["create"]:
        click.echo(f"  + {path}")
    for fit in analysis["left"]:
        click.echo(f"  left: {fit['fit_id']}, {fit['why']}")
    alone = set(analysis["only_on_tiled"])
    for fit in analysis["fits"]:
        path = f"{fit['sample']}/{fit['fit_id']}"
        if path in alone:
            where = (
                "here" if fit["here"] else "not here; `nrw tiled pull --fits` brings it"
            )
            click.echo(
                f"  = {path}  {fit['curation']}, {fit['method']}, chisq "
                f"{fit['chisq']}: {where}"
            )


def _echo_data(data: dict[str, Any]) -> None:
    """The reduced files on Tiled against the data folder, for a person."""
    if not data["on_tiled"]:
        click.echo("data        no reduced files on Tiled for this experiment yet")
        return
    files, missing, folder = data["files"], data["missing"], data["folder"]
    if folder is None:
        click.echo(
            f"data        {files} reduced file(s) on Tiled, and the data folder "
            "cannot be worked out from nrw.toml: `nrw tiled pull --data --into "
            "DIR` copies them"
        )
        return
    if not missing:
        click.echo(f"data        all {files} reduced file(s) on Tiled are in {folder}")
        return
    click.echo(
        f"data        {len(missing)} of the {files} reduced file(s) on Tiled are "
        f"not in {folder}: `nrw tiled pull --data` copies them"
    )
    click.echo(f"            from {_runs(data['missing_runs'])}")


def _runs(runs: list[int]) -> str:
    """Runs as a person reads them: each of a few, or the first and the last."""
    if len(runs) == 1:
        return f"run {runs[0]}"
    if len(runs) <= 6:
        return "runs " + ", ".join(str(run) for run in runs)
    return f"{len(runs)} runs, {runs[0]} to {runs[-1]} (`--json` lists each file)"


def run_push(
    *,
    root: str | None = None,
    only: str | None = None,
    dry_run: bool = False,
    yes: bool = False,
) -> None:
    """Write this project's catalog to ``context/``, and its starred and final
    fits to ``analysis/``, in its experiment's node on Tiled.

    The last writer wins: each record that differs is written over Tiled's
    copy, keeping the fields other programs keep on it; one that matches is
    left alone. One confirmation covers both trees. ``--only`` narrows the
    push to one; ``--dry-run`` says what would be written, and writes nothing.
    """
    from nr_workbench.project.layout import ProjectLayout
    from nr_workbench.provenance.index import FitIndex
    from nr_workbench.tiled.analysis import ANALYSIS, ANALYSIS_TREE, fit_records
    from nr_workbench.tiled.context import (
        CONTEXT,
        plan_experiment,
        plan_push,
        push,
        read_remote,
        records_of,
    )
    from nr_workbench.tiled.server import (
        EXPERIMENT_PATH,
        TiledSyncError,
        connect,
        experiment_node,
    )

    project = _project(root)
    ipts = _ipts(project)
    catalog = _catalog(project) if only != "fits" else None
    with_catalog = catalog is not None and bool(
        catalog.runs or catalog.samples or catalog.experiment.rev
    )
    with_fits = only != "context" and bool(
        FitIndex(ProjectLayout(root=project).index_file).fits()
    )
    if not with_catalog and not with_fits:
        click.echo(
            "The catalog holds no records, and no fit is recorded: nothing to push."
        )
        return
    with _asking(project) as (chosen, _):
        experiment = experiment_node(connect(project), ipts)
        if experiment is None:
            raise TiledSyncError(
                f"{EXPERIMENT_PATH.format(ipts=ipts)} is not on {chosen.url}, or "
                "not shared with this login: nothing was pushed."
            )
        place = f"{chosen.url}/{EXPERIMENT_PATH.format(ipts=ipts)}"
        remote = remote_fits = context_plan = None
        planned: list[Any] = []
        planned_fits: list[Any] = []
        if with_catalog:
            remote = read_remote(experiment)
            planned = plan_push(records_of(catalog), remote)
            context_plan = plan_experiment(catalog, remote)
            _echo_context_push(f"{place}/{CONTEXT}", planned, context_plan)
        if with_fits:
            remote_fits = read_remote(experiment, ANALYSIS_TREE)
            fits, left = fit_records(project, remote_fits)
            planned_fits = plan_push(fits, remote_fits, ANALYSIS_TREE)
            if planned_fits or left:
                _echo_fits_push(f"{place}/{ANALYSIS}", planned_fits, left)
            else:
                click.echo(
                    f"{place}/{ANALYSIS}: no fit is starred or final, so none is pushed"
                )
        writes_context = context_plan is not None and bool(
            _changes(planned) or context_plan.writes
        )
        writes_fits = bool(_changes(planned_fits))
        if not writes_context and not writes_fits:
            held = ["this catalog"] if with_catalog else []
            if planned_fits:
                held.append("these fits")
            if held:
                click.echo(f"Tiled already holds {' and '.join(held)}.")
            return
        if dry_run:
            click.echo("Nothing written (--dry-run).")
            return
        if not yes:
            click.confirm(
                "Write these over Tiled's copy, for everyone on the experiment?",
                abort=True,
            )
        if writes_context and context_plan is not None:
            written = push(
                experiment, remote, planned, ipts=ipts, context_plan=context_plan
            )
            also = " and the experiment's context" if context_plan.writes else ""
            click.echo(f"Pushed {len(written)} record(s){also}.")
        if writes_fits:
            written = push(
                experiment, remote_fits, planned_fits, ipts=ipts, tree=ANALYSIS_TREE
            )
            click.echo(f"Pushed {len(written)} fit(s) to {ANALYSIS}/.")


def _changes(planned: list[Any]) -> list[Any]:
    """What a push writes of *planned*: each record that is not the same."""
    return [item for item in planned if item.action != "same"]


def _tally(actions: list[str]) -> str:
    """``2 new, 1 changed, 85 the same``: what a push or a pull does."""
    return (
        f"{actions.count('create')} new, {actions.count('replace')} changed, "
        f"{actions.count('same')} the same"
    )


def _echo_context_push(place: str, planned: list[Any], context_plan: Any) -> None:
    """What a push writes in ``context/``, for a person."""
    click.echo(f"{place}: {_tally([item.action for item in planned])}")
    if context_plan.writes:
        mark = "+" if context_plan.action == "create" else "~"
        click.echo(
            f"  {mark} the experiment's context: {', '.join(context_plan.changed)}"
        )
    for item in _changes(planned):
        click.echo(f"  {'+' if item.action == 'create' else '~'} {item.record.path}")


def _echo_fits_push(place: str, planned: list[Any], left: list[Any]) -> None:
    """What a push writes in ``analysis/``, for a person."""
    from nr_workbench.tiled.analysis import label_of

    click.echo(f"{place}: {_tally([item.action for item in planned])}")
    for item in _changes(planned):
        label = label_of(dict(item.record.fields))
        if item.action == "create":
            click.echo(f"  + {item.record.path}  ({label})")
        else:
            click.echo(f"  ~ {item.record.path}  ({label}; {', '.join(item.changed)})")
    for fit in left:
        click.echo(f"  left: {fit.fit_id}, {fit.why}")


def _folder_of(project: Path) -> Path | None:
    """The data folder nrw.toml names, ``None`` when it cannot be worked out."""
    from nr_workbench.experiment.config import experiment_config_for

    return experiment_config_for(project).source.path


def _data_folder(project: Path) -> Path:
    """The data folder nrw.toml names."""
    folder = _folder_of(project)
    if folder is None:
        raise click.ClickException(
            "The data folder cannot be worked out from nrw.toml: give --into DIR, "
            "or set [experiment.source] location."
        )
    return folder


def run_pull(
    *,
    root: str | None = None,
    context: bool = False,
    data: bool = False,
    fits: bool = False,
    runs: tuple[int, ...] = (),
    into: str | None = None,
    dry_run: bool = False,
    yes: bool = False,
) -> None:
    """Bring back from Tiled what a colleague needs: the catalog, the reduced
    files, and the fits, in that order.

    ``--context`` writes Tiled's records into the catalog, Tiled's copy
    winning, after one confirmation. ``--data`` copies each reduced file the
    data folder lacks. ``--fits`` rebuilds each fit as it was recorded
    (:func:`nr_workbench.tiled.pull.pull_fits`). Nothing is written on Tiled.
    """
    from nr_workbench.tiled.server import EXPERIMENT_PATH, connect, experiment_node

    if not (context or data or fits):
        raise click.ClickException(
            "Say what to pull: --context, --data or --fits, or any of them."
        )
    project = _project(root)
    ipts = _ipts(project)
    folder = (Path(into).resolve() if into else _data_folder(project)) if data else None
    with _asking(project) as (chosen, _):
        experiment = experiment_node(connect(project, data_sources=data), ipts)
        place = f"{chosen.url}/{EXPERIMENT_PATH.format(ipts=ipts)}"
        if (context or fits) and experiment is None:
            raise click.ClickException(
                f"{EXPERIMENT_PATH.format(ipts=ipts)} is not on {chosen.url}, or "
                "not shared with this login: nothing was pulled."
            )
        if context:
            _pull_context(project, experiment, place, dry_run=dry_run, yes=yes)
        if data:
            _pull_data(experiment, folder, ipts, runs=runs, dry_run=dry_run)
        if fits:
            _pull_fits(project, experiment, place, dry_run=dry_run)


def _pull_context(
    project: Path, experiment: Any, place: str, *, dry_run: bool, yes: bool
) -> None:
    """Tiled's runs, samples and experiment context, into the catalog."""
    from nr_workbench.experiment.model import CatalogValidationError, RecordConflict
    from nr_workbench.experiment.store import ParquetCatalogStore
    from nr_workbench.tiled.context import CONTEXT, read_remote
    from nr_workbench.tiled.pull import plan_context_pull

    remote = read_remote(experiment)
    if remote.node is None:
        click.echo(f"{place}/{CONTEXT} is not made yet: no catalog to pull.")
        return
    plan = plan_context_pull(_catalog(project), remote)
    changes = _changes(plan.records)
    click.echo(
        f"{place}/{CONTEXT}, into this project's catalog: "
        f"{_tally([record.action for record in plan.records])}, "
        f"{len(plan.only_here)} here alone (left as they are)"
    )
    for record in changes:
        if record.action == "create":
            click.echo(f"  + {record.path}")
        else:
            click.echo(f"  ~ {record.path}  ({', '.join(record.changed)})")
    if plan.refused:
        for path, why in plan.refused:
            click.echo(f"  ! {path}: {why}")
        raise click.ClickException(
            "Nothing was written: the catalog's rules refuse what Tiled holds "
            "for the records marked !."
        )
    if not plan.writes:
        click.echo("The catalog already holds what Tiled does.")
        return
    if dry_run:
        click.echo("Nothing written (--dry-run).")
        return
    if not yes:
        click.confirm("Write Tiled's copy over this project's catalog?", abort=True)
    try:
        ParquetCatalogStore.for_project(project).update(
            runs=plan.runs, samples=plan.samples, experiment=plan.experiment
        )
    except RecordConflict as exc:
        raise click.ClickException(
            f"{exc} Nothing was written: pull again to see the catalog as it is now."
        ) from exc
    except CatalogValidationError as exc:
        raise click.ClickException(
            f"Nothing was written: the catalog's rules refuse what Tiled holds: {exc}"
        ) from exc
    click.echo(
        f"Pulled {len(changes)} record(s) into the catalog. `nrw experiment apply "
        "--write` writes each sample's sample.md and copies its data."
    )


def _pull_data(
    experiment: Any,
    folder: Path | None,
    ipts: str,
    *,
    runs: tuple[int, ...],
    dry_run: bool,
) -> None:
    """Each reduced file on Tiled the data folder lacks, copied in."""
    from nr_workbench.tiled.data import AUTOREDUCTION, autoreduction_of, plan_pull, pull
    from nr_workbench.tiled.server import EXPERIMENT_PATH, TiledSyncError

    assert folder is not None
    where = f"{EXPERIMENT_PATH.format(ipts=ipts)}/{AUTOREDUCTION}"
    autoreduction = autoreduction_of(experiment) if experiment is not None else None
    if autoreduction is None:
        raise TiledSyncError(
            f"{where} is not on Tiled, or not shared with this login: "
            "nothing was copied."
        )
    missing, held = plan_pull(autoreduction, folder, runs=runs)
    click.echo(
        f"{where} -> {folder}: {len(missing)} file(s) to copy, {held} held "
        "already (left as they are: delete one to copy it again)"
    )
    for item in missing:
        click.echo(f"  + {item.name}")
    if not missing:
        return
    if dry_run:
        click.echo("Nothing copied (--dry-run).")
        return
    written = pull(missing, folder)
    click.echo(f"Copied {len(written)} file(s) into {folder}.")


def _pull_fits(project: Path, experiment: Any, place: str, *, dry_run: bool) -> None:
    """Each fit, rebuilt here as Tiled records it."""
    from nr_workbench.tiled.analysis import ANALYSIS, ANALYSIS_TREE
    from nr_workbench.tiled.context import read_remote
    from nr_workbench.tiled.pull import data_applied, fits_of, pull_fits

    found = fits_of(read_remote(experiment, ANALYSIS_TREE))
    click.echo(f"{place}/{ANALYSIS}: {len(found)} fit(s)")
    if not found:
        return
    pulled = pull_fits(project, found, write=not dry_run)
    for fit in pulled:
        said = f"{fit.label}, {fit.method or 'fitter unknown'}, chisq {fit.chisq}"
        path = f"{fit.sample}/{fit.fit_id}"
        if fit.why_not:
            click.echo(f"  - {path}  {said}: {fit.why_not}")
            continue
        if fit.here:
            click.echo(f"  = {path}  {said}: here already")
        else:
            click.echo(f"  + {path}  {said}: {', '.join(fit.written)}")
        for held in fit.held:
            click.echo(
                f"      {held} holds another file here, left: the fit's own copy "
                "is in its data/"
            )
        for label, holder in fit.kept:
            click.echo(f"      {label}: {holder} holds it here, and keeps it")
        if fit.command:
            click.echo(f"      {fit.command}")
    for sample in sorted({fit.sample for fit in pulled if fit.script and not fit.here}):
        if not data_applied(project, sample):
            click.echo(
                f"  {sample}: its data is not applied yet, for its models to read. "
                "`nrw experiment apply --write`, once the pulled files have "
                "settled, copies it in."
            )
    if dry_run:
        click.echo("Nothing written (--dry-run).")
        return
    click.echo(
        "Each fit is here as it was recorded, on the Fits page. Its command runs "
        "it again from its own problem file: the results are this project's "
        "own, to look at before going on, finalizing it or exporting it."
    )
