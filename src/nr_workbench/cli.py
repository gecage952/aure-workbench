"""Command-line entry point for nr-workbench.

A single Click group, exposed as both ``nr-workbench`` (readable in docs) and
``nrw`` (typeable at a prompt).

Every subcommand module is imported *lazily*, inside the callback that needs
it. That is not stylistic: importing ``aure`` costs roughly 1.5-3 seconds
because ``aure/__init__.py`` eagerly pulls in langchain-core, periodictable and
scipy, and refl1d/matplotlib are not cheap either. ``nrw --help`` must stay
instant, so nothing heavy may be imported at module scope here.
``tests/test_cli.py::test_help_does_not_import_heavy_modules`` enforces it.
"""

from __future__ import annotations

import click

from nr_workbench import __version__
from nr_workbench.fitters import FITTERS, refuse

CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


def _fitter(ctx: click.Context, param: click.Parameter, value: str) -> str:
    """Accept only the fitters on the menu, and say why when refusing.

    A `click.Choice` would refuse just as firmly and print `'de' is not one of
    'amoeba', 'de', 'dream'`, which reads as an arbitrary restriction. The reason is
    the substance of this limit --- the alternative to a different fitter is
    looking at the model --- so the refusal carries it.
    """
    del ctx, param
    if value is not None and value not in FITTERS:
        raise click.BadParameter(refuse(value))
    return value


@click.group(context_settings=CONTEXT_SETTINGS)
@click.version_option(__version__, "-V", "--version", prog_name="nr-workbench")
def main() -> None:
    """Neutron reflectometry workbench for SNS REF_L (BL-4B).

    Start with `nrw init` in an empty directory (or on top of an existing
    beamtime folder), then `nrw sample new <ID>` and copy your reduced data
    into the sample's data/ folders.
    """


@main.command("init")
@click.argument(
    "path",
    type=click.Path(file_okay=False, path_type=str),
    default=".",
    required=False,
)
@click.option(
    "--name",
    "project_name",
    default=None,
    help="Project name [default: directory name].",
)
@click.option("--beamtime", default=None, help="Beamtime label, e.g. 'june2026'.")
@click.option("--ipts", default=None, help="IPTS proposal number, e.g. 'IPTS-34567'.")
@click.option(
    "--sample",
    "sample_ids",
    multiple=True,
    help="Create a sample directory (repeatable).",
)
@click.option(
    "--check",
    is_flag=True,
    help="Report what would change and exit non-zero if anything would.",
)
@click.option(
    "--diff", "show_diff", is_flag=True, help="Print unified diffs instead of writing."
)
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite user-edited files (backed up under .nrw/backups/).",
)
@click.option("--no-skills", is_flag=True, help="Skip installing the bundled skills.")
@click.option(
    "--harness",
    "harnesses",
    multiple=True,
    help=(
        "Coding assistant to scaffold for (repeatable). "
        "[default: what nrw.toml records, or claude+copilot]"
    ),
)
@click.option(
    "--nested",
    is_flag=True,
    help="Scaffold here even if an ancestor directory is already a project.",
)
def init_command(**kwargs: object) -> None:
    """Scaffold (or upgrade) a workbench project at PATH."""
    from nr_workbench.commands.init_cmd import run_init

    run_init(**kwargs)  # type: ignore[arg-type]


@main.command("doctor")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
@click.option(
    "--fix-path",
    is_flag=True,
    help="Make bare `nrw` work in assistant sessions here (shows the change first).",
)
@click.option("--yes", is_flag=True, help="Skip the --fix-path confirmation.")
def doctor_command(as_json: bool, fix_path: bool, yes: bool) -> None:
    """Check the environment: versions, optional extras, project integrity.

    Reports what is *configured*. `nrw check-llm` makes a real call and
    reports what actually answered.
    """
    from nr_workbench.commands.doctor import run_doctor

    if fix_path:
        from nr_workbench.commands.doctor import run_fix_path

        run_fix_path(yes=yes)
        return

    run_doctor(as_json=as_json)


