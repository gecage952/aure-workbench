"""``nrw fit run`` -- run a fit and record everything about it.

M1 deliberately supports **hand-written scripts**. That is the whole adoption
story: an existing `experiments-2025` model can be run through this today, with
no migration, no schema to learn, and it comes out the other side with a
complete provenance record. The generated-spec path arrives later and produces
the same record shape.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import click

from nr_workbench.project.config import ProjectConfigError, load_config
from nr_workbench.project.layout import ProjectLayout, ProjectNotFoundError
from nr_workbench.provenance import stack
from nr_workbench.provenance.env import capture as capture_env
from nr_workbench.provenance.hashing import (
    MODEL_ROLES,
    FileDigest,
    HashCache,
    digest_files,
    inputs_digest,
)
from nr_workbench.provenance.index import FitIndex
from nr_workbench.provenance.record import (
    FitIdentity,
    FitRecord,
    create_unique,
    env_digest,
    format_timestamp,
    make_fit_id,
    settings_digest,
    utc_now,
)
from nr_workbench.provenance.stamp import stamp_directory

#: Data suffixes worth hashing when we have to infer a script's inputs.
_DATA_SUFFIXES = (".txt", ".dat", ".ort", ".csv", ".json")


@dataclass
class ResolvedTarget:
    """Where a fit's output belongs.

    Attributes:
        sample: Sample the fit belongs to, or None for a project-level run.
        results_dir: Directory that will hold the new fit directory.
    """

    sample: str | None
    results_dir: Path


@dataclass(frozen=True)
class FitSource:
    """What a fit runs: a script, or a recorded fit's problem file.

    Attributes:
        path: The script, or the problem file.
        role: Its role among the fit's inputs: ``script`` or ``problem``,
            which is also the fit's ``script_origin``.
        model: The model's name, unless one is given.
        sample: The sample, unless one is given; ``None`` infers it from
            *path*.
        settings: The settings the recorded fit ran with; empty for a script.
        regenerated_from: The recorded fit; ``None`` for a script.
        measurements: The measurements the recorded fit read, as its
            ``inputs.json`` records them. Its problem file holds them, so
            they are the new fit's too.
    """

    path: Path
    role: str
    model: str
    sample: str | None = None
    settings: dict[str, Any] = field(default_factory=dict)
    regenerated_from: str | None = None
    measurements: tuple[FileDigest, ...] = ()


#: The line a fit prints as it starts, naming its result directory -- whose name
#: is the fit id. The Experiment page reads it back from a job's output to link
#: the fit that job recorded, rather than guess which of the model's fits it was.
RUNNING_LINE = "Running {method} fit -> {directory}"
RUNNING_RE = re.compile(r"^Running \S+ fit -> (?P<directory>\S.*?)\s*$", re.MULTILINE)


def resolve_target(
    layout: ProjectLayout, script: Path, sample: str | None
) -> ResolvedTarget:
    """Decide which sample a fit belongs to.

    Args:
        layout: The project layout.
        script: The script being run.
        sample: Explicit sample, or None to infer from the script's location.

    Returns:
        The resolved target.

    Raises:
        click.ClickException: If an explicit sample does not exist.
    """
    if sample is not None:
        sample_dir = layout.sample(sample)
        if not sample_dir.is_dir():
            raise click.ClickException(layout.missing_sample_message(sample))
        return ResolvedTarget(sample=sample, results_dir=sample_dir / "results")

    # Infer from the script's path: samples/<id>/... anywhere above it.
    try:
        relative = script.resolve().relative_to(layout.samples_dir)
        inferred = relative.parts[0]
        if (layout.samples_dir / inferred).is_dir():
            return ResolvedTarget(
                sample=inferred, results_dir=layout.sample(inferred) / "results"
            )
    except ValueError:
        pass

    return ResolvedTarget(sample=None, results_dir=layout.root / "results")


def role_for(path: Path, root: Path) -> str:
    """Name an input file's role in a fit.

    The role vocabulary is what makes a reverse lookup readable: seeing
    ``data:tnr:r223921_t000240`` beats seeing a bare path.

    Args:
        path: The input file.
        root: Project root.

    Returns:
        A role string such as ``data:steady:REFL_226642_combined_data_auto``.
    """
    stem = path.stem
    try:
        parts = path.resolve().relative_to(Path(root).resolve()).parts
    except ValueError:
        return f"data:{stem}"

    if "tnr" in parts:
        return f"data:tnr:{stem}"
    if "steady" in parts:
        return f"data:steady:{stem}"
    if path.suffix == ".json":
        return f"metadata:{stem}"
    return f"data:{stem}"


def observed_inputs(
    script: Path, opened: list[Path], root: Path
) -> list[tuple[str, Path]]:
    """Build the role-tagged input list from files the script actually opened.

    Args:
        script: The script that ran.
        opened: Paths observed during execution.
        root: Project root.

    Returns:
        Role-tagged paths, the script itself first.
    """
    entries: list[tuple[str, Path]] = [("script", script.resolve())]
    for path in opened:
        entries.append((role_for(path, root), path))
    return entries


def guess_inputs(script: Path, root: Path) -> list[tuple[str, Path]]:
    """Guess a script's inputs without executing it, for ``--dry-run``.

    Reads string literals from the source and, for any that look like a data
    filename, searches the project for a unique match. Weaker than observing a
    real run -- a filename built at runtime from a run number is invisible here
    -- which is exactly why the real path executes the script instead.

    Args:
        script: The script to scan.
        root: Project root.

    Returns:
        Role-tagged paths, the script itself first.
    """
    import ast

    script = script.resolve()
    entries: list[tuple[str, Path]] = [("script", script)]
    seen: set[Path] = {script}

    try:
        tree = ast.parse(script.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return entries

    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        literal = node.value
        if not literal or not literal.endswith(_DATA_SUFFIXES):
            continue
        if literal.startswith(("http://", "https://")):
            continue

        found = _locate(literal, script, Path(root))
        if found is not None and found not in seen:
            seen.add(found)
            entries.append((role_for(found, root), found))

    return entries


def _locate(literal: str, script: Path, root: Path) -> Path | None:
    """Resolve a filename literal to a real file, searching the project."""
    for candidate in (Path(literal), script.parent / literal, root / literal):
        resolved = candidate.expanduser()
        if resolved.is_file():
            return resolved.resolve()

    # Scripts commonly build paths as dirname(__file__)/../data/..., so the
    # literal is only a basename. A unique match in the project is safe to use;
    # an ambiguous one is not worth guessing at.
    name = Path(literal).name
    matches = [p for p in root.rglob(name) if p.is_file()]
    return matches[0].resolve() if len(matches) == 1 else None


def run_fit_command(
    *,
    script: str | None = None,
    from_fit: str | None = None,
    sample: str | None = None,
    method: str | None = None,
    steps: int | None = None,
    samples: int | None = None,
    burn: int | None = None,
    pop: int | None = None,
    seed: int | None = None,
    parallel: int | None = None,
    plots: bool = False,
    note: str | None = None,
    model_name: str | None = None,
    dry_run: bool = False,
    as_json: bool = False,
    verbose: bool = False,
) -> FitRecord | None:
    """Run a fit script, or a recorded fit again, and write an immutable
    record of the run.

    Args:
        script: Path to the refl1d script; or ``None``, with *from_fit*.
        from_fit: A recorded fit, or a unique prefix of its id, to run again
            instead of a script, from its problem file (``fit/<model>.json``):
            its model, its parameters at the fit's values, and its data. Its
            fitter, its fitter's settings and its seed are the fit's own but
            for those given, and so are its sample and its model's name. The
            new fit's inputs are that file, and the measurements the fit
            recorded, which the file holds; its settings name the fit as
            ``regenerated_from``.
        sample: Sample to record the fit under. Inferred from the path if
            omitted; with *from_fit*, the fit's.
        method: Bumps fitter name; unset, the project's ``nrw.toml`` [fit]
            names it, or it is DREAM. See :mod:`nr_workbench.fitting.settings`.
        steps: Maximum optimizer steps.
        samples: DREAM sample count.
        burn: DREAM burn-in.
        pop: Population size.
        seed: Random seed.
        parallel: CPUs to use; 0 means all of them. Each setting left unset is
            taken from ``nrw.toml`` [fit], else bumps' default.
        note: Free-text note stored in the record.
        model_name: Model name. Defaults to the script stem; with *from_fit*,
            the fit's.
        dry_run: Report what would run, write nothing.
        as_json: Emit the record as JSON instead of prose.

    Returns:
        The completed record, so a Python caller gets the fit id and chi-squared
        without re-reading the index. ``None`` for ``--dry-run``, which writes
        nothing and so has nothing to return.

    Raises:
        click.ClickException: If there is no project, the script is missing,
            the fit to run again has no problem file, or the fit fails.
    """
    layout, config, source = _source(script, from_fit)
    target = resolve_target(layout, source.path, sample or source.sample)
    index = FitIndex(layout.index_file)

    # The command line, then nrw.toml [fit], then bumps: see fitting/settings.
    from nr_workbench.fitting.settings import (
        FitSettingsError,
        read_fit_defaults,
        resolve,
    )

    try:
        defaults = read_fit_defaults(config.raw)
    except FitSettingsError as exc:
        raise click.ClickException(f"{layout.root / 'nrw.toml'}: {exc}") from exc
    given: dict[str, Any] = {
        "steps": steps,
        "samples": samples,
        "burn": burn,
        "pop": pop,
        "seed": seed,
        "parallel": parallel,
    }
    method, given, inherited = _inherit(source.settings, method, given)
    try:
        fit = resolve(defaults, method=method, given=given)
    except FitSettingsError as exc:
        raise click.ClickException(str(exc)) from exc
    for key in inherited:
        # Said as the fit's own, not as typed on this command line.
        fit.origins[key] = f"fit {source.regenerated_from}"
    method = fit.method
    _refuse_over_agent_limits(layout.root, config.raw, fit)
    settings: dict[str, Any] = {"method": method, **fit.settings}
    if not as_json:
        _report_origins(fit)

    # Load the script once, under observation, so the recorded inputs are the
    # files it genuinely read rather than a guess from its source. --dry-run
    # does not execute anything, so it falls back to the weaker scan.
    loaded = None
    if source.role == "problem":
        candidates = [("problem", source.path)]
        if not dry_run:
            from nr_workbench.fitting.runner import FitError, load_problem_file

            try:
                loaded = load_problem_file(source.path)
            except FitError as exc:
                raise click.ClickException(str(exc)) from exc
    elif dry_run:
        candidates = guess_inputs(source.path, layout.root)
    else:
        from nr_workbench.fitting.runner import FitError, load_problem

        try:
            loaded = load_problem(source.path, root=layout.root)
        except FitError as exc:
            raise click.ClickException(str(exc)) from exc
        candidates = observed_inputs(source.path, loaded.opened, layout.root)

    cache = HashCache(layout.cache_dir / "hashes.json")
    try:
        inputs = digest_files(candidates, root=layout.root, cache=cache)
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc
    cache.save()
    inputs = [*inputs, *source.measurements]

    environment = capture_env(layout.root, include_requirements=not dry_run)
    identity = FitIdentity(
        script_sha256=next(d.sha256 for d in inputs if d.role == source.role),
        inputs_digest=inputs_digest(inputs),
        settings_digest=settings_digest(settings),
        env_digest=env_digest(environment),
    )

    duplicates = index.find_by_run_key(identity.run_key)
    if duplicates and not dry_run:
        previous = duplicates[0]
        click.echo(
            f"  ! An identical run already exists: {previous['fit_id']} (status "
            f"{previous.get('status')}, chisq {previous.get('chisq')}). Script, "
            "inputs, settings and environment all match; this one is recorded "
            "as a replicate of it.",
            err=True,
        )

    started = utc_now()
    fit_id = make_fit_id(started, identity.run_key)

    data_inputs = [d for d in inputs if d.role not in MODEL_ROLES]
    if dry_run:
        _report_dry_run(
            fit_id, target.results_dir / fit_id, settings, inputs, environment
        )
        return None

    if not data_inputs:
        click.echo(
            "  ! no data files could be inferred from the script; the record "
            "will list only the script itself",
            err=True,
        )

    record = FitRecord(
        fit_id=fit_id,
        sample=target.sample,
        model=model_name or source.model,
        script_origin=source.role,
        identity=identity,
        inputs=inputs,
        settings=settings,
        environment=environment,
        started_at=format_timestamp(started),
        command=_command_line(),
        note=note,
        # From the assembled problem, not from the script's text: this is the
        # stack the optimizer is about to see.
        stack=stack.describe(loaded.problem) if loaded is not None else "",
    )
    if duplicates:
        record.settings["replicate_of"] = duplicates[0]["fit_id"]
    if source.regenerated_from:
        record.settings["regenerated_from"] = source.regenerated_from

    try:
        directory, fit_id = create_unique(target.results_dir, fit_id)
    except FileExistsError as exc:
        raise click.ClickException(str(exc)) from exc
    fit_dir = directory.path
    record.fit_id = fit_id

    if source.role == "script":
        directory.freeze_script(source.path)
    directory.write_inputs(inputs)
    directory.write_environment(environment)
    # Two different things, so they go to two different places: the blockquote
    # says what was launched, and --note says why it was launched. Folding the
    # second into the first put a sentence a person typed into scaffolding
    # `is_blank` skips, so a run whose reason *was* stated still read as a fit
    # nobody had thought about.
    directory.write_notes_stub(
        fit_id=fit_id,
        description=(
            f"{method} fit of {record.model}, run again from "
            f"{source.regenerated_from}'s problem file."
            if source.regenerated_from
            else f"{method} fit of {record.model}."
        ),
        why=note or "",
    )

    # A provisional manifest, before the fit rather than after. Only FitError
    # is catchable; a kill, an OOM or a full disk during an hour-long DREAM run
    # leaves the directory with a script, inputs and an environment but no
    # manifest and no index line -- invisible to `whence`, `ls` and `check`.
    # That is how 5 of 25 result directories in the first real beamtime became
    # orphans, one of them holding a 298 MB posterior nothing could find.
    record.status = "running"
    directory.write_manifest(record)

    if not as_json:
        click.echo(
            RUNNING_LINE.format(
                method=method, directory=fit_dir.relative_to(layout.root)
            )
        )

    from nr_workbench.fitting.runner import FitError, run_fit

    assert loaded is not None  # dry_run returned above
    try:
        outcome = run_fit(
            loaded.problem,
            fit_dir / "fit",
            method=method,
            steps=settings["steps"],
            samples=settings["samples"],
            burn=settings["burn"],
            pop=settings["pop"],
            seed=settings["seed"],
            parallel=settings["parallel"],
            plots=plots,
            # Quiet by default, for two reasons. It would sit in front of the
            # JSON and make it unparseable for a driver that asked for JSON. And
            # an unattended session pays for it by the turn: bumps' progress on a
            # DREAM run overflows the harness's output buffer, so the agent then
            # pages the overflow file back in 60-200 lines at a time. Measured on
            # one real session: 19 of 95 tool calls -- a fifth of a 60-turn
            # budget -- were re-reading two fit logs it had already caused to be
            # written. The same text is in `fit/<model>.out` either way, and
            # `nrw assess` reads the per-model chi-squared straight out of it.
            quiet=not verbose or as_json,
        )
    except FitError as exc:
        # A failed fit is still recorded. Knowing that a model was tried and
        # did not converge is worth as much as knowing one did.
        record.status = "failed"
        record.error = str(exc)
        record.finished_at = format_timestamp(utc_now())
        directory.write_manifest(record)
        index.append(record.index_entry())
        raise click.ClickException(f"{exc}\nRecorded the failure as {fit_id}.") from exc

    record.status = "ok"
    record.finished_at = format_timestamp(utc_now())
    record.chisq = outcome.chisq
    record.converged = outcome.converged
    record.n_free = outcome.n_free
    record.n_points = outcome.n_points
    record.artifacts = outcome.artifacts
    record.models = outcome.models

    stamped = stamp_directory(fit_dir / "figures", fit_id)
    if stamped:
        record.artifacts["figures"] = "figures"

    directory.write_manifest(record)
    index.append(record.index_entry())

    # After the index entry: the summary resolves the fit by id, and the id is
    # only findable once it is recorded.
    _write_trajectory(layout, fit_dir, record)

    if as_json:
        click.echo(json.dumps(record.index_entry(), indent=2, default=str))
    else:
        _report_success(record, fit_dir, layout.root, outcome)
        _report_regression(record, index)

    return record


def _source(
    script: str | None, from_fit: str | None
) -> tuple[ProjectLayout, Any, FitSource]:
    """The project, its configuration, and what the fit runs: *script*, or
    the problem file of the fit *from_fit* names, in the project here.

    Raises:
        click.ClickException: Both or neither given; no project; the script
            is missing; no such fit, or it left no problem file.
    """
    from nr_workbench.fitting.runner import problem_path
    from nr_workbench.provenance.lookup import FitNotFoundError, resolve_fit
    from nr_workbench.provenance.record import FitDirectory

    if (script is None) == (from_fit is None):
        raise click.ClickException(
            "Give a script to run, or a fit to run again with --from; not both."
        )
    path = Path(script).resolve() if script is not None else None
    if path is not None and not path.is_file():
        raise click.ClickException(f"Script not found: {script}")
    try:
        layout = ProjectLayout.discover(path.parent if path else Path.cwd())
        config = load_config(layout.root)
    except (ProjectNotFoundError, ProjectConfigError) as exc:
        raise click.ClickException(str(exc)) from exc
    if path is not None:
        return layout, config, FitSource(path=path, role="script", model=path.stem)

    try:
        entry, directory = resolve_fit(
            layout, FitIndex(layout.index_file), str(from_fit)
        )
    except FitNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc
    problem = problem_path(directory)
    if problem is None:
        raise click.ClickException(
            f"{entry['fit_id']} left no problem file in {directory / 'fit'}, so "
            "it cannot be run again from one."
        )
    measurements = tuple(
        FileDigest(
            role=str(item["role"]),
            path=str(item["path"]),
            sha256=str(item["sha256"]),
            bytes=int(item.get("bytes") or 0),
        )
        for item in FitDirectory(directory).read_inputs()
        if item.get("role") not in MODEL_ROLES
    )
    return (
        layout,
        config,
        FitSource(
            path=problem,
            role="problem",
            model=str(entry.get("model") or problem.stem),
            sample=entry.get("sample"),
            settings=dict(entry.get("settings") or {}),
            regenerated_from=str(entry["fit_id"]),
            measurements=measurements,
        ),
    )


def _inherit(
    theirs: dict[str, Any], method: str | None, given: dict[str, Any]
) -> tuple[str | None, dict[str, Any], list[str]]:
    """The fitter and settings a fit runs with again: those *given*, then
    those it ran with, *theirs*.

    Its fitter's own settings and its seed are taken when it runs with its
    own fitter; another fitter takes none of them, and how many CPUs is this
    machine's to say.

    Returns:
        The fitter, the settings, and the names of those taken from it.
    """
    from nr_workbench.fitting.settings import METHOD_SETTINGS

    own = str(theirs.get("method") or "") or None
    if own is None or method not in (None, own):
        return method, given, []
    taken = [] if method else ["method"]
    settings = dict(given)
    for key in (*METHOD_SETTINGS.get(own, ()), "seed"):
        if settings.get(key) is None and theirs.get(key) is not None:
            settings[key] = theirs[key]
            taken.append(key)
    return own, settings, taken


def _command_line() -> str:
    """Return the command line that produced this record."""
    return " ".join(shlex.quote(part) for part in sys.argv)


def _report_origins(fit: Any) -> None:
    """Say which settings did not come from the command line, and whence.

    A fit that takes its fitter from nrw.toml, or from nrw's own default, looks
    exactly like one told it on the command line; this line is the difference.
    """
    said = [
        f"{key} {fit.method if key == 'method' else fit.settings[key]} ({origin})"
        for key, origin in fit.origins.items()
        if origin != "command line"
    ]
    if said:
        click.echo("  settings  " + ", ".join(said))


def _report_dry_run(
    fit_id: str,
    fit_dir: Path,
    settings: dict[str, Any],
    inputs: list[Any],
    environment: Any,
) -> None:
    """Print what a fit would do without running it."""
    click.echo(f"Would create {fit_dir}")
    click.echo(f"  fit_id    {fit_id}")
    click.echo(f"  method    {settings['method']}")
    given = [
        f"{key} {value}"
        for key, value in settings.items()
        if key != "method" and value is not None
    ]
    click.echo(f"  with      {', '.join(given)}; the rest bumps' defaults")
    click.echo(f"  inputs    {len(inputs)} file(s)")
    for digest in inputs:
        click.echo(f"    {digest.role:<24} {digest.path}  {digest.sha256[:12]}")
    if environment.git.available:
        state = "dirty" if environment.git.dirty else "clean"
        click.echo(f"  git       {(environment.git.commit or '?')[:12]} ({state})")


def _report_success(record: FitRecord, fit_dir: Path, root: Path, outcome: Any) -> None:
    """Print a summary of a completed fit."""
    click.echo()
    click.echo(f"  fit_id   {record.fit_id}")
    if record.chisq is not None:
        click.echo(f"  chisq    {record.chisq:.4g}")
    if record.n_free is not None:
        click.echo(f"  free     {record.n_free} parameter(s)")
    click.echo(f"  output   {fit_dir.relative_to(root)}")

    # The fitter's own log is suppressed by default, so say where it went rather
    # than leaving someone to wonder why a DREAM run printed four lines.
    log = next((fit_dir / "fit").glob("*.out"), None)
    if log is not None:
        click.echo(f"  log      {log.relative_to(root)}  (--verbose to see it live)")

    if not outcome.export_ok:
        click.echo(f"  ! bumps export incomplete: {outcome.export_error}", err=True)
        click.echo(
            "    Parameters were fitted; uncertainty output may be missing.", err=True
        )

    _next_steps(record)


def _report_regression(record: FitRecord, index: Any) -> None:
    """Say so, loudly, when this fit is much worse than the last of its model.

    The information already existed --- `nrw ls` has always shown
    `chisq 1.306 -> 16.58` --- but only afterwards, in a list, phrased as a
    fact. An unattended session read exactly that line, kept the one-line spec
    edit that caused it, and spent eleven further fits changing the optimiser,
    the population and the step count. None of it could work: the edit had made
    the model physically impossible, and a better search of an impossible model
    is still impossible.

    So it is said here, at the only moment it can change what happens next, and
    it names the move rather than describing it: the edit is the suspect, not
    the fitter. Never fatal --- a regression is a legitimate thing to record,
    and this must not stop a fit that ran.
    """
    from nr_workbench.provenance.summary import REGRESSION_FACTOR

    try:
        previous = _previous_fit_of_model(record, index)
        if previous is None or record.chisq is None:
            return
        was = previous.get("chisq")
        if not isinstance(was, int | float) or was <= 0:
            return
        if record.chisq <= was * REGRESSION_FACTOR:
            return

        click.echo()
        click.secho(
            f"  ! chi-squared got {record.chisq / was:.3g}x worse than "
            f"{str(previous.get('fit_id') or '')[-8:]} ({was:.4g} -> "
            f"{record.chisq:.4g}).",
            fg="yellow",
            err=True,
        )
        click.secho(
            "    Suspect the last change to the model, not the fitter. Read the "
            "spec diff\n    first; a search that cannot find a good minimum is "
            "usually one that has none\n    to find. Changing method, steps or "
            "population will not recover this.",
            err=True,
        )
    except Exception:  # noqa: BLE001 - a warning must not fail a fit that ran
        return


def _previous_fit_of_model(record: FitRecord, index: Any) -> dict[str, Any] | None:
    """The fit of this same model that ran before this one, if there is one."""
    for entry in index.fits(sample=record.sample):
        if entry.get("fit_id") == record.fit_id:
            continue
        if entry.get("model") == record.model:
            return entry
    return None


def _next_steps(record: FitRecord) -> None:
    """Name what to do with the fit that just finished.

    ``nrw note`` leads because it is the only one of these a machine cannot do
    later. The numbers stay in the record forever; the reason lasts about as
    long as it takes to start the next fit, which is why an unattended session
    reached morning with 25 result directories and no reason recorded in any of
    them. Naming the exact command --- with the sections it still needs, not the
    one ``--note`` already answered --- is the difference between a reminder and
    an instruction.

    ``nrw promote`` is dropped when an agent is driving: a hook refuses it
    (:mod:`nr_workbench.agent.guard`), so offering it as the next step spends a
    turn on a command that cannot run, at the moment the session should be
    writing down what it just learned.
    """
    from nr_workbench.agent.guard import agent_is_driving

    short = record.fit_id[-8:]
    # --note filled "Why this run" at launch, so asking for it again invites a
    # second answer to a question already answered.
    wanted = '--showed "..." --caveat "..."'
    if not record.note:
        wanted = f'--why "..." {wanted}'

    click.echo()
    click.echo(f"  nrw assess {short}      the checks that can be automated")
    click.echo(f"  nrw note {short} {wanted}")
    click.secho("      why you ran it, what it means, what not to conclude", dim=True)
    click.echo(f"  nrw whence {record.fit_id}      show the full provenance")
    if not agent_is_driving():
        click.echo(f"  nrw promote {record.fit_id} --as final --reason '...'")


def _refuse_over_agent_limits(root: Path, document: dict[str, Any], fit: Any) -> None:
    """Refuse an unattended fit that asks for more than ``[agent.limits]`` allows.

    Checked here, on what the fit would actually run with, rather than in a
    harness hook: a hook sees only the command line, and a fit that names no
    setting asks for whatever ``nrw.toml`` [fit] and bumps' defaults say. Here
    it is also the same check whichever harness, or none, is driving.

    Args:
        root: The project root, for messages.
        document: The parsed ``nrw.toml``.
        fit: The settled fit (:class:`~nr_workbench.fitting.settings.ResolvedFit`).

    Raises:
        click.ClickException: When ``NRW_AGENT`` is set and a setting is over
            its limit, or ``[agent.limits]`` cannot be read.
    """
    from nr_workbench.agent.guard import AGENT_ENV, agent_is_driving
    from nr_workbench.fitting.settings import FitSettingsError, read_agent_limits

    if not agent_is_driving():
        return
    try:
        limits = read_agent_limits(document)
    except FitSettingsError as exc:
        raise click.ClickException(f"{root / 'nrw.toml'}: {exc}") from exc
    over = limits.exceeded(fit)
    if not over:
        return
    caps = ", ".join(
        f"{key} {limit}" for key, limit in limits.per_method[fit.method].items()
    )
    asked = ", ".join(
        f"{key} {value} ({origin})" for key, (value, _limit, origin) in over.items()
    )
    raise click.ClickException(
        f"{AGENT_ENV} is set, so this is running unattended, and nrw.toml "
        f"[agent.limits.{fit.method}] caps such a fit at {caps}; this one asks "
        f"for {asked}. Run it again within those limits. If the answer needs "
        "more, say so in ESCALATIONS.md: the limits are the scientist's to change."
    )


def _write_trajectory(layout: ProjectLayout, fit_dir: Path, record: Any) -> None:
    """Precompute the time evolution and store it beside the fit.

    Assembling it means resolving the spec, reading 21 slab tables and
    evaluating the constraint across the posterior -- a second or two, paid
    every time the fit page is opened if it is done on demand. The inputs are
    all immutable once the fit is written, so the answer can never change:
    compute it once, here, and let the page load a file.

    Never fatal. A fit that ran is worth recording even if the summary of it
    cannot be built.
    """
    from nr_workbench.web.project import ProjectData

    try:
        payload = ProjectData(layout.root).trajectory(record.fit_id)
    except Exception as exc:  # noqa: BLE001 - a summary must not fail a fit
        click.echo(f"  ! could not summarise the time evolution: {exc}", err=True)
        return

    if not payload.get("traces"):
        return
    (fit_dir / "trajectory.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )
