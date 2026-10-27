"""``nrw aure`` -- set up, run, and import a first fit from AuRE.

AuRE is good at the one thing nr-workbench cannot do for you: propose a layer
stack from a sentence and iterate it against the data until it fits. It is not
a provenance system, and this package's offline fit checks are better than its
own, so the division of labour is deliberate:

* ``nrw aure new`` writes the setup, filling in the facts from disk and the
  prose from ``sample.md``, and validates it through AuRE's own loader.
* ``nrw aure run`` runs it, in the foreground, on a budget, and records the
  environment-only knobs AuRE does not record for itself.
* ``nrw aure import`` turns the fitted model into an ordinary model spec, after
  which everything downstream -- ``generate``, ``fit run``, ``assess``,
  ``promote`` -- is the normal path with full provenance.

The last step is the point. An AuRE run is reconnaissance; the fit that counts
is the one ``nrw fit run`` records.
"""

from __future__ import annotations

import itertools
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import click

from nr_workbench.project.layout import ProjectLayout, ProjectNotFoundError

#: The one knob a caller can change, and the value it takes when asked for.
#: Everything else in ``ENVIRONMENT_ONLY_KNOBS`` runs at its recorded default.
MODE_ENUMERATION_ON = "1"

#: What a ``quick`` run asks for. `de` rather than `lm` because a local
#: optimiser started from an LLM-proposed stack can sit in the wrong basin and
#: report a confident, wrong answer; differential evolution at least looks
#: around. One refinement, because the point of a first fit is to find out
#: whether the description was right, not to polish it.
QUICK_REFINEMENTS = 1

#: The run controls each budget writes into the setup. AuRE counts
#: `max_refinements` in evaluations, the first fit's included, so it is one
#: more than the refinements wanted.
BUDGETS: dict[str, dict[str, Any]] = {
    "quick": {
        "fit_method": "de",
        "fit_steps": 300,
        "max_refinements": QUICK_REFINEMENTS + 1,
    },
    "standard": {},  # AuRE's own defaults
}

#: Written beside the setup, naming the environment-only knobs a run used.
RUN_ENV_FILE = "run-env.json"


def _safe(value: str, what: str) -> str:
    """Return ``value`` if it is a plain name, else refuse.

    These come from the invoking user's own command line, so this grants no
    privilege they lack -- but a `--name ../../x` would write outside the
    project and then fail with a confusing `relative_to` error rather than a
    clear one, and the containment the rest of the layout assumes would be
    quietly untrue.
    """
    # Applied to --name as well as the sample: a name reaches the filesystem
    # exactly as a sample does. Matched whole: `$` let "name\n" through.
    from nr_workbench.project.samples import PLAIN_NAME_RE

    if not PLAIN_NAME_RE.fullmatch(value):
        raise click.ClickException(
            f"{what} {value!r} must be a plain name -- letters, digits, dot, "
            "dash and underscore -- not a path."
        )
    return value


def _layout() -> ProjectLayout:
    """Discover the project, or fail with guidance."""
    try:
        return ProjectLayout.discover()
    except ProjectNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc


