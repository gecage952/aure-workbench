"""``nrw tiled``: log in, and say what is on Tiled for this experiment.

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
    from nr_workbench.tiled.server import (
        EXPERIMENT_PATH,
        connect,
        experiment_node,
        server,
        tags_of,
        whoami,
    )

    ipts = _ipts(project)
    chosen = server(project)
    client = connect(project)
    node = experiment_node(client, ipts)
    context = None
    if node is not None:
        try:
            context = node["context"]
        except KeyError:
            context = None
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
    click.echo(
        "context/    "
        + ("there" if found["context_found"] else "not made yet: a push makes it")
    )
