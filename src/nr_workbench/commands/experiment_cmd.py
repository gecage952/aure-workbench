"""``nrw experiment`` -- the experiment's runs, organized into samples.

The same catalog the Experiment page edits, from the command line: what has
arrived, which sample each run belongs to, and what applying would write.
``apply`` and ``adopt`` only show what they would do until given ``--write``,
the same convention as ``nrw import``: they write into directories people are
working in.

Assigning, applying, adopting and releasing are refused to an unattended
agent (``NRW_AGENT``); ``status`` and the previews are not.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

from nr_workbench.project.layout import ProjectLayout, ProjectNotFoundError


def _root(root: str | None) -> Path:
    try:
        layout = (
            ProjectLayout(root=Path(root).resolve())
            if root
            else ProjectLayout.discover()
        )
    except ProjectNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc
    return layout.root


def _workspace(root: str | None) -> Any:
    from nr_workbench.experiment.workspace import Workspace

    return Workspace(_root(root))


def _catalog(workspace: Any) -> Any:
    from nr_workbench.experiment.store import CatalogError

    try:
        return workspace.store.load()
    except CatalogError as exc:
        raise click.ClickException(str(exc)) from exc


def _observed(workspace: Any) -> tuple[Any, dict, dict]:
    snapshot = workspace.observe()
    runs = {k: v.source for k, v in snapshot.runs.items() if v.source is not None}
    statuses = {k: v.status for k, v in snapshot.runs.items()}
    return snapshot, runs, statuses


def _echo_problems(problems: Any) -> None:
    for problem in problems:
        click.secho(f"  ! {problem.message}", fg="yellow")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def run_status(*, root: str | None = None, as_json: bool = False) -> None:
    """Report what the source holds, what the catalog says, and what is pending."""
    from nr_workbench.experiment.model import EXPERIMENT_FIELDS
    from nr_workbench.experiment.store import CatalogError
    from nr_workbench.experiment.views import experiment_card, run_row, sample_card

    workspace = _workspace(root)
    snapshot, _, _ = _observed(workspace)
    problems = [
        *workspace.problems,
        *snapshot.inventory.problems,
        *snapshot.feed.problems,
    ]
    readable = True
    try:
        catalog = workspace.store.load()
    except CatalogError as exc:
        from nr_workbench.experiment.model import Catalog
        from nr_workbench.problems import Problem

        # Empty for the report, and said to be unreadable: an assistant
        # reading --json must not take an unreadable catalog for an unwritten one.
        catalog = Catalog()
        readable = False
        problems.append(Problem("catalog", str(exc)))

    runs = [
        run_row(key, snapshot.runs.get(key), catalog.runs.get(key))
        for key in sorted(set(snapshot.runs) | set(catalog.runs))
    ]

    payload = {
        "schema": "nrw-experiment-status/1",
        "ipts": workspace.config.ipts,
        "source": workspace.source.describe(),
        "reachable": snapshot.inventory.reachable,
        "feed": workspace.feed.describe(),
        "catalog": {**workspace.store.describe(), "readable": readable},
        "runs": runs,
        "samples": [
            sample_card(workspace.root, catalog, sample_id)
            for sample_id in catalog.sample_ids()
        ],
        "experiment": experiment_card(catalog.experiment),
        "problems": [p.as_dict() for p in problems],
    }
    if as_json:
        # ASCII only, the default: an assistant reads this, and every invisible
        # character in a committed record then arrives as a visible escape.
        click.echo(json.dumps(payload, indent=2))
        return

    source = payload["source"]
    click.echo(f"Experiment {workspace.config.ipts or '(no IPTS in nrw.toml)'}")
    reach = "reachable" if snapshot.inventory.reachable else "NOT reachable"
    click.echo(
        f"  source   {source.get('kind')}  {source.get('path') or source.get('location')}"
        f"  ({reach}, {len(snapshot.inventory.runs)} run(s))"
    )
    click.echo(f"  feed     {payload['feed'].get('kind')}")
    click.echo(
        f"  catalog  {workspace.store.directory.name}/  "
        + (
            f"{len(catalog.sample_ids())} sample(s), {len(catalog.runs)} run record(s)"
            if workspace.store.exists()
            else "none yet"
        )
    )
    written = [name for name in EXPERIMENT_FIELDS if payload["experiment"][name]]
    if not readable:
        context = "cannot be read; see below"
    elif written:
        context = f'{", ".join(written)}; --json shows them under "experiment"'
    else:
        context = "none yet; it is written on the Experiment page"
    click.echo(f"  context  {context}")

    counts: dict[str, int] = {}
    for row in runs:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    if counts:
        click.echo(
            "  runs     "
            + ", ".join(f"{n} {state}" for state, n in sorted(counts.items()))
        )

    unassigned = [r for r in runs if not r["sample"]]
    if unassigned:
        click.echo("\nUnassigned")
        for row in unassigned:
            click.echo(f"  {row['run']}  {row['state']:<12} {row['title']}")
    if payload["samples"]:
        click.echo("\nSamples")
        for sample in payload["samples"]:
            excluded = (
                f", {len(sample['excluded'])} excluded" if sample["excluded"] else ""
            )
            click.echo(
                f"  {sample['id']:<12} {len(sample['runs'])} run(s){excluded}  "
                f"{sample['title'] or sample['id']}"
            )
    if problems:
        click.echo()
        _echo_problems(problems)


# ---------------------------------------------------------------------------
# assign
# ---------------------------------------------------------------------------


def run_assign(
    *,
    runs: tuple[str, ...],
    sample: str | None = None,
    unassign: bool = False,
    measurement: str | None = None,
    condition: str | None = None,
    note: str | None = None,
    exclude: bool = False,
    include: bool = False,
    root: str | None = None,
) -> None:
    """Record which sample runs belong to, and how they were measured."""
    from nr_workbench.agent.guard import refuse_if_agent
    from nr_workbench.experiment.model import (
        CatalogValidationError,
        RecordConflict,
        RunChange,
        RunKey,
    )
    from nr_workbench.experiment.store import CatalogError

    refuse_if_agent("experiment")
    if sample and unassign:
        raise click.ClickException("--sample and --unassign contradict each other.")
    if exclude and include:
        raise click.ClickException("--exclude and --include contradict each other.")
    try:
        keys = [RunKey.parse(text) for text in runs]
    except CatalogValidationError as exc:
        raise click.ClickException(str(exc)) from exc

    fields: dict[str, Any] = {}
    if sample:
        fields["sample_id"] = sample
    if unassign:
        fields["sample_id"] = None
    for name, value in (
        ("measurement", measurement),
        ("condition", condition),
        ("note", note),
    ):
        if value is not None:
            fields[name] = value
    if exclude:
        fields["include"] = False
    if include:
        fields["include"] = True
    if not fields:
        raise click.ClickException(
            "Nothing to record: give --sample, --unassign, --type, --condition, "
            "--note, --exclude or --include."
        )

    workspace = _workspace(root)
    catalog = _catalog(workspace)
    snapshot, _, _ = _observed(workspace)
    changes = []
    for key in keys:
        current = catalog.runs.get(key)
        view = snapshot.runs.get(key)
        snapshot_fields: dict[str, Any] = {}
        if view is not None and view.source is not None:
            snapshot_fields = {
                "title": view.source.title,
                "start_time": view.source.start_time,
            }
        changes.append(
            RunChange(key, current.rev if current else 0, {**snapshot_fields, **fields})
        )
    try:
        workspace.store.update(runs=changes)
    except (CatalogValidationError, RecordConflict, CatalogError) as exc:
        raise click.ClickException(str(exc)) from exc

    described = ", ".join(f"{k}={v}" for k, v in fields.items())
    click.echo(f"  recorded {len(keys)} run(s): {described}")
    click.echo("  `nrw experiment apply` shows what that would write into samples/.")


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


def run_apply(
    *,
    samples: tuple[str, ...] = (),
    write: bool = False,
    confirm: tuple[str, ...] = (),
    root: str | None = None,
    as_json: bool = False,
) -> None:
    """Show, and with --write carry out, what applying the catalog would do."""
    from nr_workbench.agent.guard import refuse_if_agent
    from nr_workbench.experiment.apply import ApplyError, apply, plan_apply
    from nr_workbench.experiment.model import CatalogValidationError, RunKey
    from nr_workbench.experiment.render import SampleRenderError
    from nr_workbench.project.scaffold import LockProblemError

    if write:
        refuse_if_agent("experiment")
    try:
        confirmed = {RunKey.parse(text) for text in confirm}
    except CatalogValidationError as exc:
        raise click.ClickException(str(exc)) from exc

    workspace = _workspace(root)
    catalog = _catalog(workspace)
    _, runs, statuses = _observed(workspace)
    try:
        context = workspace.render_context()
    except SampleRenderError as exc:
        raise click.ClickException(str(exc)) from exc

    plan = plan_apply(
        workspace.root,
        catalog,
        runs,
        statuses,
        workspace.source,
        context,
        samples=list(samples) or None,
        confirmed=confirmed,
    )
    # Anything a person must look at makes the exit code non-zero: the command
    # succeeding would otherwise read as "in step".
    needs_attention = plan.needs_attention

    if not write:
        if as_json:
            click.echo(json.dumps(plan.as_dict(), indent=2))
        else:
            _echo_plan(plan)
            if plan.writes:
                click.echo("\n  Nothing was written. Run again with --write to apply.")
        if needs_attention:
            raise SystemExit(1)
        return

    try:
        report = apply(
            workspace.root,
            catalog,
            runs,
            statuses,
            workspace.source,
            context,
            expected_plan_id=plan.plan_id,
            samples=list(samples) or None,
            confirmed=confirmed,
        )
    except (ApplyError, LockProblemError) as exc:
        raise click.ClickException(str(exc)) from exc

    if as_json:
        click.echo(
            json.dumps({"plan": plan.as_dict(), "report": report.as_dict()}, indent=2)
        )
    else:
        _echo_plan(plan)
        click.echo()
        click.echo(f"  done: {len(report.done)} file action(s)")
        for failed in report.failed:
            click.secho(f"  ! {failed.name}: {failed.detail}", fg="red")
    if report.failed or needs_attention:
        raise SystemExit(1)


_SYMBOLS = {
    "copy": "+",
    "restore": "+",
    "record": "=",
    "unchanged": " ",
    "move-out": "-",
    "deferred": "…",
    "not-managed": "?",
}


def _echo_plan(plan: Any) -> None:
    _echo_problems(plan.problems)
    if not plan.samples:
        click.echo("  The catalog assigns no runs to any sample yet.")
    for sample in plan.samples:
        creates = "  (new sample)" if sample.creates else ""
        click.echo(f"\n{sample.sample_id}{creates}")
        _echo_problems(sample.problems)
        for action in sample.files:
            if action.action.value == "unchanged":
                continue
            symbol = _SYMBOLS.get(action.action.value, "!")
            detail = f"  {action.detail}" if action.detail else ""
            line = f"  {symbol} {action.action.value:<15}{action.name}{detail}"
            if symbol == "!":
                click.secho(line, fg="yellow")
            else:
                click.echo(line)
        unchanged = sum(1 for a in sample.files if a.action.value == "unchanged")
        if unchanged:
            click.echo(f"    {unchanged} file(s) already copied and unchanged")
        if sample.sample_md is not None:
            label = f"  sample.md {sample.sample_md_detail}"
            if sample.sample_md.value in ("drifted", "untracked"):
                click.secho(label, fg="yellow")
            else:
                click.echo(label)


# ---------------------------------------------------------------------------
# adopt
# ---------------------------------------------------------------------------


def run_adopt(
    *,
    samples: tuple[str, ...] = (),
    write: bool = False,
    rewrite: bool = False,
    root: str | None = None,
    as_json: bool = False,
) -> None:
    """Bring hand-written samples into the catalog, or pull hand edits back."""
    from nr_workbench.agent.guard import refuse_if_agent
    from nr_workbench.experiment.adopt import AdoptRefused, adopt, plan_adopt
    from nr_workbench.experiment.model import RecordConflict
    from nr_workbench.experiment.render import SampleRenderError
    from nr_workbench.experiment.store import CatalogError
    from nr_workbench.project.scaffold import LockProblemError

    if write:
        refuse_if_agent("experiment")
    if rewrite and not write:
        raise click.ClickException("--rewrite changes sample.md, so it needs --write.")

    workspace = _workspace(root)
    layout = ProjectLayout(root=workspace.root)
    catalog = _catalog(workspace)
    try:
        context = workspace.render_context()
    except SampleRenderError as exc:
        raise click.ClickException(str(exc)) from exc

    targets = list(samples) or [
        s for s in layout.list_samples() if not catalog.manages(s)
    ]
    if not targets:
        click.echo(
            "  Every sample is already in the catalog; name one to pull its hand edits."
        )
        return

    plans = []
    failed = False
    for sample_id in targets:
        plan = plan_adopt(workspace.root, catalog, sample_id, context)
        plans.append(plan)
        if not as_json:
            _echo_adopt(plan, show_diff=rewrite)
        if plan.problems:
            failed = True
            continue
        if write:
            try:
                report = adopt(
                    workspace.root, workspace.store, plan, context, rewrite=rewrite
                )
            except (
                AdoptRefused,
                RecordConflict,
                CatalogError,
                LockProblemError,
            ) as exc:
                click.secho(f"  ! {sample_id}: {exc}", fg="red")
                failed = True
                continue
            catalog = workspace.store.load()
            if not as_json:
                done = f"  adopted: {report.catalog_changes} catalog record(s)"
                if report.rewritten:
                    done += f"; sample.md rewritten, previous kept at {report.backup}"
                click.echo(done)

    if as_json:
        click.echo(json.dumps([p.as_dict() for p in plans], indent=2))
    elif not write:
        click.echo(
            "\n  Nothing was written. Run again with --write (and --rewrite to let"
        )
        click.echo("  the catalog rewrite sample.md) to adopt.")
    if failed:
        raise SystemExit(1)


def _echo_adopt(plan: Any, *, show_diff: bool) -> None:
    click.echo(f"\n{plan.sample_id}  sample.md is {plan.sample_md or 'missing'}")
    _echo_problems(plan.problems)
    for change in plan.run_changes:
        described = ", ".join(f"{k}={v!r}" for k, v in change.changes.items())
        click.echo(f"  run {change.key.run}: {described}")
    if plan.sample_change is not None:
        click.echo(f"  context: {', '.join(sorted(plan.sample_change.changes))}")
    for leftover in plan.parsed.leftovers:
        click.secho(f"  leftover  {leftover}", fg="yellow")
    if plan.undocumented:
        click.echo(
            f"  on disk but not in the table (not assigned): "
            f"{', '.join(map(str, plan.undocumented))}"
        )
    if plan.kept:
        click.echo(
            f"  in the catalog but not in the table (kept): "
            f"{', '.join(map(str, plan.kept))}"
        )
    if plan.in_step:
        click.echo("  the catalog would render this file exactly; no rewrite needed")
    if show_diff and plan.diff:
        click.echo(plan.diff)


# ---------------------------------------------------------------------------
# release
# ---------------------------------------------------------------------------


def run_release(*, sample: str, root: str | None = None) -> None:
    """Hand a catalog-rendered sample.md back to its people."""
    from nr_workbench.agent.guard import refuse_if_agent
    from nr_workbench.experiment.render import sample_md_relpath
    from nr_workbench.project.scaffold import LockProblemError, forget

    refuse_if_agent("experiment")
    project = _root(root)
    try:
        dropped = forget(project, sample_md_relpath(sample))
    except LockProblemError as exc:
        raise click.ClickException(str(exc)) from exc
    if not dropped:
        click.echo(
            f"  nrw has no record of writing samples/{sample}/sample.md; nothing to release."
        )
        return
    click.echo(
        f"  samples/{sample}/sample.md is yours now: no nrw command will write it.\n"
        "  If the catalog still assigns runs to this sample, apply keeps copying\n"
        "  their data, and the catalog's context no longer reaches the file."
    )


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------


def run_settings(
    *,
    ipts: str | None = None,
    label: str | None = None,
    location: str | None = None,
    default_location: bool = False,
    settle: float | None = None,
    poll: float | None = None,
    check: bool = False,
    confirm_ipts_change: bool = False,
    write: bool = False,
    root: str | None = None,
    as_json: bool = False,
) -> None:
    """Show the experiment's settings; with options, preview or make a change.

    The same settings, through the same save, as the Settings page of ``nrw
    serve``: only nrw's own lines of nrw.toml change, and a file nrw cannot
    edit safely is refused with the lines to add by hand.
    """
    from nr_workbench.agent.guard import refuse_if_agent
    from nr_workbench.experiment.views import settings_view
    from nr_workbench.project import settings as project_settings
    from nr_workbench.project.settings import CONFIRM_IPTS_CHANGE
    from nr_workbench.project.tomlfile import TomlEditError

    if write:
        refuse_if_agent("experiment")
    if location is not None and default_location:
        raise click.ClickException(
            "--location and --default-location contradict each other; give one."
        )
    project = _root(root)
    changes: dict[str, Any] = {}
    for name, value in (
        ("ipts", ipts),
        ("label", label),
        ("source.location", location),
        ("source.settle_seconds", settle),
        ("feed.poll_seconds", poll),
    ):
        if value is not None:
            changes[name] = value
    if default_location:
        changes["source.location"] = None
    if write and not changes:
        raise click.ClickException(
            "Nothing to write: name a setting to change, e.g. --ipts IPTS-34347."
        )

    catalogued = _catalogued_runs(project)
    checked = _check(project, changes) if check else None

    result = None
    if changes:
        try:
            result = project_settings.save(
                project,
                changes,
                confirmed={CONFIRM_IPTS_CHANGE} if confirm_ipts_change else set(),
                catalogued_runs=catalogued,
                write=write,
            )
        except project_settings.SettingsError as exc:
            raise click.ClickException(str(exc)) from exc
        except project_settings.NeedsConfirmation as exc:
            raise click.ClickException(
                f"{exc} Run again with --confirm-ipts-change to go ahead."
            ) from exc
        except TomlEditError as exc:
            raise click.ClickException(exc.explained()) from exc
        except OSError as exc:
            raise click.ClickException(f"Could not write nrw.toml: {exc}") from exc

    if as_json:
        # One shape, whichever options were given: the settings as they now
        # are, the change if one was asked for, and the check if one was run.
        click.echo(
            json.dumps(
                {
                    "settings": settings_view(project, catalogued_runs=catalogued),
                    "change": result.as_dict() if result is not None else None,
                    "check": checked,
                },
                indent=2,
            )
        )
        return
    if result is None:
        _echo_settings(settings_view(project, catalogued_runs=catalogued))
        if checked is not None:
            _echo_check(checked)
        return
    if not result.changed:
        click.echo("  nrw.toml already says that; nothing to change.")
    else:
        click.echo(result.diff, nl=False)
        if write:
            click.echo(f"\n  Saved. The previous nrw.toml is kept at {result.backup}.")
        else:
            click.echo("\n  Nothing was written. Run again with --write to save it.")
    for line in (*result.notes, *result.warnings):
        click.secho(f"  ! {line}", fg="yellow")
    if checked is not None:
        _echo_check(checked)


def _catalogued_runs(project: Path) -> int | None:
    """How many runs the catalog holds; ``None`` when it cannot be read."""
    from nr_workbench.experiment.store import CatalogError
    from nr_workbench.experiment.workspace import Workspace

    try:
        return len(Workspace(project).store.load().runs)
    except CatalogError:
        return None


def _check(project: Path, changes: dict[str, Any]) -> dict[str, Any]:
    """What the data folder the change names -- or the current one -- holds.

    Built as the watcher builds it, and given the same deadline as the
    Settings page's check: a dead mount must not hang a terminal either.
    """
    from nr_workbench.bounded import Bounded, TimedOut
    from nr_workbench.experiment.sources import SOURCE_TIMEOUT
    from nr_workbench.experiment.workspace import check_source
    from nr_workbench.project.settings import SettingsError

    once = Bounded(
        slots=1, timeout=SOURCE_TIMEOUT, name="nrw-folder-check", what="The folder"
    )
    try:
        return check_source(project, changes, run=once.run)
    except (SettingsError, TimedOut) as exc:
        raise click.ClickException(str(exc)) from exc


def _echo_settings(view: dict[str, Any]) -> None:
    values, defaults = view["values"], view["defaults"]

    def shown(name: str, unit: str = "") -> str:
        value = values[name]
        if value is None:
            return f"{defaults[name]}{unit}  (nrw's default)"
        return f"{value}{unit}"

    click.echo("Settings, from nrw.toml")
    click.echo(f"  IPTS       {values['ipts'] or '(none)'}")
    click.echo(f"  label      {values['label'] or '(none)'}")
    click.echo(f"  source     {shown('source.kind')}")
    location = shown("source.location")
    if values["source.location"] is None:
        location = location.replace("(nrw's default)", "(nrw's default; provisional)")
    click.echo(f"  location   {location}")
    effective = view["effective"]
    click.echo(f"             -> {effective['path'] or 'cannot be worked out yet'}")
    click.echo(f"  settle     {shown('source.settle_seconds', ' s')}")
    click.echo(f"  watcher    {shown('feed.kind')}")
    click.echo(f"  poll       {shown('feed.poll_seconds', ' s')}")
    click.echo(f"  catalog    {effective['catalog_kind']}")
    coming = [
        f"{option['label']} ({side})"
        for side, options in (
            ("source", view["options"]["source"]),
            ("watcher", view["options"]["feed"]),
            ("catalog", view["options"]["catalog"]),
        )
        for option in options
        if not option["available"]
    ]
    if coming:
        click.echo(f"  coming     {', '.join(coming)}")
    if effective["needs_setup"]:
        click.secho(
            "\n  Not set up yet: give the IPTS (--ipts) or a full data folder (--location).",
            fg="yellow",
        )
    if view["problems"]:
        click.echo()
    for problem in view["problems"]:
        click.secho(f"  ! {problem['message']}", fg="yellow")


def _echo_check(found: dict[str, Any]) -> None:
    click.echo(f"\nCheck of {found['path']}")
    if not found["reachable"]:
        click.secho(f"  {found['problems'][0]['message']}", fg="red")
        return
    if not found["runs"]:
        click.echo("  reachable, and no reduced runs in it yet")
    else:
        click.echo(f"  {found['runs']} run(s), {found['first']} to {found['last']}")
        for run in reversed(found["newest"]):
            segments = ",".join(map(str, run["segments"]))
            planned = f" of {run['n_segments']}" if run["n_segments"] else ""
            click.echo(
                f"    {run['run']}  {run['title']}  segments {segments}{planned}"
            )
            for problem in run["problems"]:
                click.secho(f"      ! {problem}", fg="yellow")
        if found["experiments"]:
            click.echo(f"  headers name {', '.join(found['experiments'])}")
    if found["unrecognized"]:
        click.echo(
            f"  {found['unrecognized']} data file(s) with names nrw does not recognise"
        )
    for problem in found["problems"]:
        click.secho(f"  ! {problem['message']}", fg="yellow")