def run_aure_new(
    *,
    sample: str,
    run: int | None = None,
    name: str | None = None,
    force: bool = False,
) -> None:
    """Write the AuRE setup for one run of one sample.

    Args:
        sample: The sample identifier.
        run: Which steady run to fit; inferred when there is only one.
        name: Name for this run; defaults to ``<sample>-<run>``.
        force: Overwrite an existing setup.

    Raises:
        click.ClickException: If the data or the description is missing, or the
            target exists and ``force`` was not given.
    """
    from nr_workbench.aure_setup import SETUP_FILE, SetupError, compose, setup_dir
    from nr_workbench.project.scan import load_register, scan_sample

    layout = _layout()
    _safe(sample, "sample")
    if name is not None:
        _safe(name, "--name")
    notes_path = layout.sample(sample) / "sample.md"
    if not notes_path.is_file():
        raise click.ClickException(
            f"{notes_path} does not exist. Run `nrw sample new {sample}` first."
        )
    notes = notes_path.read_text(encoding="utf-8")

    # The register wins over the disk where it exists, for the same reason
    # `nrw model new` prefers it: a beamtime folder holds alignment scans and
    # aborted runs that belong to the sample but not to a fit.
    found = load_register(layout.root, sample)
    if found is None:
        try:
            found = scan_sample(layout.root, sample)
        except FileNotFoundError as exc:
            raise click.ClickException(str(exc)) from exc

    try:
        composed = compose(
            sample=sample, scan=found, notes=notes, root=layout.root, run=run, name=name
        )
    except SetupError as exc:
        raise click.ClickException(str(exc)) from exc

    from nr_workbench.experiment.render import sample_md_pending

    if sample_md_pending(layout.root, sample):
        click.secho(
            f"  !  samples/{sample}/sample.md is not what the experiment catalog "
            "would write now:\n     edits saved on the Experiment page reach it "
            "when you Apply. AuRE reads it as it is.",
            fg="yellow",
        )

    run_name = composed.document["name"]
    target = setup_dir(layout.root, sample, run_name) / SETUP_FILE
    if target.exists() and not force:
        raise click.ClickException(
            f"{target.relative_to(layout.root)} already exists. Use --force to "
            "overwrite, or pass --name for a second setup on the same run."
        )

    validated = _validated(composed.document, target)
    click.echo(f"Wrote {target.relative_to(layout.root)}")

    click.echo()
    click.echo("  read from the files (exact, do not edit):")
    for line in composed.from_files:
        click.echo(f"    - {line}")
    click.echo("  read from sample.md (yours, check it):")
    for line in composed.from_notes:
        click.echo(f"    - {line}")
    for warning in composed.warnings:
        click.secho(f"  !  {warning}", fg="yellow")

    click.echo()
    click.echo(f"  {len(validated['states'][0]['data_files'])} data file(s) resolved.")
    click.echo("Next:")
    click.echo(f"  nrw aure run {target.relative_to(layout.root)}")


def _validated(document: dict[str, Any], target: Path) -> dict[str, Any]:
    """Write the setup, then load it back through AuRE's own parser.

    Writing first and validating the file on disk -- rather than validating the
    mapping and then writing -- is deliberate: AuRE resolves ``data_files``
    relative to the file's own location, so a mapping that validates in memory
    can still fail from where it ends up.
    """
    from nr_workbench.aure_adapter import (
        AureUnavailableError,
        SetupInvalidError,
        validate_setup,
    )

    target.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# AuRE setup, written by `nrw aure new`.\n"
        "#\n"
        "# `states` and `data_files` were read from the files on disk and are\n"
        "# exact -- the segment order is Q order. `sample_description` and\n"
        "# `hypothesis` came from sample.md and are yours to correct; AuRE\n"
        "# builds the entire model from them.\n"
        "#\n"
        "#   nrw aure run <this file>\n"
    )
    target.write_text(header + _dump(document), encoding="utf-8")

    try:
        return validate_setup(target)
    except SetupInvalidError as exc:
        # Usually the data rather than us: a file listed in sample.yaml that
        # is not on disk, or a run reduced two ways. Saying "report a bug"
        # here would send a ten-second fix to an issue tracker.
        raise click.ClickException(
            f"AuRE rejected the setup:\n  {exc}\n"
            "Check the data files it names are on disk and run "
            "`nrw sample scan`. If they are and this still fails, it is a bug "
            "in `nrw aure new` -- please report it with the file above."
        ) from exc
    except AureUnavailableError as exc:
        raise click.ClickException(str(exc)) from exc


def _dump(document: dict[str, Any]) -> str:
    """Render a setup as YAML a scientist would be willing to edit.

    Multi-line prose goes in a literal block rather than a quoted scalar with
    escaped newlines. The sample description is the field most likely to be
    corrected by hand, and a paragraph rendered as `'line one\n\n  line two'`
    invites the kind of edit that breaks the quoting.
    """
    import yaml

    class _Dumper(yaml.SafeDumper):
        pass

    def _str(dumper: yaml.Dumper, value: str):
        style = "|" if "\n" in value else None
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)

    _Dumper.add_representer(str, _str)
    return yaml.dump(
        document, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=88
    )


