"""``nrw tiled``: log in, say what is on Tiled for this experiment, push to it.

Each command is a person's: it acts with their login. The guard refuses them
all to an agent, and each refuses itself under one too.
"""

from __future__ import annotations

import json
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


def run_login(*, root: str | None = None) -> None:
    """Log in to the person's server, through Globus, as the client does."""
    from nr_workbench.agent.guard import refuse_if_agent
    from nr_workbench.tiled.server import TiledSyncError, connect, server, whoami

    refuse_if_agent("tiled")
    start = _start(root)
    try:
        chosen = server(start)
        if chosen.key is not None:
            raise click.ClickException(
                f"TILED_API_KEY, set in {chosen.key_source}, is sent to "
                f"{chosen.url} instead of a login: remove it to log in."
            )
        client = connect(start, login=True)
        who = whoami(client)
    except TiledSyncError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"Logged in to {chosen.url} as {who}. The Tiled client keeps the login, "
        "and renews it; `nrw tiled logout` ends it."
    )


def run_logout(*, root: str | None = None) -> None:
    """End the login the client keeps for the person's server."""
    from nr_workbench.agent.guard import refuse_if_agent
    from nr_workbench.tiled.server import TiledSyncError, logout

    refuse_if_agent("tiled")
    try:
        click.echo(logout(_start(root)))
    except TiledSyncError as exc:
        raise click.ClickException(str(exc)) from exc


def status(project: Path) -> dict[str, Any]:
    """What the person's server holds for this project's experiment.

    Raises:
        TiledSyncError: The server could not be read.
    """
    from nr_workbench.tiled.context import plan_push, read_remote
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
    context = remote.context if remote is not None else None
    planned = plan_push(catalog, remote) if remote is not None else []
    ours = {(p.record.container, p.record.key) for p in planned}
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
        "context_found": context is not None,
        # Each of this project's records against Tiled's copy.
        "records": {
            action: sorted(p.record.path for p in planned if p.action == action)
            for action in ("same", "replace", "create")
        },
        "only_on_tiled": sorted(
            f"{container}/{key}"
            for container, key in (remote.records if remote is not None else {})
            if (container, key) not in ours
        ),
        "others": remote.others if remote is not None else [],
    }


def run_status(*, root: str | None = None, as_json: bool = False) -> None:
    """Print :func:`status`."""
    from nr_workbench.agent.guard import refuse_if_agent
    from nr_workbench.tiled.server import TiledSyncError

    refuse_if_agent("tiled")
    project = _project(root)
    try:
        found = status(project)
    except TiledSyncError as exc:
        raise click.ClickException(str(exc)) from exc
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
    if not found["context_found"]:
        click.echo("context/    not made yet: `nrw tiled push` makes it")
        return
    records = found["records"]
    click.echo(
        f"context/    {len(records['same'])} record(s) as this project has them, "
        f"{len(records['replace'])} different, {len(records['create'])} not "
        f"there yet, {len(found['only_on_tiled'])} on Tiled alone"
    )
    for path in records["replace"]:
        click.echo(f"  ~ {path}")
    for path in records["create"]:
        click.echo(f"  + {path}")
    for path in found["only_on_tiled"]:
        click.echo(f"  = {path}  (Tiled's alone: a push leaves it)")
    if found["others"]:
        click.echo(f"  others: {', '.join(found['others'])}  (not nrw's)")


def run_push(
    *, root: str | None = None, dry_run: bool = False, yes: bool = False
) -> None:
    """Write this project's catalog to its experiment's ``context/`` on Tiled.

    The last writer wins: each record that differs is written over Tiled's
    copy, keeping the fields other programs keep on it; one that matches is
    left alone. ``--dry-run`` says what would be written, and writes nothing.
    """
    from nr_workbench.agent.guard import refuse_if_agent
    from nr_workbench.tiled.context import CONTEXT, plan_push, push, read_remote
    from nr_workbench.tiled.server import (
        EXPERIMENT_PATH,
        TiledSyncError,
        connect,
        experiment_node,
        server,
    )

    refuse_if_agent("tiled")
    project = _project(root)
    ipts = _ipts(project)
    catalog = _catalog(project)
    if not catalog.runs and not catalog.samples:
        click.echo("The catalog holds no records yet: nothing to push.")
        return
    try:
        chosen = server(project)
        client = connect(project)
        experiment = experiment_node(client, ipts)
        if experiment is None:
            raise TiledSyncError(
                f"{EXPERIMENT_PATH.format(ipts=ipts)} is not on {chosen.url}, or "
                "not shared with this login: nothing was pushed."
            )
        remote = read_remote(experiment)
        planned = plan_push(catalog, remote)
        changes = [p for p in planned if p.action != "same"]
        place = f"{chosen.url}/{EXPERIMENT_PATH.format(ipts=ipts)}/{CONTEXT}"
        click.echo(
            f"{place}: {sum(p.action == 'create' for p in changes)} new, "
            f"{sum(p.action == 'replace' for p in changes)} changed, "
            f"{len(planned) - len(changes)} the same"
        )
        for item in changes:
            click.echo(
                f"  {'+' if item.action == 'create' else '~'} {item.record.path}"
            )
        if not changes:
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
        written = push(experiment, remote, planned, ipts=ipts)
    except TiledSyncError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Pushed {len(written)} record(s).")
