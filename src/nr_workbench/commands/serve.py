"""``nrw serve`` -- the web view of a project, and its Experiment page."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

import click

from nr_workbench.project.layout import ProjectLayout, ProjectNotFoundError

#: Carries the one-time secret to the reloader's child process under
#: ``--debug``: the reloader re-executes the program, and a secret generated
#: again in the child would not match the link the parent printed.
TOKEN_ENV = "NRW_SERVE_TOKEN"


def run_serve(
    *,
    root: str | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    debug: bool = False,
) -> None:
    """Serve the project UI.

    Args:
        root: Project root. Discovered from the working directory if omitted.
        host: Interface to bind to.
        port: Port to listen on.
        debug: Enable the Flask reloader and debugger.

    Raises:
        click.ClickException: If no project can be found, the port is taken,
            or the debugger would be exposed beyond this machine.
    """
    from nr_workbench.agent.guard import AGENT_ENV
    from nr_workbench.web.security import is_loopback

    try:
        layout = (
            ProjectLayout(root=Path(root).resolve())
            if root
            else ProjectLayout.discover()
        )
    except ProjectNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc

    loopback = is_loopback(host)
    if debug and not loopback:
        raise click.ClickException(
            f"--debug with --host {host} would let anyone who can reach this port "
            "run code on this machine: the debugger executes what it is sent. "
            "Use --debug only on loopback."
        )

    reason = ""
    if not loopback:
        reason = (
            f"Bound to {host}, not loopback, so the experiment can be viewed but "
            "not edited. Run `nrw serve` without --host to edit it."
        )
    elif os.environ.get(AGENT_ENV):
        reason = f"{AGENT_ENV} is set, so this server does not accept edits."

    token = os.environ.get(TOKEN_ENV) or secrets.token_urlsafe(24)
    os.environ[TOKEN_ENV] = token

    # Imported here rather than at module scope so that `nrw --help` does not
    # pay for Flask, matching how every other command treats its heavy deps.
    from nr_workbench.web.app import create_app

    try:
        app = create_app(
            layout.root,
            writable=not reason,
            read_only_reason=reason,
            bound_host=host,
            token=token,
        )
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc

    overview = app.config["NRW_DATA"].overview()
    shown = f"[{host}]" if ":" in host else host
    click.echo(f"  {overview['name']}  {layout.root}")
    click.echo(f"  {len(overview['samples'])} sample(s), {overview['n_fits']} fit(s)")
    click.echo("")
    click.echo(f"  http://{shown}:{port}/")
    click.echo(f"  http://{shown}:{port}/experiment      the experiment's runs")
    click.echo(
        f"  http://{shown}:{port}/settings        its IPTS, data folder, watcher "
        "and language model"
    )
    click.echo(f"  http://{shown}:{port}/api/overview    the same data as JSON")
    click.echo("")
    _echo_data_folder(layout.root)
    needs_setup = app.config["NRW_SETTINGS"].needs_setup()
    if needs_setup:
        click.echo(
            "  This experiment is not set up yet: nrw needs its IPTS, or the\n"
            "  folder its reduced data is in, before it can watch anything.\n"
        )
    if reason:
        click.echo(f"  {reason}")
    else:
        click.echo(
            "  To edit the experiment, curate fits or change the settings, open "
            "this link\n  in your browser:"
        )
        click.echo(f"    http://{shown}:{port}/auth/{token}")
        click.echo(
            "  It works once, for one browser, and is kept out of the request log.\n"
            "  Without it the pages are view-only."
            + ("\n  It opens Settings." if needs_setup else "")
        )
    ignored = app.config["NRW_PERSONAL_IGNORED"]
    if ignored:
        click.echo(
            f"\n  ! The project's .env sets {', '.join(ignored)}, which it may "
            "not:\n    anyone who can write the project writes that file. "
            "Set them in ~/.nrw or your shell.",
            err=True,
        )
    if not loopback:
        click.echo(
            f"\n  ! Bound to {host}. There is no authentication for reading, so "
            "anyone who\n    can reach this port can read the project.",
            err=True,
        )

    try:
        app.run(host=host, port=port, debug=debug)
    except OSError as exc:
        raise click.ClickException(
            f"Cannot bind {host}:{port} -- {exc}. "
            "Another server may already be running; try --port."
        ) from exc
    finally:
        # A fit started from the page runs in a process group of its own, so
        # Ctrl-C here does not reach it; stopping the server stops it, rather
        # than leaving it to hold the cores unseen.
        stopped = app.config["NRW_MODELS"].jobs.stop()
        if stopped is not None:
            click.echo(f"  Stopped the {stopped.label}.")


def _echo_data_folder(root: Path) -> None:
    """Which folder the Experiment page will watch, and anything wrong with that.

    From nrw.toml alone, never the data mount. A location typed into a comment
    once left the server on its default folder, and nothing it printed said so.
    """
    from nr_workbench.experiment.config import experiment_config_for
    from nr_workbench.project.config import DEFAULT_EXPERIMENT_LOCATION

    config = experiment_config_for(root)
    folder = config.source.path or config.source.location
    default = config.source.location == DEFAULT_EXPERIMENT_LOCATION
    click.echo(f"  Data folder  {folder}" + ("  (nrw's default)" if default else ""))
    for problem in config.problems:
        click.secho(f"  ! {problem.message}", fg="yellow")
    click.echo("")