def run_aure_run(
    *,
    setup: str,
    budget: str = "quick",
    mode_enumeration: bool = False,
    dry_run: bool = False,
) -> None:
    """Run an AuRE analysis from a setup written by ``nrw aure new``.

    Args:
        setup: Path to the setup YAML.
        budget: ``quick`` or ``standard``.
        mode_enumeration: Enumerate discrete SLD seeds for thin layers. Slower,
            and the answer to a first pass that put a thin layer in the wrong
            basin.
        dry_run: Validate and print the plan; call no endpoint.

    Raises:
        click.ClickException: If the setup is invalid, no endpoint is
            configured, or the run fails.
    """
    from nr_workbench.aure_adapter import (
        AureUnavailableError,
        SetupInvalidError,
        llm_available,
        llm_info,
        validate_setup,
    )

    layout = _layout()
    setup_path = Path(setup).resolve()
    if not setup_path.is_file():
        raise click.ClickException(f"{setup} does not exist.")

    try:
        parsed = validate_setup(setup_path)
    except (AureUnavailableError, SetupInvalidError) as exc:
        raise click.ClickException(str(exc)) from exc

    if budget not in BUDGETS:
        raise click.ClickException(
            f"Unknown budget {budget!r}. Choose from: {', '.join(BUDGETS)}."
        )

    from nr_workbench.aure_setup import OUTPUT_DIR

    output = setup_path.parent / OUTPUT_DIR
    overrides = _run_environment(mode_enumeration=mode_enumeration)
    settings = BUDGETS[budget]

    click.echo(f"  setup     {_display(setup_path, layout.root)}")
    click.echo(f"  files     {len(parsed['states'][0]['data_files'])}")
    click.echo(
        f"  budget    {budget}" + (f" {settings}" if settings else " (AuRE defaults)")
    )
    shown = ", ".join(
        f"{key}={value}" if value else f"{key}=(unset)"
        for key, value in overrides.items()
    )
    click.echo(f"  knobs     {shown}")
    click.echo(f"  output    {_display(output, layout.root)}")

    if dry_run:
        click.echo()
        click.echo("  --dry-run: validated, nothing run.")
        return

    # Two mechanisms, as everywhere else here: the PreToolUse hook refuses the
    # command line, and this refuses from the inside. An AuRE run bills for as
    # long as it takes and substitutes a second, weaker model for the judgement
    # the harness was supposed to supply -- the same argument that makes
    # `nrw model new --from-notes` stand down under an agent.
    from nr_workbench.agent.guard import refuse_if_agent

    refuse_if_agent("aure-run")

    if not llm_available():
        from nr_workbench.aure_adapter import endpoint_hint

        raise click.ClickException(
            "AuRE is language-model driven and no endpoint is configured, so "
            f"there is nothing to run. {endpoint_hint()} "
            "`nrw check-llm --endpoint` makes a real call and says what it "
            "found; `.env.example` lists the variables."
        )
    info = llm_info()
    click.echo(f"  llm       {info.get('provider')}/{info.get('model')}")

    _apply_budget(setup_path, settings)
    try:
        # The file on disk is no longer the one validated above.
        validate_setup(setup_path)
    except (AureUnavailableError, SetupInvalidError) as exc:
        raise click.ClickException(
            f"Writing the budget into the setup made it invalid: {exc}"
        ) from exc

    output.mkdir(parents=True, exist_ok=True)
    (setup_path.parent / RUN_ENV_FILE).write_text(
        json.dumps(overrides, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    click.echo()
    code = _invoke(setup_path, output, overrides)
    if code != 0:
        raise click.ClickException(
            f"AuRE exited {code}. Its checkpoints are in "
            f"{_display(output / 'checkpoints', layout.root)}; `aure resume` "
            "can restart from the last one."
        )

    _report_result(output, layout.root)


def _run_environment(*, mode_enumeration: bool) -> dict[str, str]:
    """Return the environment-only knobs this run will use.

    Built from :data:`~nr_workbench.aure_adapter.ENVIRONMENT_ONLY_KNOBS` rather
    than from a second list here, so that adding a knob upstream is one edit in
    one place. A knob named there but not set here would be inherited from
    whoever's shell happened to export it -- which is the precise failure this
    function exists to prevent, and one nothing else would report.

    Every knob is set explicitly, including the ones left at their default.
    ``MODE_ENUMERATION`` is the only one a caller can change; it decides whether
    a thin layer is found at all.
    """
    from nr_workbench.aure_adapter import ENVIRONMENT_ONLY_KNOBS

    environment = dict(ENVIRONMENT_ONLY_KNOBS)
    if mode_enumeration:
        environment["MODE_ENUMERATION"] = MODE_ENUMERATION_ON
    return environment


def _apply_budget(setup_path: Path, settings: dict[str, Any]) -> None:
    """Write the budget's run controls into the setup, in place.

    These *do* have setup keys, so they belong in the file where they are part
    of the record, rather than in the environment where they are not.
    """
    if not settings:
        return
    import yaml

    document = yaml.safe_load(setup_path.read_text(encoding="utf-8")) or {}
    if all(document.get(key) == value for key, value in settings.items()):
        return
    document.update(settings)
    text = setup_path.read_text(encoding="utf-8")
    # Only the LEADING comment block. Collecting every `#` line in the file
    # would hoist a scientist's own annotation -- on a state, beside a data
    # file -- to the top of a file we told them to edit, stripped of the thing
    # it was next to.
    header = "".join(
        itertools.takewhile(lambda line: line.startswith("#"), text.splitlines(True))
    )
    stray = [
        line
        for line in text.splitlines()[len(header.splitlines()) :]
        if line.lstrip().startswith("#")
    ]
    if stray:
        raise click.ClickException(
            f"{setup_path.name} has comments below the header that this cannot "
            "preserve while rewriting the run controls into it:\n"
            + "".join(f"    {line.strip()}\n" for line in stray[:5])
            + "Move them into the header block at the top, or set the run "
            "controls in the file yourself and re-run with --budget standard."
        )
    setup_path.write_text(header + _dump(document), encoding="utf-8")


def _invoke(setup_path: Path, output: Path, overrides: dict[str, str]) -> int:
    """Run ``aure analyze`` in the foreground, streaming its output."""
    binary = _aure_binary()
    if binary is None:
        raise click.ClickException(
            "The `aure` command was not found beside this interpreter or on "
            "PATH. It installs with this package; `nrw doctor` reports whether "
            "the library is importable, and `pip install -e .` restores the "
            "console script."
        )

    # Ours last, deliberately: a knob exported in the user's shell must not
    # beat the value recorded in run-env.json, or the record is a lie. An
    # empty override means "leave it unset", which is a real upstream default
    # no number can express -- so it is removed rather than passed as "".
    environment = {**os.environ, **overrides}
    for key, value in overrides.items():
        if value == "":
            environment.pop(key, None)
    command = [
        binary,
        "analyze",
        "-c",
        str(setup_path),
        "-o",
        str(output),
        "--verbose",
    ]
    try:
        return subprocess.run(command, env=environment, check=False).returncode
    except OSError as exc:
        raise click.ClickException(f"Could not run {binary}: {exc}") from exc


def _aure_binary() -> str | None:
    """Locate the `aure` console script, preferring this interpreter's own.

    `shutil.which` alone takes whatever comes first on an inherited PATH, and
    the child is handed the LLM credential -- so on a shared analysis machine
    a stray `aure` earlier in PATH would receive it. The script installed
    beside the running interpreter is the one belonging to the environment
    that also provides the `aure` library we validated the setup with, which
    makes it both the safer and the more correct choice.
    """
    beside = Path(sys.executable).parent / "aure"
    if beside.is_file() and os.access(beside, os.X_OK):
        return str(beside)
    return shutil.which("aure")


def _report_result(output: Path, root: Path) -> None:
    """Print what the run produced, and the command that follows."""
    from nr_workbench.aure_import import (
        ImportError_,
        fitted_model,
        ordered_stack,
        read_final_state,
        reported_chisq,
    )

    try:
        state = read_final_state(output)
        model = fitted_model(state)
    except ImportError_ as exc:
        raise click.ClickException(str(exc)) from exc

    chisq = reported_chisq(state)
    click.echo()
    click.echo(
        f"  chi-squared  {chisq:.4g}" if chisq is not None else "  chi-squared  ?"
    )
    click.echo("  stack (incident medium last):")
    for entry in ordered_stack(model):
        thickness = entry.get("thickness")
        shown = f"{float(thickness):g} A" if thickness else "semi-infinite"
        click.echo(f"    {entry['name']:<12} rho {entry.get('sld')!s:<8} {shown}")

    click.echo()
    click.echo("Next:")
    click.echo(
        f"  nrw aure import {_display(output, root)} --sample <ID> --name <name>"
    )


def run_aure_import(
    *,
    output_dir: str,
    sample: str,
    name: str,
    run: int | None = None,
    force: bool = False,
    replace_unedited: bool = False,
) -> None:
    """Turn a finished AuRE run into an nrw model spec.

    Args:
        output_dir: The AuRE output directory.
        sample: Sample the spec belongs to.
        name: Model name; also the filename.
        run: Which steady run the states describe; inferred when there is one.
        force: Overwrite an existing spec.
        replace_unedited: Overwrite an existing spec only if it is a proposal
            of AuRE's nobody has edited since -- as a new quick fit does.

    Raises:
        click.ClickException: If the run holds no model, or the target exists
            and may not be replaced.
    """
    from nr_workbench.aure_import import (
        ImportError_,
        fitted_model,
        is_unedited_proposal,
        layer_names,
        read_final_state,
        reported_chisq,
        reported_fit,
        run_of,
        to_spec,
        untranslatable,
    )
    from nr_workbench.aure_setup import SetupError, choose_run
    from nr_workbench.commands.model import (
        _emit_spec,
        blank_angles_comment,
        state_for_run,
        warn_blank_angles,
    )
    from nr_workbench.project.scan import load_register, scan_sample

    layout = _layout()
    _safe(sample, "--sample")
    _safe(name, "--name")
    try:
        state = read_final_state(Path(output_dir))
        model = fitted_model(state)
    except ImportError_ as exc:
        raise click.ClickException(str(exc)) from exc

    try:
        found = load_register(layout.root, sample) or scan_sample(layout.root, sample)
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc

    # The setup beside the output names the files AuRE was actually given, so
    # prefer it over asking again -- an explicit --run still wins, for the case
    # where somebody knows better than the file.
    inferred = run if run is not None else run_of(Path(output_dir), found)
    if run is None and inferred is None and _names_files(Path(output_dir)):
        # The setup is readable and names files, but none of them belongs to
        # this sample. Pairing AuRE's stack with another sample's states would
        # validate, generate and fit -- the numbers would simply be about a
        # different measurement, and nothing would say so.
        raise click.ClickException(
            f"The run in {output_dir} was not fitted to any of {sample!r}'s "
            "data files. Importing it here would pair its layers with another "
            "sample's measurements. Check --sample, or pass --run explicitly "
            "if you really mean to."
        )
    try:
        chosen = choose_run(found, inferred)
        block, _, blank = state_for_run(layout.root, found.steady[chosen])
    except (SetupError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc

    target = layout.sample(sample) / "models" / f"{name}.yaml"
    if target.exists() and not force:
        # Checked as the file is written, not when a job was started minutes
        # before: an edit made while AuRE ran is never replaced.
        existing = target.read_text(encoding="utf-8")
        if not (replace_unedited and is_unedited_proposal(existing)):
            detail = (
                " and has been edited since AuRE proposed it, or was not proposed "
                "by AuRE; --replace-unedited replaces only a proposal nobody has "
                "edited"
                if replace_unedited
                else ""
            )
            raise click.ClickException(
                f"{target.relative_to(layout.root)} already exists{detail}. Use "
                "another --name, or --force to overwrite it anyway."
            )

    # What the model has no field for -- each segment's intensity, the sample
    # broadening, ranges AuRE filled in from its defaults -- is in the fit.
    fit = reported_fit(state)
    document = to_spec(
        model=model,
        sample=sample,
        name=name,
        states=[block],
        chisq=reported_chisq(state),
        fit=fit,
    )

    # AuRE names layers in prose; a spec's names are identifiers. The file
    # keeps AuRE's, so the stack can still be read against AuRE's report.
    renamed = [(aure, spec) for aure, spec in layer_names(model) if aure != spec]
    target.parent.mkdir(parents=True, exist_ok=True)
    from nr_workbench.codegen.generator import stamp_self_hash

    target.write_text(
        stamp_self_hash(
            _provenance_header(layout, target, Path(output_dir))
            + _renamed_comment(renamed)
            + blank_angles_comment(blank)
            + _emit_spec(document)
        ),
        encoding="utf-8",
    )
    click.echo(f"Wrote {target.relative_to(layout.root)}")
    if renamed:
        click.echo("  layer names, as a spec takes them (AuRE's in quotes):")
        for aure, spec in renamed:
            click.echo(f"    {spec}  {aure!r}")
    warn_blank_angles(blank)

    # Anything AuRE's fit had that this spec does not. Silence here would mean
    # an unconstrained spec quoting the chi-squared of a constrained fit.
    dropped = untranslatable(model, fit)
    if dropped:
        click.secho(
            "  !  AuRE's fit carried things this spec does not express:", fg="yellow"
        )
        for note in dropped:
            click.secho(f"       - {note}", fg="yellow")
        click.secho(
            "     Its chi-squared therefore belongs to a slightly different "
            "model than this one.",
            fg="yellow",
        )

    click.echo()
    click.echo("  The stack is AuRE's proposal, not a measurement. Check every")
    click.echo("  layer and every range before you quote anything from it.")
    click.echo("Next:")
    click.echo(f"  nrw model validate {target.relative_to(layout.root)}")
    click.echo(f"  nrw model generate {target.relative_to(layout.root)}")


def _renamed_comment(renamed: list[tuple[str, str]]) -> str:
    """The layers AuRE named otherwise, as comments; ``repr`` keeps each on one
    line whatever AuRE's name held."""
    if not renamed:
        return ""
    lines = ["# Layer names, as a spec takes them, and AuRE's own:"]
    lines += [f"#   {spec} = {aure!r}" for aure, spec in renamed]
    return "\n".join(lines) + "\n#\n"


def _provenance_header(layout: ProjectLayout, target: Path, output_dir: Path) -> str:
    """Say who proposed this stack, and under what.

    The same shape `nrw model new --from-notes` writes, for the same reason: a
    reader opening the file months later has to be able to tell a proposal from
    a measurement without going looking.
    """
    from nr_workbench.aure_adapter import resolved_commit
    from nr_workbench.aure_import import PROPOSED_MARKER
    from nr_workbench.commands.model import _schema_relative

    try:
        from importlib.metadata import version

        release = version("aure")
    except Exception:  # noqa: BLE001 - a missing version must not lose the spec
        release = "unknown"

    commit = resolved_commit() or "unknown"
    # An absent record has to be visible. Omitting the line would make a spec
    # imported from a moved or copied run indistinguishable from one whose run
    # was fully recorded.
    knobs = output_dir.parent / RUN_ENV_FILE
    knob_line = (
        f"# Physics knobs for that run are recorded in {knobs.name}.\n"
        if knobs.is_file()
        else "# The physics knobs for that run were NOT recorded -- no "
        f"{RUN_ENV_FILE}\n# beside the output directory, so what it ran with "
        "cannot be reconstructed.\n"
    )
    try:
        source = output_dir.resolve().relative_to(layout.root.resolve()).as_posix()
    except ValueError:
        source = output_dir.as_posix()
    return (
        f"# yaml-language-server: $schema={_schema_relative(layout, target)}\n"
        "#\n"
        f"# The stack below {PROPOSED_MARKER} {release} @ {commit[:12]},\n"
        "# from sample.md and the data. It is a starting point, not a\n"
        "# measurement -- check every layer and range before fitting.\n"
        "# States, angles and data_dir were read from the files' own headers\n"
        "# and were not proposed.\n"
        f"{knob_line}"
        f"#   from:        {source!r}\n"
        # Edited, this no longer holds -- and a new quick fit of the model then
        # leaves the file alone rather than replacing it.
        f"#   self sha256: {'0' * 64}  (nrw:self)\n"
        "#\n"
        "#   nrw model validate <this file>\n"
        "#   nrw model generate <this file>\n"
    )


def _names_files(output_dir: Path) -> bool:
    """Whether a setup beside this output names any data file at all.

    Distinguishes "this run belongs to another sample" from "there is no setup
    here to check against" -- only the first is worth refusing over.
    """
    from nr_workbench.aure_import import setup_files

    return bool(setup_files(output_dir))


def _display(path: Path, root: Path) -> str:
    """Return ``path`` relative to the project root when it is inside it."""
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)