@main.command("check-llm")
@click.option(
    "--harness",
    is_flag=False,
    flag_value="",
    default=None,
    help=(
        "Probe only the coding harness. Give a name (claude, opencode) to "
        "probe that one [default: claude]."
    ),
)
@click.option("--endpoint", is_flag=True, help="Probe only the LLM_BASE_URL endpoint.")
@click.option(
    "--model",
    default=None,
    help="Model to test [default: whatever the harness resolves to]. On a "
    "third-party provider this is a deployment name.",
)
@click.option(
    "--timeout",
    type=int,
    default=None,
    help="Seconds to wait for the harness [default: 180].",
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def check_llm_command(
    harness: str | None,
    endpoint: bool,
    model: str | None,
    timeout: int | None,
    as_json: bool,
) -> None:
    """Make a real call to each language model and report what answered.

    `nrw doctor` reports what is configured; a deployment name that was never
    created, an expired key or a gateway that refuses all look configured. This
    calls them. With neither flag it probes both:

    \b
    - the harness (`claude`, or $NRW_HARNESS) that `nrw agent run` drives.
      This is what a Microsoft Foundry, Bedrock or Vertex setup applies to.
    - the LLM_BASE_URL endpoint used by `nrw assess`, `nrw model new
      --from-notes` and `nrw isaac export`.

    Having only one of the two is normal, so an unconfigured endpoint is
    reported rather than failed. Exits non-zero when something that was asked
    for did not answer.

    The harness probe is one real turn against your default model, so it costs
    what that model costs -- of the order of ten cents. The measured cost is
    printed. Probing something cheaper would not catch the failure this exists
    to find: a pinned model that is not deployed in your account.
    """
    from nr_workbench.commands.check_llm import DEFAULT_TIMEOUT, run_check_llm

    run_check_llm(
        harness=harness is not None,
        # `--harness` alone means "probe the harness"; `--harness opencode`
        # also says which. One flag rather than two, because a separate
        # --harness-name next to a boolean --harness reads as a mistake.
        harness_name=harness or None,
        endpoint=endpoint,
        model=model,
        timeout=DEFAULT_TIMEOUT if timeout is None else timeout,
        as_json=as_json,
    )


@main.command("handoff")
@click.argument("sample", required=False)
@click.option("--root", default=None, help="Project root; discovered if omitted.")
@click.option("--brief", is_flag=True, help="Drop the escalations and the digest.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def handoff_command(
    sample: str | None, root: str | None, brief: bool, as_json: bool
) -> None:
    """Everything a session taking over needs, in one command.

    Run this first when picking up from `nrw agent run`, rather than
    rediscovering the project by hand.
    """
    from nr_workbench.commands.handoff import run_handoff

    run_handoff(sample=sample, root=root, brief=brief, as_json=as_json)


@main.command("audience")
@click.option(
    "--set",
    "assignments",
    multiple=True,
    metavar="AXIS=VALUE",
    help="Set one axis, e.g. --set statistics=expert. Repeatable.",
)
@click.option("--ask", is_flag=True, help="Walk through the axes interactively.")
@click.option("--notes", default=None, help="Free text the axes cannot express.")
@click.option(
    "--guidance",
    "show_guidance",
    is_flag=True,
    help="Also print the instructions these settings imply.",
)
def audience_command(
    assignments: tuple[str, ...], ask: bool, notes: str | None, show_guidance: bool
) -> None:
    """Show or set who this project's output is written for."""
    from nr_workbench.commands.audience import run_audience

    run_audience(
        assignments=assignments,
        ask=ask,
        notes=notes,
        show_guidance=show_guidance,
    )


@main.group("sample")
def sample_group() -> None:
    """Create and inspect samples."""


@sample_group.command("new")
@click.argument("sample_id")
@click.option("--title", default=None, help="Human-readable sample title.")
@click.option(
    "--beamtime", default=None, help="Beamtime label [default: the project's]."
)
def sample_new_command(sample_id: str, title: str | None, beamtime: str | None) -> None:
    """Create samples/SAMPLE_ID/ with the standard layout and a sample.md stub."""
    from nr_workbench.commands.sample import run_sample_new

    run_sample_new(sample_id=sample_id, title=title, beamtime=beamtime)


@sample_group.command("scan")
@click.argument("sample_id", required=False)
@click.option(
    "--no-write",
    "write",
    flag_value=False,
    default=True,
    help="Report only; do not update sample.yaml.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def sample_scan_command(sample_id: str | None, write: bool, as_json: bool) -> None:
    """Register the data on disk for SAMPLE_ID, or every sample.

    Also reports where sample.md and the files disagree -- a run written up
    with no data, or data nobody wrote up.
    """
    from nr_workbench.commands.sample import run_sample_scan

    run_sample_scan(sample_id=sample_id, as_json=as_json, write=write)


@sample_group.command("reset")
@click.argument("sample_id")
@click.option(
    "--dry-run", is_flag=True, help="Report what would go and change nothing."
)
@click.option("--yes", is_flag=True, help="Skip the confirmation.")
def sample_reset_command(sample_id: str, dry_run: bool, yes: bool) -> None:
    """Clear SAMPLE_ID's fits, models and index entries together.

    Deleting result directories by hand does not work: the index still records
    the fits, `nrw ls` reports them BROKEN forever, and an unattended session --
    which reads the index, not the directory -- keeps numbering from models that
    are no longer there.

    Leaves data/, sample.md and reports/ alone, and refuses if the sample holds
    a promoted fit.
    """
    from nr_workbench.commands.sample import run_sample_reset

    run_sample_reset(sample_id=sample_id, dry_run=dry_run, yes=yes)


@main.group("model")
def model_group() -> None:
    """Write, check, and generate fit scripts from a model spec."""


@model_group.command("validate")
@click.argument("spec", type=click.Path(exists=True, dir_okay=False))
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
@click.option(
    "--result-out", default=None, help="Write an ndip-tool-result/1 manifest here."
)
def model_validate_command(**kwargs: object) -> None:
    """Check SPEC against the project: paths, layers, bounds, degrees of freedom."""
    from nr_workbench.commands.model import run_validate

    run_validate(**kwargs)  # type: ignore[arg-type]


@model_group.command("preview")
@click.argument("spec", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--build", is_flag=True, help="Also build the problem and report the initial chisq."
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def model_preview_command(**kwargs: object) -> None:
    """Show the parameter table SPEC resolves to, without writing anything.

    The check worth running before a long fit: it says how many parameters will
    vary and how they are grouped.
    """
    from nr_workbench.commands.model import run_preview

    run_preview(**kwargs)  # type: ignore[arg-type]


@model_group.command("generate")
@click.argument("spec", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--out", default=None, help="Output path [default: the spec with a .py suffix]."
)
@click.option("--force", is_flag=True, help="Overwrite a hand-edited script.")
def model_generate_command(**kwargs: object) -> None:
    """Write the standalone refl1d script for SPEC."""
    from nr_workbench.commands.model import run_generate

    run_generate(**kwargs)  # type: ignore[arg-type]


@model_group.command("new")
@click.argument("sample")
@click.option("--name", required=True, help="Model name; also the filename.")
@click.option("--out", default=None, help="Explicit output path.")
@click.option("--force", is_flag=True, help="Overwrite an existing spec.")
@click.option(
    "--from-notes",
    is_flag=True,
    help="Ask a configured LLM endpoint to propose the stack from sample.md.",
)
@click.option(
    "--print-prompt",
    is_flag=True,
    help="Write the skeleton, and print the instruction to hand a coding "
    "assistant, instead of asking a language model.",
)
def model_new_command(**kwargs: object) -> None:
    """Scaffold a spec for SAMPLE from the data found on disk.

    The stack is a placeholder for you to correct; everything else -- states,
    series, file discovery -- is filled in from what `nrw sample scan` sees.
    """
    from nr_workbench.commands.model import run_new

    run_new(**kwargs)  # type: ignore[arg-type]


@model_group.command("fork")
@click.argument("spec", type=click.Path(exists=True, dir_okay=False))
@click.option("--name", default=None, help="Name for the forked script.")
@click.option("--out", default=None, help="Explicit output path.")
def model_fork_command(**kwargs: object) -> None:
    """Take manual ownership of SPEC's generated script.

    The supported way to hand-edit. The fork is yours to change, and stays
    fully tracked by `nrw fit run` -- escaping the generator must not mean
    escaping provenance.
    """
    from nr_workbench.commands.model import run_fork

    run_fork(**kwargs)  # type: ignore[arg-type]


@model_group.command("deprecate")
@click.argument("spec", type=click.Path(exists=True, dir_okay=False))
@click.option("--reason", default=None, help="Why the model was abandoned.")
@click.option("--undo", is_flag=True, help="Remove an existing deprecation banner.")
def model_deprecate_command(**kwargs: object) -> None:
    """Mark SPEC as abandoned, at the top of the file.

    Specs are never deleted, because the fits they produced are part of the
    record -- which leaves an abandoned spec looking exactly like a live one,
    with the evidence in a NOTES.md three directories away. This puts it where
    anyone opening the file will see it, and makes `nrw model generate` refuse.

    Hash-neutral: nothing generated from the spec changes state.
    """
    from nr_workbench.commands.model import run_deprecate

    run_deprecate(**kwargs)  # type: ignore[arg-type]


@model_group.command("schema")
@click.option("--out", default=None, help="Where to write it; '-' for stdout.")
def model_schema_command(out: str | None) -> None:
    """Emit the JSON Schema for nrw-model/1."""
    from nr_workbench.commands.model import run_schema

    run_schema(out=out)


@model_group.command("forms")
def model_forms_command() -> None:
    """List the constraint forms available for a series."""
    from nr_workbench.commands.model import run_forms

    run_forms()


@main.group("aure")
def aure_group() -> None:
    """Get a first fit from AuRE, then bring it back under provenance."""


@aure_group.command("new")
@click.argument("sample")
@click.option("--run", type=int, default=None, help="Which steady run to fit.")
@click.option(
    "--name", default=None, help="Name for this run [default: <sample>-<run>]."
)
@click.option("--force", is_flag=True, help="Overwrite an existing setup.")
def aure_new_command(**kwargs: object) -> None:
    """Write the AuRE setup for SAMPLE from its data and its notes.

    The run and its segment files are read from disk and are exact. The
    sample description and the hypothesis come from `sample.md`, which is
    where they belong -- AuRE builds the whole model from that prose, so it
    is worth writing once, in the file, rather than into a prompt.
    """
    from nr_workbench.commands.aure_cmd import run_aure_new

    run_aure_new(**kwargs)  # type: ignore[arg-type]


@aure_group.command("run")
@click.argument("setup", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--budget",
    type=click.Choice(["quick", "standard"]),
    default="quick",
    show_default=True,
    help="quick: de, few steps, one refinement. standard: AuRE's defaults.",
)
@click.option(
    "--mode-enumeration",
    is_flag=True,
    help="Enumerate SLD seeds for thin layers. Slower; the answer to a first "
    "pass that landed a thin layer in the wrong basin.",
)
@click.option("--dry-run", is_flag=True, help="Validate and print the plan only.")
def aure_run_command(**kwargs: object) -> None:
    """Run the analysis described by SETUP, in the foreground.

    Needs a language-model endpoint: AuRE is LLM-driven and will not fit
    without one. The environment-only knobs it does not record for itself are
    written beside the setup as `run-env.json`.
    """
    from nr_workbench.commands.aure_cmd import run_aure_run

    run_aure_run(**kwargs)  # type: ignore[arg-type]


@aure_group.command("import")
@click.argument("output_dir", type=click.Path(exists=True, file_okay=False))
@click.option("--sample", required=True, help="Sample the spec belongs to.")
@click.option("--name", required=True, help="Model name; also the filename.")
@click.option("--run", type=int, default=None, help="Which steady run it fitted.")
@click.option("--force", is_flag=True, help="Overwrite an existing spec.")
@click.option(
    "--replace-unedited",
    is_flag=True,
    help="Replace an existing spec only if it is a proposal of AuRE's that nobody "
    "has edited since -- as a new quick fit of the model does.",
)
def aure_import_command(**kwargs: object) -> None:
    """Turn the fitted model in OUTPUT_DIR into a model spec.

    This is where an AuRE run stops being reconnaissance: everything after it
    -- generate, fit run, assess, promote -- is the normal path, and the fit
    that counts is the one `nrw fit run` records.
    """
    from nr_workbench.commands.aure_cmd import run_aure_import

    run_aure_import(**kwargs)  # type: ignore[arg-type]


@main.group("fit")
def fit_group() -> None:
    """Run fits and record what produced every result."""


@fit_group.command("run")
@click.argument("script", required=False, type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--from",
    "from_fit",
    metavar="FIT_ID",
    default=None,
    help="Run this fit again from its own problem file, instead of a script, "
    "with its fitter and settings but for those given.",
)
@click.option(
    "--sample",
    default=None,
    help="Sample to record under [default: inferred from the path; with "
    "--from, the fit's].",
)
@click.option(
    "--method",
    metavar="[amoeba|de|dream]",
    callback=_fitter,
    default=None,
    help=(
        "amoeba while you are still changing the model, de when amoeba is "
        "stalling on the starting point rather than the model, dream to quote "
        "a number. [default: nrw.toml [fit] method, else dream]"
    ),
)
@click.option(
    "--steps",
    type=int,
    default=None,
    help="Maximum optimizer steps. [default: nrw.toml [fit.<method>], else bumps']",
)
@click.option(
    "--samples",
    type=int,
    default=None,
    help="DREAM sample count. [default: nrw.toml [fit.dream], else 10000]",
)
@click.option(
    "--burn",
    type=int,
    default=None,
    help="DREAM burn-in. [default: nrw.toml [fit.dream], else 100]",
)
@click.option(
    "--pop",
    type=int,
    default=None,
    help="Population size, for de and dream. [default: nrw.toml, else 10]",
)
@click.option(
    "--seed",
    type=int,
    default=None,
    help="Random seed, for a reproducible run. [default: nrw.toml [fit], else none]",
)
@click.option(
    "--parallel",
    type=int,
    default=None,
    help="CPUs to use; 0 means all of them, 1 forces serial. "
    "[default: nrw.toml [fit], else 0]",
)
@click.option(
    "--plots",
    is_flag=True,
    help="Let bumps render its PNGs (off by default; it runs before the chain "
    "is saved, so a failure costs the uncertainty output).",
)
@click.option(
    "--verbose",
    is_flag=True,
    help="Stream the fitter's own progress. Off by default -- the same log is "
    "written to fit/<model>.out, and on a DREAM run the live output is long "
    "enough that an unattended session spends turns paging it back in.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit the fit record as JSON. The write path had no machine-readable "
    "output, so a driver could not tell whether a fit succeeded except by "
    "re-reading the index.",
)
@click.option("--note", default=None, help="Free-text note stored in the record.")
@click.option(
    "--name",
    "model_name",
    default=None,
    help="Model name [default: the script stem; with --from, the fit's].",
)
@click.option("--dry-run", is_flag=True, help="Report what would run; write nothing.")
def fit_run_command(**kwargs: object) -> None:
    """Run SCRIPT, or the fit named by --from again, and record the fit.

    Works on hand-written refl1d scripts as they are: no migration, no schema.
    The script must define a module-level `problem = FitProblem(...)`. With
    --from, the fit's own problem file runs: its model, its parameters at the
    fit's values, and its data, as bumps saved them.
    """
    from nr_workbench.commands.fit import run_fit_command

    run_fit_command(**kwargs)  # type: ignore[arg-type]


@fit_group.command("star")
@click.argument("fit_id")
def fit_star(fit_id: str) -> None:
    """Star FIT_ID: one worth coming back to."""
    from nr_workbench.commands.curate import run_curate

    run_curate("star", fit_id)


@fit_group.command("unstar")
@click.argument("fit_id")
def fit_unstar(fit_id: str) -> None:
    """Take FIT_ID's star away."""
    from nr_workbench.commands.curate import run_curate

    run_curate("unstar", fit_id)


@fit_group.command("discard")
@click.argument("fit_id")
@click.option(
    "--reason", required=True, help="Why it is set aside. Required; kept with it."
)
def fit_discard(fit_id: str, reason: str) -> None:
    """Set FIT_ID aside: out of the listings, every file kept.

    `nrw fit restore` brings it back; `nrw fit delete` then frees the disk.
    """
    from nr_workbench.commands.curate import run_curate

    run_curate("discard", fit_id, reason=reason)


@fit_group.command("restore")
@click.argument("fit_id")
def fit_restore(fit_id: str) -> None:
    """Bring a discarded FIT_ID back into the listings."""
    from nr_workbench.commands.curate import run_curate

    run_curate("restore", fit_id)


@fit_group.command("delete")
@click.argument("fit_id")
@click.option("--yes", is_flag=True, help="Skip the confirmation.")
def fit_delete(fit_id: str, yes: bool) -> None:
    """Delete a discarded FIT_ID's files; the record that it ran stays.

    Refused while a report, a figure, another fit or an ISAAC record uses it.
    """
    from nr_workbench.commands.curate import run_curate

    run_curate("delete", fit_id, yes=yes)


@main.group("tnr")
def tnr_group() -> None:
    """Assess temporal change in a time-resolved run."""


def _reference_options(func):
    """Attach the block-selection options every tNR command shares.

    Defined here rather than imported so that `nrw --help` still touches
    nothing heavy: importing anything under `nr_workbench.tnr` pulls in numpy
    and every metric through the package __init__.
    """
    options = (
        click.option(
            "--min-duration",
            type=float,
            default=5.0,
            show_default=True,
            help="Skip intervals shorter than this many seconds.",
        ),
        click.option(
            "--json-path",
            "json_path",
            type=click.Path(exists=True, dir_okay=False),
            default=None,
            help="Reduction JSON [default: discovered in DATA_DIR].",
        ),
        click.option(
            "--ref-seconds",
            type=float,
            default=None,
            help="Reference block: intervals within N s of the run start.",
        ),
        click.option(
            "--ref-labels",
            default=None,
            help="Reference block: comma-separated labels or globs.",
        ),
        click.option(
            "--late-seconds",
            type=float,
            default=None,
            help="Late block: intervals within N s of the run end.",
        ),
        click.option(
            "--late-labels",
            default=None,
            help="Late block: comma-separated labels or globs.",
        ),
        click.option(
            "--min-ref-snr",
            type=float,
            default=0.0,
            show_default=True,
            help="Drop reference Q bins below this many sigma above zero.",
        ),
        click.option(
            "--ref-loo/--no-ref-loo",
            default=True,
            show_default=True,
            help="Exact leave-one-out variance for reference members; "
            "disabling it biases their chi-squared low by about 10%.",
        ),
    )
    for option in reversed(options):
        func = option(func)
    return func


@tnr_group.command("assess")
@click.argument("data_dir", type=click.Path(exists=True, file_okay=False))
@click.option(
    "--out", default=None, help="Output directory [default: ../assessments/<label>]."
)
@click.option("--label", default="", help="Filename prefix and plot-title label.")
@click.option(
    "--template",
    type=click.Choice(["late", "pca"]),
    default="late",
    show_default=True,
    help="Template source for the amplitude projection.",
)
@click.option(
    "--template-smooth",
    type=int,
    default=9,
    show_default=True,
    help="Boxcar width for the template, in Q points.",
)
@click.option(
    "--qbands", type=int, default=4, show_default=True, help="Number of Q bands."
)
@click.option(
    "--qband-edges", default=None, help="Explicit band edges, comma-separated."
)
@click.option("--variogram-bins", type=int, default=6, show_default=True)
@click.option("--variogram-min-pairs", type=int, default=3, show_default=True)
@click.option(
    "--variogram-lag-scale",
    type=click.Choice(["log", "linear"]),
    default="log",
    show_default=True,
)
@click.option(
    "--delta2-clip",
    type=click.Choice(["mean", "element"]),
    default="mean",
    show_default=True,
)
@click.option(
    "--heatmap-coadd-seconds",
    type=float,
    default=None,
    help="Coadd the residual heatmap into blocks of this many seconds.",
)
@click.option("--no-plots", is_flag=True, help="Write tables only; skip the PNGs.")
@click.option(
    "--result-out", default=None, help="Write an ndip-tool-result/1 manifest here."
)
@click.option("--json", "as_json", is_flag=True, help="Emit the assessment as JSON.")
@_reference_options
def tnr_assess_command(**kwargs: object) -> None:
    """Run every metric on DATA_DIR, in the documented reading order.

    Writes the plots and tables under their original names, plus an
    assessment.json carrying the machine-readable verdict.
    """
    from nr_workbench.commands.tnr_cmd import run_assess

    run_assess(**kwargs)  # type: ignore[arg-type]


@tnr_group.command("manifest")
@click.argument("data_dir", type=click.Path(exists=True, file_okay=False))
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
@_reference_options
def tnr_manifest_command(**kwargs: object) -> None:
    """Show DATA_DIR's interval structure without computing any metric."""
    from nr_workbench.commands.tnr_cmd import run_manifest

    run_manifest(**kwargs)  # type: ignore[arg-type]


def _add_metric_command(name: str, help_text: str) -> None:
    """Register a per-metric subcommand sharing the assess options."""

    @tnr_group.command(name, help=help_text)
    @click.argument("data_dir", type=click.Path(exists=True, file_okay=False))
    @click.option("--out", default=None, help="Output directory.")
    @click.option("--label", default="", help="Filename prefix.")
    @click.option("--no-plots", is_flag=True, help="Write tables only.")
    @click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
    @_reference_options
    def _command(
        data_dir: str,
        out: str | None,
        label: str,
        no_plots: bool,
        as_json: bool,
        **shared: object,
    ) -> None:
        from nr_workbench.commands.tnr_cmd import run_metric

        run_metric(
            name,
            data_dir=data_dir,
            out=out,
            label=label,
            as_json=as_json,
            extra={"no_plots": no_plots},
            **shared,
        )

    _command.__name__ = f"tnr_{name}_command"


for _name, _help in (
    ("amplitude", "The change amplitude a(t) +- sigma: the primary metric."),
    ("variogram", "The lag variogram: is anything changing, and on what timescale?"),
    ("qbands", "Where in Q the change lives."),
    ("chi2", "Running chi-squared, the fractional change delta, and significance."),
    ("pca", "PCA of the R(Q,t) matrix."),
    ("kl", "Symmetric KL divergence per interval."),
):
    _add_metric_command(_name, _help)


@main.command("whence")
@click.argument("path")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def whence_command(path: str, as_json: bool) -> None:
    """Trace PATH back to the fit that produced or consumed it.

    Accepts a figure, an artifact, a data file, a fit directory, or a fit id.
    """
    from nr_workbench.commands.provenance_cmd import run_whence

    run_whence(path=path, as_json=as_json)


@main.command("ls")
@click.option("--sample", default=None, help="Restrict to one sample.")
@click.option("--limit", type=int, default=50, show_default=True, help="Maximum rows.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
@click.option(
    "--all", "show_all", is_flag=True, help="Include discarded and deleted fits."
)
def ls_command(sample: str | None, limit: int, as_json: bool, show_all: bool) -> None:
    """List recorded fits, newest first."""
    from nr_workbench.commands.provenance_cmd import run_ls

    run_ls(sample=sample, as_json=as_json, limit=limit, show_all=show_all)


@main.command("promote")
@click.argument("fit_id")
@click.option(
    "--as", "label", default="final", show_default=True, help="Label to apply."
)
@click.option("--reason", required=True, help="Why this fit is the answer. Required.")
@click.option("--force", is_flag=True, help="Promote even if the inputs have changed.")
def promote_command(fit_id: str, label: str, reason: str, force: bool) -> None:
    """Mark FIT_ID as the answer, recording who decided and why."""
    from nr_workbench.commands.provenance_cmd import run_promote

    run_promote(fit_id=fit_id, label=label, reason=reason, force=force)


@main.group("isaac")
def isaac_group() -> None:
    """Publish results as ISAAC AI-Ready Records."""


@isaac_group.command("export")
@click.argument("fit_id")
@click.option(
    "--out", "-o", default=None, help="Where to write [default: the fit's isaac/]."
)
@click.option(
    "--context",
    default=None,
    help="Notes for the record [default: the fit's NOTES.md].",
)
@click.option("--upload", is_flag=True, help="Push the records to the ISAAC Portal.")
@click.option(
    "--validate-only",
    is_flag=True,
    help="With --upload, ask the API to validate without persisting.",
)
@click.option("--yes", is_flag=True, help="Skip the upload confirmation.")
@click.option(
    "--force",
    is_flag=True,
    help="Replace an output directory nrw did not write.",
)
@click.option(
    "--no-llm",
    is_flag=True,
    help="Do not ask a language model to read sample.md for conditions.",
)
def isaac_export_command(**kwargs: object) -> None:
    """Export FIT_ID as ISAAC AI-Ready Records.

    Each state becomes one record, with its angle segments assembled into a
    single measurement rather than mistaken for separate ones. A co-refinement
    therefore yields one record per condition, all sharing a sample id so the
    portal reads them as one experiment.

    Needs `data-assembler` and `nr-isaac-format`, which own the schema
    mapping: re-run the installer with NRW_EXTRAS=isaac (docs/install.md).
    """
    from nr_workbench.commands.isaac_cmd import run_export

    run_export(**kwargs)  # type: ignore[arg-type]


@isaac_group.command("push")
@click.argument("fit_id")
@click.option(
    "--validate-only",
    is_flag=True,
    help="Ask the API whether the records would be accepted, without keeping them.",
)
@click.option("--yes", is_flag=True, help="Skip the confirmation.")
def isaac_push_command(fit_id: str, validate_only: bool, yes: bool) -> None:
    """Send FIT_ID's exported records to the ISAAC Portal.

    The records `nrw isaac export` wrote, unchanged: what the server validated
    is what is published. Only the final fit of a sample is published, and
    each push is recorded in the fit index. ISAAC_URL and ISAAC_KEY are read
    as every other setting is.
    """
    from nr_workbench.commands.isaac_cmd import run_push

    run_push(fit_id=fit_id, validate_only=validate_only, yes=yes)


@main.group("tiled")
def tiled_group() -> None:
    """Tiled: push the catalog and the fits, say what is there, pull them back.

    Needs the installer's NRW_EXTRAS=tiled. The server is TILED_URL
    (tiled.ornl.gov by default), read as nrw's other settings are. Each
    command acts with your login, so an agent is refused them all.
    """


@tiled_group.command("login")
@click.option("--root", type=click.Path(file_okay=False), help="Project root.")
def tiled_login_command(**kwargs: object) -> None:
    """Log in through Globus: Tiled prints an address and a code to enter."""
    from nr_workbench.commands.tiled_cmd import run_login

    run_login(**kwargs)  # type: ignore[arg-type]


@tiled_group.command("logout")
@click.option("--root", type=click.Path(file_okay=False), help="Project root.")
def tiled_logout_command(**kwargs: object) -> None:
    """End the login the Tiled client keeps for your server."""
    from nr_workbench.commands.tiled_cmd import run_logout

    run_logout(**kwargs)  # type: ignore[arg-type]


@tiled_group.command("status")
@click.option("--root", type=click.Path(file_okay=False), help="Project root.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def tiled_status_command(**kwargs: object) -> None:
    """Whose your login is, and whether this experiment is on Tiled."""
    from nr_workbench.commands.tiled_cmd import run_status

    run_status(**kwargs)  # type: ignore[arg-type]


@tiled_group.command("push")
@click.option("--root", type=click.Path(file_okay=False), help="Project root.")
@click.option(
    "--only",
    type=click.Choice(["context", "fits"]),
    default=None,
    help="Push only the catalog (context/) or only the fits (analysis/).",
)
@click.option(
    "--dry-run", is_flag=True, help="Say what would be written; write nothing."
)
@click.option("--yes", is_flag=True, help="Skip the confirmation.")
def tiled_push_command(**kwargs: object) -> None:
    """Write the catalog to context/, and the starred and final fits to analysis/.

    The last writer wins: a record that differs is written over Tiled's copy,
    keeping fields other programs keep on it. Records Tiled alone holds stay.
    """
    from nr_workbench.commands.tiled_cmd import run_push

    run_push(**kwargs)  # type: ignore[arg-type]


@tiled_group.command("pull")
@click.option(
    "--context", is_flag=True, help="The catalog's runs, samples and context."
)
@click.option("--data", is_flag=True, help="The experiment's reduced files.")
@click.option("--fits", is_flag=True, help="The fits, as they were recorded.")
@click.option(
    "--run", "runs", type=int, multiple=True, help="Only this run; repeat for more."
)
@click.option(
    "--into",
    type=click.Path(file_okay=False),
    help="Copy into this folder [default: the configured data folder].",
)
@click.option(
    "--dry-run", is_flag=True, help="Say what would be pulled; write nothing."
)
@click.option("--yes", is_flag=True, help="Skip the confirmation.")
@click.option("--root", type=click.Path(file_okay=False), help="Project root.")
def tiled_pull_command(**kwargs: object) -> None:
    """Bring back the catalog, the reduced files and the fits from Tiled.

    --context writes Tiled's records into the catalog, Tiled's copy winning.
    --data copies each reduced file the data folder lacks; the folder watcher
    does the rest. --fits rebuilds each fit as it was recorded, with the
    reduced files it read, and writes its spec and script into its sample's
    models/; `nrw fit run --from` runs it again.
    """
    from nr_workbench.commands.tiled_cmd import run_pull

    run_pull(**kwargs)  # type: ignore[arg-type]


@main.group("agent")
def agent_group() -> None:
    """Run and constrain an unattended analysis harness."""


@agent_group.command("guard")
@click.option(
    "--command",
    default=None,
    help="Judge this command [default: read a PreToolUse payload from stdin].",
)
def agent_guard_command(command: str | None) -> None:
    """Refuse the commands an unattended agent must not run.

    Wired into a project's `.claude/settings.json` as a PreToolUse hook, so
    the refusal happens before the command executes rather than depending on
    the model agreeing. Exits 2 to block, 0 to allow.
    """
    from nr_workbench.agent.guard import run_guard

    run_guard(command)


@agent_group.command("status")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def agent_status_command(as_json: bool) -> None:
    """Show which unattended sessions are running.

    A stale entry -- a pidfile whose process is gone -- is reported rather than
    hidden: it means a session died without cleaning up.
    """
    from nr_workbench.commands.agent_cmd import run_agent_status

    run_agent_status(as_json=as_json)


@agent_group.command("stop")
@click.argument("sample", required=False)
def agent_stop_command(sample: str | None) -> None:
    """Stop the unattended session on SAMPLE, or every running session.

    Kills the process group, not just the harness: it launches refl1d, and a fit
    that outlives its session keeps writing into the project.
    """
    from nr_workbench.commands.agent_cmd import run_agent_stop

    run_agent_stop(sample=sample)


@agent_group.command("run")
@click.argument("sample")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Compose the prompt and print it; start no session.",
)
@click.option(
    "--turns",
    default=None,
    type=int,
    help="Cap on harness turns [default: 200].",
)
@click.option("--model", default=None, help="Model to run [default: the harness's].")
@click.option(
    "--timeout",
    default=None,
    type=int,
    help="Seconds before the session is killed [default: none].",
)
@click.option(
    "--quiet",
    is_flag=True,
    help="Print no progress; the transcript still records everything.",
)
@click.option(
    "--again",
    is_flag=True,
    help="Run even though the sample already has a written report.",
)
@click.option(
    "--harness",
    default=None,
    help="Which harness to drive [default: claude].",
)
@click.option(
    "--task-file",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="The session's task, in place of `## Fits to perform`.",
)
def agent_run_command(
    sample: str,
    dry_run: bool,
    turns: int | None,
    model: str | None,
    timeout: int | None,
    quiet: bool,
    again: bool,
    harness: str | None,
    task_file: str | None,
) -> None:
    """Run one unattended analysis session over SAMPLE.

    The task comes from `## Fits to perform` in the sample's notes, or from
    --task-file when whoever starts the session has planned it (nr-watcher
    does, for the whole experiment). With neither this refuses to start,
    because deciding what is worth fitting is the one thing an unattended
    session must not do.

    It also refuses when the sample already has a written report, since the task
    text does not change when the work is finished. Say what is left under
    `## Fits to perform`, or pass --again for a deliberate second pass.
    """
    from nr_workbench.agent.session import DEFAULT_TURNS, SessionError, compose
    from nr_workbench.agent.session import run as run_session
    from nr_workbench.project.layout import ProjectLayout, ProjectNotFoundError

    try:
        root = ProjectLayout.discover().root
    except ProjectNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc

    task = None
    if task_file is not None:
        from pathlib import Path

        task = Path(task_file).read_text(encoding="utf-8").strip()
        if not task:
            raise click.ClickException(f"{task_file} is empty: there is no task in it.")

    try:
        if dry_run:
            session = compose(root, sample, again=again, task=task)
            click.echo(session.prompt)
            return
        session = run_session(
            root,
            sample,
            turns=DEFAULT_TURNS if turns is None else turns,
            model=model,
            harness=harness,
            timeout=timeout,
            on_progress=None if quiet else click.echo,
            again=again,
            task=task,
        )
    except SessionError as exc:
        raise click.ClickException(str(exc)) from exc

    where = session.transcript.relative_to(root) if session.transcript else "?"
    click.echo(f"Session finished (exit {session.returncode}); transcript {where}")
    escalations = root / "ESCALATIONS.md"
    if escalations.is_file():
        click.echo(
            f"  ! {escalations.name} exists -- read it before promoting anything"
        )


@agent_group.command("watch")
@click.argument("samples", nargs=-1)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Report what each measurement is waiting for; start nothing.",
)
@click.option(
    "--settle",
    default=None,
    type=int,
    help="Seconds a measurement's files must be unchanged [default: 300].",
)
@click.option(
    "--poll", default=None, type=int, help="Seconds between polls [default: 60]."
)
@click.option(
    "--max-sessions",
    default=None,
    type=int,
    help="Stop after this many sessions [default: run until interrupted].",
)
@click.option(
    "--session-timeout",
    default=None,
    type=int,
    help="Seconds before one session is killed [default: 7200].",
)
@click.option("--turns", default=None, type=int, help="Cap on turns per session.")
@click.option("--model", default=None, help="Model to run [default: the harness's].")
@click.option(
    "--harness",
    default=None,
    help="Which harness to drive [default: claude].",
)
def agent_watch_command(
    samples: tuple[str, ...],
    dry_run: bool,
    settle: int | None,
    poll: int | None,
    max_sessions: int | None,
    session_timeout: int | None,
    turns: int | None,
    model: str | None,
    harness: str | None,
) -> None:
    """Watch SAMPLES for settled measurements and analyse each once.

    A scheduler, not a second decision-maker: it decides *when* a session
    starts and over what, and `nrw agent run` does the rest. A measurement is
    started only when its files have stopped changing, its segments are
    coherent, and nothing has fitted it yet.

    With no SAMPLES, every sample in the project is watched.
    """
    from nr_workbench.agent import watch as watcher
    from nr_workbench.project.layout import ProjectLayout, ProjectNotFoundError

    try:
        layout = ProjectLayout.discover()
    except ProjectNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc

    chosen = list(samples) or layout.list_samples()
    if not chosen:
        raise click.ClickException("No samples yet; run `nrw sample new <ID>`.")

    settle_seconds = watcher.DEFAULT_SETTLE_SECONDS if settle is None else settle

    if dry_run:
        found = watcher.once(
            layout.root, chosen, watcher.WatchState(), settle_seconds=settle_seconds
        )
        for sample, verdicts in found.items():
            click.echo(sample)
            for verdict in verdicts:
                mark = "\u2192" if verdict.ready else " "
                click.echo(
                    f"  {mark} run {verdict.run} ({verdict.kind})  "
                    f"{verdict.state:<12} {verdict.reason}"
                )
        return

    # Ctrl-C is handled inside watch(), which returns what it managed rather
    # than raising over the last session's output.
    started = watcher.watch(
        layout.root,
        chosen,
        settle_seconds=settle_seconds,
        poll_seconds=watcher.DEFAULT_POLL_SECONDS if poll is None else poll,
        max_sessions=max_sessions,
        session_timeout=(
            watcher.DEFAULT_SESSION_TIMEOUT
            if session_timeout is None
            else session_timeout
        ),
        turns=turns,
        model=model,
        harness=harness,
        on_event=click.echo,
    )
    click.echo(f"{started} session(s) run.")


@main.command("assess")
@click.argument("fit_id")
@click.option(
    "--no-write",
    "write",
    flag_value=False,
    default=True,
    help="Print only; do not append to the fit's NOTES.md.",
)
@click.option(
    "--no-llm",
    "use_llm",
    flag_value=False,
    default=True,
    help="Skip the language-model reading even if an endpoint is configured.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def assess_command(fit_id: str, write: bool, use_llm: bool, as_json: bool) -> None:
    """Check FIT_ID and record what the checks found.

    Always runs the arithmetic: parameters on their bounds, posteriors that
    span most of their prior, a best fit outside its own credible interval,
    BIC. With an LLM endpoint configured it also asks whether the values are
    physically sensible, given sample.md and the skills its notes call for.

    The result is appended to the fit's NOTES.md, so it travels in a bundle
    and shows on the fit page.
    """
    from nr_workbench.commands.assess import run_assess

    run_assess(fit_id=fit_id, write=write, use_llm=use_llm, as_json=as_json)


@main.command("note")
@click.argument("target", required=False)
@click.option("-m", "--message", default=None, help="Text to append.")
@click.option(
    "--sample",
    default=None,
    help="Write a report about a SAMPLE instead of one fit.",
)
@click.option("--title", default=None, help="Title for a new sample report.")
@click.option("--edit", is_flag=True, help="Open it in $EDITOR afterwards.")
@click.option("--why", default=None, help="Fill 'Why this run': what you were testing.")
@click.option(
    "--showed", default=None, help="Fill 'What it showed': the result in words."
)
@click.option(
    "--caveat",
    default=None,
    help="Fill 'Caveats': what not to conclude from this fit.",
)
def note_command(
    target: str | None,
    message: str | None,
    sample: str | None,
    title: str | None,
    edit: bool,
    why: str | None,
    showed: str | None,
    caveat: str | None,
) -> None:
    """Write or read the notes attached to a fit or a sample.

    With no -m, shows every note that mentions TARGET -- the fit's own
    NOTES.md and any sample report that cites it.

    \b
    nrw note 0103d9c7 -m "oxide sits on its floor; conditional, not measured"
    nrw note 0103d9c7
    nrw note --sample expt11 --title "why the tNR is fitted alone" -m "..."
    """
    from nr_workbench.commands.note import run_note

    run_note(
        target=target,
        message=message,
        title=title,
        sample=sample,
        edit=edit,
        why=why,
        showed=showed,
        caveat=caveat,
    )


@main.command("report")
@click.argument("sample", required=False)
@click.option(
    "--topic",
    default=None,
    help="Slug for a report on a separate question, so new work needs no overwrite.",
)
@click.option(
    "--tier",
    default=None,
    help="Write only one tier: technical, si, or plain. All three by default.",
)
@click.option(
    "--concept",
    multiple=True,
    help="Explain an extra concept in the plain tier, by slug. Repeatable.",
)
@click.option(
    "--concepts",
    "list_concepts",
    is_flag=True,
    help="List the concepts detected for this sample, and why.",
)
@click.option(
    "--check",
    "check_only",
    is_flag=True,
    help="Verify the tiers agree with each other, and write nothing.",
)
@click.option("--root", default=None, help="Project root. Discovered if omitted.")
@click.option(
    "--force",
    is_flag=True,
    help="Refresh the generated sequence table in an existing report.",
)
@click.option("--stdout", is_flag=True, help="Print it instead of writing a file.")
def report_command(
    sample: str | None,
    topic: str | None,
    tier: str | None,
    concept: tuple[str, ...],
    list_concepts: bool,
    check_only: bool,
    root: str | None,
    force: bool,
    stdout: bool,
) -> None:
    """Scaffold a sample's report at three levels, with its fit sequence filled in.

    A beamtime result is read by a mixed team: someone who will argue with the
    model, someone writing it up, and someone who owns the chemistry and does
    not fit reflectivity. All three tiers are written, because a document that
    serves one of those readers well serves the others badly.

    The ordered fit chain is generated because it is derivable; the reasoning is
    left blank because it is the analysis.
    """
    from nr_workbench.commands.report import run_report, run_report_check

    if check_only:
        run_report_check(sample=sample, root=root)
        return

    if sample is None:
        raise click.UsageError("Which sample? `nrw report <sample>`.")

    run_report(
        sample=sample,
        topic=topic,
        tier=tier,
        concept=concept,
        root=root,
        force=force,
        stdout=stdout,
        list_concepts=list_concepts,
    )


@main.command("report-figure")
@click.argument("script", type=click.Path(exists=True, dir_okay=False))
@click.option("--root", default=None, help="Project root. Discovered if omitted.")
@click.option(
    "--stdout-to",
    default=None,
    help="Write the script's stdout to this file, beside it. For a markdown table.",
)
def report_figure_command(script: str, root: str | None, stdout_to: str | None) -> None:
    """Run a report's figure script and record what it read and wrote.

    A plot built from several fits belongs to all of them. Without a record,
    `nrw whence` on a published figure says "unknown" -- which is the one
    question this project exists to answer.
    """
    from nr_workbench.commands.report import run_report_figure

    run_report_figure(script=script, root=root, stdout_to=stdout_to)


@main.command("supersede")
@click.argument("sample")
@click.argument("old_stem")
@click.option("--by", "new_stem", required=True, help="The report that replaces it.")
@click.option("--reason", required=True, help="Why it was superseded.")
@click.option("--root", default=None, help="Project root. Discovered if omitted.")
def supersede_command(
    sample: str, old_stem: str, new_stem: str, reason: str, root: str | None
) -> None:
    """Mark a report as replaced, without deleting it.

    The reasoning in a superseded report is usually worth more than its
    conclusion -- the reference project's most useful artefact is a claim its
    own author retracted, with both versions left in place.
    """
    from nr_workbench.commands.report import run_supersede

    run_supersede(
        sample=sample,
        old_stem=old_stem,
        new_stem=new_stem,
        reason=reason,
        root=root,
    )


@main.command("pack")
@click.argument("fit_id")
@click.option(
    "--out",
    "-o",
    default=None,
    help="Where to write it [default: ./<fit_id>.zip].",
)
@click.option(
    "--dir",
    "as_dir",
    is_flag=True,
    help="Leave a directory instead of archiving it.",
)
@click.option(
    "--with-chain",
    is_flag=True,
    help="Include the MCMC chain, usually the largest file in the result.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Pack even though an input no longer matches its recorded hash.",
)
def pack_command(
    fit_id: str, out: str | None, as_dir: bool, with_chain: bool, force: bool
) -> None:
    """Package FIT_ID so a collaborator can reproduce it with only refl1d.

    A result directory records the hashes of its data, not the data. A bundle
    carries the measurements themselves, at the paths the frozen script
    expects, plus a `verify.py` that recomputes chi-squared and checks it
    against the recorded value.
    """
    from nr_workbench.commands.pack import run_pack

    run_pack(
        fit_id=fit_id,
        out=out,
        archive=not as_dir,
        with_chain=with_chain,
        force=force,
    )


@main.command("diff")
@click.argument("fit_a")
@click.argument("fit_b")
@click.option(
    "--script", is_flag=True, help="Also print a unified diff of the scripts."
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def diff_command(**kwargs: object) -> None:
    """Compare two fits: what changed, and whether it explains the result.

    The verdict separates "the fit got better" from "the data changed
    underneath me" -- indistinguishable in a chi-squared column.
    """
    from nr_workbench.commands.provenance_cmd import run_diff

    run_diff(**kwargs)  # type: ignore[arg-type]


@main.command("check")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def check_command(as_json: bool) -> None:
    """Verify project integrity: stale results, missing inputs, broken pointers."""
    from nr_workbench.commands.provenance_cmd import run_check

    run_check(as_json=as_json)


@main.group("data")
def data_group() -> None:
    """Check reduced data before modelling it."""


@data_group.command("reconcile")
@click.argument("sample")
@click.option("--root", default=None, help="Project root [default: discovered].")
@click.option("--result-out", default=None, help="Write the full result here.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def data_reconcile_command(**kwargs: object) -> None:
    """Compare what SAMPLE's files record against what sample.md claims.

    Every reduced file carries the run title, the direct beam it was divided
    by and the angle. Nothing has ever compared those to the measurement
    table. Exits non-zero when they disagree.
    """
    from nr_workbench.commands.data import run_reconcile

    run_reconcile(**kwargs)  # type: ignore[arg-type]


@data_group.command("overlap")
@click.argument("sample")
@click.option("--run", type=int, help="Restrict to one run number.")
@click.option("--root", type=click.Path(file_okay=False), help="Project root.")
@click.option("--result-out", type=click.Path(), help="Write JSON here.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def data_overlap_command(**kwargs: object) -> None:
    """Check that a run's angle segments agree where they overlap in Q."""
    from nr_workbench.commands.data import run_overlap

    run_overlap(**kwargs)  # type: ignore[arg-type]


@data_group.command("features")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
@click.option("--result-out", type=click.Path(), help="Write JSON here.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def data_features_command(**kwargs: object) -> None:
    """Report critical edges, Kiessig fringes and thickness for one curve."""
    from nr_workbench.commands.data import run_features

    run_features(**kwargs)  # type: ignore[arg-type]


@data_group.command("check")
@click.argument("sample", required=False)
@click.option("--root", type=click.Path(file_okay=False), help="Project root.")
@click.option("--result-out", type=click.Path(), help="Write JSON here.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def data_check_command(**kwargs: object) -> None:
    """Validate every reduced steady-state file for a sample."""
    from nr_workbench.commands.data import run_check

    run_check(**kwargs)  # type: ignore[arg-type]


@main.group("experiment")
def experiment_group() -> None:
    """The experiment's runs, organized into samples (also: nrw serve).

    The data folder is watched for new runs; the catalog in experiment/ says
    which sample each belongs to and renders each managed sample's sample.md;
    `apply` copies the data and writes the files.
    """


@experiment_group.command("status")
@click.option("--root", type=click.Path(file_okay=False), help="Project root.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def experiment_status_command(**kwargs: object) -> None:
    """What the data source holds, what the catalog says, what is unassigned."""
    from nr_workbench.commands.experiment_cmd import run_status

    run_status(**kwargs)  # type: ignore[arg-type]


@experiment_group.command("settings")
@click.option("--ipts", default=None, help="The experiment's IPTS, e.g. IPTS-34347.")
@click.option("--label", default=None, help="The beamtime label.")
@click.option(
    "--location",
    default=None,
    metavar="PATH",
    help="The folder the reduced data is in; {ipts} is filled in.",
)
@click.option(
    "--default-location",
    is_flag=True,
    help="Follow nrw's default data location again (it is provisional).",
)
@click.option(
    "--settle",
    type=float,
    default=None,
    help="Seconds a run's files must be unchanged.",
)
@click.option(
    "--poll", type=float, default=None, help="Seconds between looks at the data folder."
)
@click.option("--check", is_flag=True, help="Say what the data folder holds.")
@click.option(
    "--confirm-ipts-change",
    is_flag=True,
    help="Change the IPTS even though the catalog already holds runs.",
)
@click.option(
    "--write", is_flag=True, help="Save the change; without it, only show it."
)
@click.option("--root", type=click.Path(file_okay=False), help="Project root.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def experiment_settings_command(**kwargs: object) -> None:
    """Show the IPTS, data folder and watcher; with options, change them.

    The same settings as the Settings page of `nrw serve`, saved into
    nrw.toml the same way: only nrw's own lines change. Shows the change
    unless --write is given.
    """
    from nr_workbench.commands.experiment_cmd import run_settings

    run_settings(**kwargs)  # type: ignore[arg-type]


@experiment_group.command("assign")
@click.argument("runs", nargs=-1, required=True)
@click.option("--sample", default=None, help="The sample these runs belong to.")
@click.option("--unassign", is_flag=True, help="Take the runs out of any sample.")
@click.option(
    "--type", "measurement", default=None, help="Measurement type, e.g. 'full Q'."
)
@click.option(
    "--condition", default=None, help="Condition, e.g. 'OCV' or '-0.5 mA/cm2'."
)
@click.option(
    "--note", default=None, help="A note for the page (not written to sample.md)."
)
@click.option("--exclude", is_flag=True, help="Record the runs as not used.")
@click.option("--include", is_flag=True, help="Record the runs as used again.")
@click.option("--root", type=click.Path(file_okay=False), help="Project root.")
def experiment_assign_command(**kwargs: object) -> None:
    """Record which sample RUNS belong to, and how they were measured."""
    from nr_workbench.commands.experiment_cmd import run_assign

    run_assign(**kwargs)  # type: ignore[arg-type]


@experiment_group.command("apply")
@click.argument("samples", nargs=-1)
@click.option(
    "--write", is_flag=True, help="Carry the plan out; without it, only show it."
)
@click.option(
    "--confirm",
    multiple=True,
    metavar="RUN",
    help="Copy this unconfirmed run anyway (it settled, but nothing shows it ended).",
)
@click.option("--root", type=click.Path(file_okay=False), help="Project root.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def experiment_apply_command(**kwargs: object) -> None:
    """Copy assigned data into samples/ and render each managed sample.md.

    Shows what it would do unless --write is given. Nothing is overwritten:
    a re-reduced source, an edited copy or a hand-edited sample.md is
    reported, and exits non-zero, rather than replaced.
    """
    from nr_workbench.commands.experiment_cmd import run_apply

    run_apply(**kwargs)  # type: ignore[arg-type]


@experiment_group.command("adopt")
@click.argument("samples", nargs=-1)
@click.option(
    "--write", is_flag=True, help="Record in the catalog; without it, only show."
)
@click.option(
    "--rewrite",
    is_flag=True,
    help="Also replace sample.md with the catalog's rendering (backed up first).",
)
@click.option("--root", type=click.Path(file_okay=False), help="Project root.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def experiment_adopt_command(**kwargs: object) -> None:
    """Bring hand-written samples into the catalog, or pull hand edits back."""
    from nr_workbench.commands.experiment_cmd import run_adopt

    run_adopt(**kwargs)  # type: ignore[arg-type]


@experiment_group.command("release")
@click.argument("sample")
@click.option("--root", type=click.Path(file_okay=False), help="Project root.")
def experiment_release_command(**kwargs: object) -> None:
    """Make SAMPLE's sample.md yours again: no nrw command will rewrite it."""
    from nr_workbench.commands.experiment_cmd import run_release

    run_release(**kwargs)  # type: ignore[arg-type]


@main.command("import")
@click.argument("source", type=click.Path(exists=True, file_okay=False))
@click.option("--root", type=click.Path(file_okay=False), help="Project root.")
@click.option(
    "--sample",
    default="Sample1",
    show_default=True,
    help="Sample for files that name none of their own.",
)
@click.option("--write", is_flag=True, help="Create the links; off by default.")
@click.option("--verbose", is_flag=True, help="List every planned file.")
@click.option("--result-out", type=click.Path(), help="Write the plan here as JSON.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def import_command(**kwargs: object) -> None:
    """Import an existing beamtime directory, symlinking its data."""
    from nr_workbench.commands.import_cmd import run_import

    run_import(**kwargs)  # type: ignore[arg-type]


@main.command("serve")
@click.option("--root", type=click.Path(file_okay=False), help="Project root.")
@click.option("--host", default="127.0.0.1", show_default=True, help="Interface.")
@click.option("--port", default=8765, show_default=True, type=int, help="Port.")
@click.option(
    "--debug",
    is_flag=True,
    help="Enable the Flask reloader and its debugger.",
)
def serve_command(**kwargs: object) -> None:
    """Browse the project, and organize the experiment's runs into samples.

    Pages only read, except where they change the project -- the Experiment
    and Settings pages, and curating and publishing fits -- and those are
    offered when the server is bound to loopback, as it is without --host.
    """
    from nr_workbench.commands.serve import run_serve

    run_serve(**kwargs)  # type: ignore[arg-type]


@main.group("skills")
def skills_group() -> None:
    """Manage the project's skills/ directory."""


@skills_group.command("list")
@click.option(
    "--bundled", is_flag=True, help="List the skills shipped with nr-workbench instead."
)
def skills_list_command(bundled: bool) -> None:
    """List skills installed in this project."""
    from nr_workbench.commands.skills import run_skills_list

    run_skills_list(bundled=bundled)


@skills_group.command("add")
@click.argument("names", nargs=-1, required=True)
@click.option("--force", is_flag=True, help="Overwrite locally edited files.")
def skills_add_command(names: tuple[str, ...], force: bool) -> None:
    """Install the named bundled skills into this project.

    `nrw init` seeds the skills that apply to any sample and leaves out the
    material-specific ones, because each costs attention on every query that is
    not about it. Add the ones this sample needs -- `metal-oxide-interfaces` for
    an electrode, `polymer-films` for a brush, `solvent-contrast-matching` for a
    contrast series. `nrw skills list --bundled` says what each one is for.
    """
    from nr_workbench.commands.skills import run_skills_add

    run_skills_add(names=names, force=force)


@skills_group.command("sync")
@click.option("--force", is_flag=True, help="Overwrite locally edited skills.")
def skills_sync_command(force: bool) -> None:
    """Install every bundled skill, leaving locally edited ones alone.

    This adds the skills a project does not have as well as refreshing the ones
    it does. `nrw skills add <name>` is the targeted form.
    """
    from nr_workbench.commands.skills import run_skills_sync

    run_skills_sync(force=force)


@skills_group.command("path")
def skills_path_command() -> None:
    """Print the directory of the skills bundled with nr-workbench."""
    from nr_workbench.commands.skills import run_skills_path

    run_skills_path()


if __name__ == "__main__":  # pragma: no cover
    main()
