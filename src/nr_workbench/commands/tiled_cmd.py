"""``nrw tiled``: log in, say what is on Tiled for this experiment, push the
catalog to it, and copy its reduced files in.

Each command is a person's: it acts with their login. The guard refuses them
all to an agent, and each refuses itself under one too. Every way the client
fails is caught in one place, :func:`nr_workbench.tiled.server.talking`.
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
    from nr_workbench.tiled.context import plan_experiment, plan_push, read_remote
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
    planned = plan_push(catalog, remote) if remote is not None else []
    context_plan = plan_experiment(catalog, remote) if remote is not None else None
    ours = {(p.record.container, p.record.key) for p in planned}
    theirs = remote.records if remote is not None else {}
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
        "context_found": remote is not None and remote.context is not None,
        # Each of this project's records against Tiled's copy.
        "records": {
            action: sorted(p.record.path for p in planned if p.action == action)
            for action in ("same", "replace", "create")
        },
        "only_on_tiled": sorted(
            f"{container}/{key}"
            for container, key in theirs
            if (container, key) not in ours
        ),
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
        # The reduced files there against the data folder: what a pull copies.
        "data": _data_status(node, project) if node is not None else None,
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
    *, root: str | None = None, dry_run: bool = False, yes: bool = False
) -> None:
    """Write this project's catalog to its experiment's ``context/`` on Tiled.

    The last writer wins: each record that differs is written over Tiled's
    copy, keeping the fields other programs keep on it; one that matches is
    left alone. ``--dry-run`` says what would be written, and writes nothing.
    """
    from nr_workbench.tiled.context import (
        CONTEXT,
        plan_experiment,
        plan_push,
        push,
        read_remote,
    )
    from nr_workbench.tiled.server import (
        EXPERIMENT_PATH,
        TiledSyncError,
        connect,
        experiment_node,
    )

    project = _project(root)
    ipts = _ipts(project)
    catalog = _catalog(project)
    if not catalog.runs and not catalog.samples and not catalog.experiment.rev:
        click.echo("The catalog holds no records yet: nothing to push.")
        return
    with _asking(project) as (chosen, _):
        experiment = experiment_node(connect(project), ipts)
        if experiment is None:
            raise TiledSyncError(
                f"{EXPERIMENT_PATH.format(ipts=ipts)} is not on {chosen.url}, or "
                "not shared with this login: nothing was pushed."
            )
        remote = read_remote(experiment)
        planned = plan_push(catalog, remote)
        context_plan = plan_experiment(catalog, remote)
        changes = [p for p in planned if p.action != "same"]
        place = f"{chosen.url}/{EXPERIMENT_PATH.format(ipts=ipts)}/{CONTEXT}"
        click.echo(
            f"{place}: {sum(p.action == 'create' for p in changes)} new, "
            f"{sum(p.action == 'replace' for p in changes)} changed, "
            f"{len(planned) - len(changes)} the same"
        )
        if context_plan.writes:
            mark = "+" if context_plan.action == "create" else "~"
            click.echo(
                f"  {mark} the experiment's context: {', '.join(context_plan.changed)}"
            )
        for item in changes:
            click.echo(
                f"  {'+' if item.action == 'create' else '~'} {item.record.path}"
            )
        if not changes and not context_plan.writes:
            click.echo("Tiled already holds this catalog.")
            return
        if dry_run:
            click.echo("Nothing written (--dry-run).")
            return
        if not yes:
            click.confirm(
                "Write these over Tiled's copy, for everyone on the experiment?",
                abort=True,
            )
        written = push(
            experiment, remote, planned, ipts=ipts, context_plan=context_plan
        )
    also = " and the experiment's context" if context_plan.writes else ""
    click.echo(f"Pushed {len(written)} record(s){also}.")


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
    data: bool = False,
    runs: tuple[int, ...] = (),
    into: str | None = None,
    dry_run: bool = False,
) -> None:
    """Copy the experiment's reduced files from Tiled into the data folder.

    Each one the folder lacks is copied whole; one it holds already is left
    as it is. The folder watcher does the rest. Nothing is written on Tiled.
    """
    from nr_workbench.tiled.data import AUTOREDUCTION, autoreduction_of, plan_pull, pull
    from nr_workbench.tiled.server import (
        EXPERIMENT_PATH,
        TiledSyncError,
        connect,
        experiment_node,
    )

    if not data:
        raise click.ClickException(
            "Say what to pull: --data copies the experiment's reduced files."
        )
    project = _project(root)
    ipts = _ipts(project)
    folder = Path(into).resolve() if into else _data_folder(project)
    with _asking(project):
        experiment = experiment_node(connect(project, data_sources=True), ipts)
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
