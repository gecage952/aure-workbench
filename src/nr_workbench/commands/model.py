"""``nrw model`` -- validate, preview, and generate fit scripts from a spec."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Any

import click

from nr_workbench.instrument.reduced import find_segments
from nr_workbench.problems import one_line
from nr_workbench.project.layout import ProjectLayout, ProjectNotFoundError


def _layout(start: Path | None = None) -> ProjectLayout:
    """Discover the project, or fail with guidance."""
    try:
        return ProjectLayout.discover(start)
    except ProjectNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc


def _load(spec_path: str):
    """Load a spec and its project, reporting problems clearly."""
    from nr_workbench.spec.models import SpecError, load_spec

    path = Path(spec_path).resolve()
    layout = _layout(path.parent)
    try:
        return layout, path, load_spec(path)
    except SpecError as exc:
        raise click.ClickException(str(exc)) from exc


def _spec_sha256(path: Path) -> str:
    """Digest of a spec file, as recorded in a generated script's header.

    Taken over the spec with any deprecation banner removed, so that labelling a
    spec abandoned never makes its generated script report as stale. See
    :mod:`nr_workbench.spec.deprecation`.
    """
    from nr_workbench.spec.deprecation import identity_hash

    return identity_hash(path)


def run_validate(
    *, spec: str, as_json: bool = False, result_out: str | None = None
) -> None:
    """Check a spec against the project on disk.

    Args:
        spec: Path to the spec YAML.
        as_json: Emit machine-readable JSON.
        result_out: Write an ``ndip-tool-result/1`` manifest here.

    Raises:
        SystemExit: With code 1 when the spec has errors.
    """
    from nr_workbench.spec.deprecation import is_deprecated, reason_of
    from nr_workbench.spec.validate import validate_spec

    layout, path, model = _load(spec)
    report = validate_spec(model, layout.root)

    # Said before anything else, and not as an error: the spec may well be
    # perfectly valid. What matters is that someone already decided against it.
    text = path.read_text(encoding="utf-8")
    if is_deprecated(text) and not as_json:
        because = reason_of(text)
        click.secho(
            f"  DEPRECATED  {path.name}" + (f" -- {because}" if because else ""),
            fg="yellow",
        )

    if as_json:
        click.echo(
            json.dumps(
                {"spec": str(path), "ok": report.ok, **report.as_dict()}, indent=2
            )
        )
    else:
        click.echo(f"{path.relative_to(layout.root)}  [{model.name}]")
        for line in report.info:
            click.echo(f"  info     {line}")
        for line in report.warnings:
            click.echo(f"  warning  {line}")
        for line in report.errors:
            click.echo(f"  ERROR    {line}")
        click.echo("  ok" if report.ok else f"\n  {len(report.errors)} error(s)")

    if result_out:
        from nr_workbench.provenance.result_manifest import write_manifest

        write_manifest(
            result_out,
            "nrw-model-validate",
            "ok" if report.ok else "failed",
            params={"spec": str(path)},
            info={"errors": len(report.errors), "warnings": len(report.warnings)},
            exit_code=0 if report.ok else 1,
        )

    if not report.ok:
        raise SystemExit(1)


def run_preview(*, spec: str, as_json: bool = False, build: bool = False) -> None:
    """Show the parameter table a spec resolves to, without writing anything.

    The real gate before a long fit: it says how many parameters will vary and
    how they are grouped, so a mistake costs a second rather than an hour of
    DREAM.

    Args:
        spec: Path to the spec YAML.
        as_json: Emit machine-readable JSON.
        build: Also construct the refl1d problem and report the initial chisq.

    Raises:
        click.ClickException: If the spec cannot be resolved.
    """
    from nr_workbench.spec.models import SpecError
    from nr_workbench.spec.resolve import build_table, discover_measurements

    layout, path, model = _load(spec)
    try:
        table = build_table(model, discover_measurements(model, layout.root))
    except SpecError as exc:
        raise click.ClickException(str(exc)) from exc

    payload: dict[str, Any] = {
        "spec": str(path),
        "model": model.name,
        "experiments": table.n_experiments,
        "free_parameters": table.n_free,
        "constrained_values": len(table.expressions),
        "groups": {
            group: [
                {"index": m.index, "file": m.file, "theta": m.theta, "time": m.time}
                for m in measurements
            ]
            for group, measurements in table.measurements.items()
        },
        "parameters": [
            {
                "key": p.key,
                "name": p.display,
                "value": p.value,
                "bounds": list(p.bounds) if p.bounds else None,
                "fixed": p.fixed,
            }
            for p in table.free
        ],
        "warnings": table.warnings,
    }

    if build:
        payload.update(_build_problem(table, layout.root))

    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return

    click.echo(f"{model.name}  ({path.name})")
    click.echo(
        f"  {table.n_experiments} experiment(s) in {len(table.measurements)} group(s)"
    )
    for group, measurements in table.measurements.items():
        times = [m.time for m in measurements if m.time is not None]
        span = f", t = {min(times):g}..{max(times):g} s" if times else ""
        click.echo(f"    {group:<12} {len(measurements):>3} measurement(s){span}")

    click.echo(
        f"\n  {table.n_free} free parameter(s), {len(table.expressions)} constrained"
    )
    width = max((len(p.key) for p in table.free), default=4)
    for parameter in table.free:
        if parameter.fixed:
            click.echo(
                f"    {parameter.key:<{width}}  {parameter.value:>10.4g}   fixed"
            )
        else:
            low, high = parameter.bounds or (float("nan"), float("nan"))
            click.echo(
                f"    {parameter.key:<{width}}  {parameter.value:>10.4g}   [{low:g}, {high:g}]"
            )

    for warning in table.warnings:
        click.echo(f"\n  warning  {warning}")

    if build:
        click.echo()
        if "error" in payload:
            click.echo(f"  build FAILED: {payload['error']}")
        else:
            click.echo(
                f"  built ok: chisq {payload['chisq']:.6g}, "
                f"{payload['n_points']} data point(s)"
            )


def _build_problem(table, root: Path) -> dict[str, Any]:
    """Generate, execute, and report on the problem, without keeping a script.

    Written inside the project's own cache rather than a temp directory. The
    generated script locates the project by walking up to ``nrw.toml``, so it
    has to sit somewhere that walk succeeds -- and staging a parallel tree of
    symlinks to fake that is fragile in exactly the way this check exists to
    catch.
    """
    from nr_workbench.codegen.generator import generate

    scratch = root / ".nrw" / "cache" / "preview"
    scratch.mkdir(parents=True, exist_ok=True)
    script = scratch / "preview.py"
    script.write_text(generate(table), encoding="utf-8")

    try:
        from nr_workbench.fitting.runner import FitError, load_problem

        try:
            loaded = load_problem(script, root=root)
        except FitError as exc:
            return {"error": str(exc)}

        problem = loaded.problem
        try:
            return {
                "chisq": float(problem.chisq()),
                "n_free_actual": len(problem.getp()),
                "n_points": int(sum(len(e.probe.Q) for e in problem.models)),
            }
        except Exception as exc:  # pragma: no cover - bumps API drift
            return {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        script.unlink(missing_ok=True)


def run_generate(*, spec: str, out: str | None = None, force: bool = False) -> None:
    """Write the refl1d script for a spec.

    Args:
        spec: Path to the spec YAML.
        out: Output path. Defaults to the spec's path with a ``.py`` suffix.
        force: Overwrite a script that has been hand-edited.

    Raises:
        click.ClickException: If the spec is invalid or the target was edited.
    """
    from nr_workbench.codegen.generator import generate, generated_at, verify_self_hash
    from nr_workbench.provenance.env import package_version
    from nr_workbench.spec.deprecation import is_deprecated, reason_of
    from nr_workbench.spec.models import SpecError
    from nr_workbench.spec.resolve import build_table, discover_measurements
    from nr_workbench.spec.validate import validate_spec

    layout, path, model = _load(spec)

    # Before validation: a deprecated spec is not a spec with a problem, it is a
    # spec someone has already decided against, and reporting its parameter
    # bounds would invite a fix rather than a stop.
    text = path.read_text(encoding="utf-8")
    if is_deprecated(text):
        because = reason_of(text)
        raise click.ClickException(
            f"{path.name} is deprecated"
            + (f": {because}" if because else ".")
            + "\nIt is kept because the fits it produced are part of the record, "
            "not because it is a model to run. Copy it to a new name and fix "
            "what was wrong, or `nrw model deprecate "
            f"{Path(spec).as_posix()} --undo` if it should not have been marked."
        )

    report = validate_spec(model, layout.root)
    if not report.ok:
        for line in report.errors:
            click.echo(f"  ERROR  {line}", err=True)
        raise click.ClickException("spec is not valid; nothing generated")

    try:
        table = build_table(model, discover_measurements(model, layout.root))
    except SpecError as exc:
        raise click.ClickException(str(exc)) from exc

    target = Path(out) if out else path.with_suffix(".py")
    existing = target.read_text(encoding="utf-8") if target.exists() else None
    if (
        existing is not None
        and not force
        and existing.startswith("# ---")
        and not verify_self_hash(existing)
    ):
        raise click.ClickException(
            f"{target.name} has been edited by hand since it was generated.\n"
            "Overwriting would discard those edits. Either re-apply them to the "
            "spec, or run `nrw model fork` to take ownership of the script with "
            "its provenance intact. Use --force to overwrite anyway."
        )

    versions = {
        name: version
        for name in ("refl1d", "bumps", "numpy", "nr-workbench")
        if (version := package_version(name))
    }

    def render(now: Any = None) -> str:
        return generate(
            table,
            spec_path=path.relative_to(layout.root),
            spec_sha256=_spec_sha256(path),
            versions=versions,
            now=now,
        )

    # A script that would come out the same but for its timestamp is left as it
    # is. Rewriting it would change its hash, and so the identity of every fit
    # of it: an unchanged model, fitted again with unchanged settings, would
    # read as a new run instead of being refused as the identical one it is.
    previous = (
        generated_at(existing) if existing and verify_self_hash(existing) else None
    )
    unchanged = previous is not None and render(previous) == existing
    if not unchanged:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render(), encoding="utf-8")

    # The explanation is derived from the same table, so it cannot describe a
    # different model than the one just written.
    from nr_workbench.spec.explain import explain

    notes_target = target.with_suffix(".md")
    notes_target.write_text(
        explain(
            table,
            spec_path=path.relative_to(layout.root),
            spec_sha256=_spec_sha256(path),
        ),
        encoding="utf-8",
    )

    if unchanged:
        click.echo(
            f"{target.relative_to(layout.root)} is up to date with its spec; "
            "left as it is"
        )
    else:
        click.echo(f"Wrote {target.relative_to(layout.root)}")
    click.echo(f"      {notes_target.relative_to(layout.root)}  (what it assumes)")

    click.echo(
        f"  {table.n_experiments} experiment(s), {table.n_free} free parameter(s), "
        f"{len(table.expressions)} constrained"
    )
    for warning in report.warnings:
        click.echo(f"  warning  {warning}")
    click.echo(f"\n  nrw fit run {target.relative_to(layout.root)}")


def run_schema(*, out: str | None = None) -> None:
    """Emit the JSON Schema for ``nrw-model/1``.

    Args:
        out: Where to write it. Defaults to the project's
            ``.nrw/schema/nrw-model-1.json``; ``-`` prints to stdout.
    """
    from nr_workbench.spec.schema import build_schema, write_schema

    if out == "-":
        click.echo(json.dumps(build_schema(), indent=2))
        return

    if out is None:
        layout = _layout()
        target = layout.schema_dir / "nrw-model-1.json"
    else:
        target = Path(out)

    write_schema(target)
    click.echo(f"Wrote {target}")


def run_forms() -> None:
    """List the available constraint forms."""
    from nr_workbench.spec.constraints import ENDPOINT_NOTE, describe_forms

    rows = describe_forms()
    width = max(len(name) for name, _ in rows)
    for name, summary in rows:
        click.echo(f"  {name:<{width}}  {summary}")
    click.echo(ENDPOINT_NOTE)


def run_new(
    *,
    sample: str,
    name: str,
    out: str | None = None,
    force: bool = False,
    from_notes: bool = False,
    print_prompt: bool = False,
) -> None:
    """Scaffold a spec from what `nrw sample scan` found on disk.

    Produces something that validates and generates immediately, with the
    stack left as a placeholder for the scientist to correct. Starting from a
    working file beats starting from a blank one -- the schema is easier to
    learn by editing than by reading.

    Args:
        sample: The sample to build a spec for.
        name: Model name; also the filename.
        out: Explicit output path.
        force: Overwrite an existing spec.
        from_notes: Ask a configured language model to propose the stack from
            ``sample.md``. Falls back to the placeholder, with an explanation,
            when no endpoint is configured.
        print_prompt: Print the instruction to hand a coding assistant instead
            of calling an endpoint.

    Raises:
        click.ClickException: If the sample has no usable data, or the target
            exists and ``force`` was not given.
    """

    from nr_workbench.project.samples import validate_model_name
    from nr_workbench.project.scan import load_register, register_drift, scan_sample

    # The name becomes a filename three times over; a path here would write
    # the spec outside models/ -- the rule `nrw aure` and the page apply.
    try:
        validate_model_name(name)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    layout = _layout()

    # The register wins over the disk. `sample.yaml` is what this sample's
    # analysis is about, and editing it is the supported way to co-refine a
    # subset -- a beamtime directory routinely holds alignment scans, aborted
    # runs and other conditions that belong to the sample but not to a model.
    found = load_register(layout.root, sample)
    from_register = found is not None
    if found is None:
        try:
            found = scan_sample(layout.root, sample)
        except FileNotFoundError as exc:
            raise click.ClickException(str(exc)) from exc

    if not found.steady and not found.series:
        raise click.ClickException(
            f"No data found for {sample!r}. Copy reduced files into "
            f"samples/{sample}/data/steady and data/tnr, then run `nrw sample scan`."
        )

    if from_register:
        unregistered, missing = register_drift(layout.root, sample)
        if unregistered:
            click.echo(
                f"  note  using samples/{sample}/sample.yaml, which does not list "
                f"run(s) {', '.join(str(r) for r in unregistered)} that are on "
                "disk.\n"
                "        If that is deliberate, nothing to do. If the register is "
                f"stale, run `nrw sample scan {sample}`."
            )
        if missing:
            click.echo(
                f"  note  sample.yaml lists run(s) "
                f"{', '.join(str(r) for r in missing)} with no data on disk; "
                "they are skipped."
            )

    target = Path(out) if out else layout.sample(sample) / "models" / f"{name}.yaml"
    if target.exists() and not force:
        raise click.ClickException(
            f"{target} already exists. Use --force to overwrite."
        )

    document = _scaffold_document(sample, name, found, layout.root)
    blank = document.pop("_nrw_blank_angles", [])
    summed = document.pop("_nrw_summed_series", [])

    notes_path = layout.sample(sample) / "sample.md"
    notes = notes_path.read_text(encoding="utf-8") if notes_path.is_file() else ""
    provenance = ""
    if from_notes:
        from nr_workbench.agent.guard import agent_is_driving

        if agent_is_driving():
            # --from-notes asks a configured endpoint to propose the stack.
            # Under a harness that is a model asking a weaker model to do the
            # part it is best at, so the scaffold is written plain and the
            # instruction is printed instead --- which is what --print-prompt
            # already does for a person working in an editor.
            print_prompt = True
        else:
            document, provenance = _author_from_notes(
                layout=layout, document=document, notes=notes
            )
    target.parent.mkdir(parents=True, exist_ok=True)
    stack_note = (
        "# The stack below is a PLACEHOLDER -- replace it with the real layers\n"
        "# and starting values, then:\n"
        if not provenance
        else "# Check the proposed stack, then:\n"
    )
    header = (
        f"# yaml-language-server: $schema="
        f"{_schema_relative(layout, target)}\n"
        "#\n"
        "# Scaffolded by `nrw model new` from the data on disk.\n"
        f"{stack_note}"
        "#\n"
        "#   nrw model validate <this file>\n"
        "#   nrw model preview  <this file>\n"
        "#   nrw model generate <this file>\n"
    )
    target.write_text(
        header + provenance + blank_angles_comment(blank) + _emit_spec(document),
        encoding="utf-8",
    )

    click.echo(f"Wrote {target.relative_to(layout.root)}")
    if print_prompt:
        click.echo()
        _print_agent_instructions(layout, sample, notes, target)
        return
    for run in summed:
        click.echo(
            f"  note  run {run} is in data/steady as a summed dataset and also\n"
            "        as a time-resolved series. Only the series is in the spec --\n"
            "        fitting both would count the same neutrons twice. The summed\n"
            "        file is where the series' incident angle is read from."
        )
    warn_blank_angles(blank)

    click.echo(
        f"  {len(document.get('states', []))} state(s), "
        f"{len(document.get('series', []))} series"
    )
    click.echo(
        "\n  Check the proposed stack, then:"
        if provenance
        else "\n  The stack is a placeholder. Edit it, then:"
    )
    click.echo(f"    nrw model validate {target.relative_to(layout.root)}")


def _thetas_from_headers(paths: list[Path]) -> tuple[list[float | None], list[str]]:
    """Read each segment's incident angle from its own file.

    The angle is recorded exactly, in radians, in the header REF_L writes at
    the top of a reduced file. A file that records none gets no angle at all:
    it is left blank, never filled from the group's usual settings. Those were
    once assumed -- ``[0.45, 1.2, 3.5]`` truncated to the segment count --
    which is silently wrong for anything else. theta sets the wavelength axis
    and the resolution, and a wrong one is absorbed into roughness rather than
    raising.

    Args:
        paths: Segment files, in order.

    Returns:
        ``(thetas, blank)`` -- the angle of each file in degrees, ``None``
        where it records none, and the names of those files.
    """
    from nr_workbench.instrument.header import recorded_angle

    thetas: list[float | None] = []
    blank: list[str] = []
    for path in paths:
        # The same reader resolving the spec uses, so the two cannot disagree
        # about whether a file records its angle.
        angle = recorded_angle(path).theta
        if angle is None:
            blank.append(path.name)
            thetas.append(None)
        else:
            thetas.append(round(angle, 4))
    return thetas, blank


def _dq_is_fwhm_from_headers(paths: list[Path]) -> tuple[bool, list[str]]:
    """Read the ``dQ`` width convention off the files, rather than assuming it.

    The 4th column is FWHM in every reduction written so far, but that is a
    property of the reduction and it is expected to change to sigma. The two
    differ by 2.355, and a resolution wrong by that factor does not raise -- the
    fit absorbs it into roughness and reports a confident wrong interface width.
    So the convention is read from each file's column-title line at intake and
    written into the spec as a fact, the same way theta is.

    Args:
        paths: The steady-state files this spec will fit.

    Returns:
        ``(dq_is_fwhm, notes)`` -- the convention to write into the spec, and
        human-readable notes for anything the caller should be told: files that
        did not state a convention, or a set that disagrees with itself.

    Raises:
        click.ClickException: If the files disagree. ``probe.dq_is_fwhm`` is one
            boolean for the whole spec, so a spec mixing conventions would be
            wrong for half its data with nothing to show it. Splitting the fit
            is the only correct answer and it has to be the scientist's.
    """
    from nr_workbench.instrument.header import read_header

    seen: dict[str, list[str]] = {}
    silent: list[str] = []
    for path in paths:
        try:
            convention = read_header(path).dq_convention
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            silent.append(f"{path.name} ({exc})")
            continue
        if convention is None:
            silent.append(path.name)
        else:
            seen.setdefault(convention, []).append(path.name)

    notes: list[str] = []
    if len(seen) > 1:
        detail = "; ".join(
            f"{convention}: {', '.join(names)}"
            for convention, names in sorted(seen.items())
        )
        raise click.ClickException(
            "These runs do not share a dQ convention, so one spec cannot fit "
            f"them: {detail}. probe.dq_is_fwhm applies to the whole spec, so a "
            "mixed set would scale half the data's resolution by 2.355 with "
            "nothing in the record to show it. Fit each convention as its own "
            "spec, or re-reduce so they agree."
        )

    if silent:
        notes.append(
            f"{len(silent)} file(s) do not state whether dQ is FWHM or sigma "
            f"({', '.join(silent[:3])}"
            f"{', ...' if len(silent) > 3 else ''}). "
            "Assuming FWHM, which is what every reduction has written so far -- "
            "confirm it, because sigma is 2.355x different and would be absorbed "
            "into roughness rather than reported."
        )

    if not seen:
        return True, notes
    convention = next(iter(seen))
    notes.append(f"dQ read from the file headers as {convention.upper()}.")
    return convention == "fwhm", notes


def _series_angle_is_recorded(root: Path, found_series) -> bool:
    """Whether a time-resolved series' angle is on disk, for the spec to read.

    The slices carry no header, but the same run is also reduced as a summed
    dataset into ``data/steady``, and that file does -- which is where
    resolving the spec reads it from (``spec.resolve.summed_dataset_dir``).
    """
    from nr_workbench.instrument.header import theta_for_run
    from nr_workbench.spec.resolve import summed_dataset_dir

    steady = summed_dataset_dir(found_series.directory)
    if found_series.run is None or steady is None:
        return False
    theta, _ = theta_for_run(root / steady, found_series.run)
    return theta is not None


def blank_angles_comment(blank: list[str]) -> str:
    """The spec's comment naming each file whose angle it leaves blank.

    Shared by `nrw model new` and `nrw aure import`: both leave a ``null``
    where no header records the angle, and the comment says whose it is.
    """
    if not blank:
        return ""
    # One line each, whatever the name: a line break in a directory name would
    # end the comment and make the rest of the name keys of the spec.
    return (
        "# BLANK ANGLES. No header records the incident angle of:\n"
        + "".join(f"#   {one_line(name)}\n" for name in blank)
        + "# Each is `null` below. Give it in degrees; `nrw model validate`\n"
        "# refuses this spec until then.\n"
        "#\n"
    )


def warn_blank_angles(blank: list[str]) -> None:
    """Report the files whose incident angle the spec leaves blank.

    Shared by `nrw model new` and `nrw aure import`. A bare filename in yellow
    would not say that a resolution-setting number is missing, which is the one
    thing this is for.

    Args:
        blank: Names of files with no angle in their header.
    """
    if not blank:
        return
    click.secho(
        "  !  no incident angle recorded for: "
        + ", ".join(one_line(name) for name in blank[:6])
        + ("" if len(blank) <= 6 else f" (+{len(blank) - 6} more)"),
        fg="yellow",
    )
    click.secho(
        "     The spec leaves each blank: give it, in degrees, before fitting.\n"
        "     `nrw model validate` refuses the spec until then. Theta sets each\n"
        "     probe's wavelength axis, and a guessed one would be absorbed into\n"
        "     roughness rather than reported.",
        fg="yellow",
    )


def state_for_run(
    root: Path, measurement: Any
) -> tuple[dict[str, Any], list[Path], list[str]]:
    """Build one spec ``states`` entry from a steady run on disk.

    Shared by ``nrw model new`` and ``nrw aure import`` -- both need the same
    thing (which files, and where each one's incident angle comes from), so a
    second implementation would be a second chance to get it wrong. The block
    has no ``thetas`` when every file records its angle: resolving the spec
    reads each from its header. Where one records none, ``thetas`` lists the
    recorded angles with a ``null`` in its place, for a person to fill in.

    Args:
        root: The project root, which the recorded paths are relative to.
        measurement: A :class:`~nr_workbench.project.scan.SteadyMeasurement`.

    Returns:
        ``(state, files, blank)`` -- the spec block, the data files it names,
        and any file whose angle it leaves blank.

    Raises:
        ValueError: If the run has no files on disk.
    """
    run = measurement.run
    if measurement.partials:
        numbers = sorted(measurement.partials)
        paths = [root / measurement.partials[k] for k in numbers]
        thetas, blank = _thetas_from_headers(paths)
        directory = Path(measurement.partials[numbers[0]]).parent
        block: dict[str, Any] = {"name": f"run{run}", "run": run}
        if _is_every_segment_on_disk(root, directory, measurement):
            block["segments"] = "auto"
            if blank:
                block["thetas"] = thetas
            block["data_dir"] = str(directory)
        else:
            # The register lists some of the run's segments, not all: `auto`
            # would read the rest too. Listed, each still takes its angle from
            # its file -- or is a blank for a person, where none is recorded.
            block["segments"] = [
                {"file": measurement.partials[k]}
                | ({"theta": None} if theta is None else {})
                for k, theta in zip(numbers, thetas, strict=True)
            ]
        return block, paths, blank

    if measurement.combined:
        path = root / measurement.combined
        thetas, blank = _thetas_from_headers([path])
        block = {
            "name": f"run{run}",
            "run": run,
            "kind": "combined",
            "segments": "auto",
        }
        if blank:
            block["thetas"] = thetas
        block["data_dir"] = str(Path(measurement.combined).parent)
        return block, [path], blank

    raise ValueError(f"run {run} has no reduced files on disk")


def _is_every_segment_on_disk(root: Path, directory: Path, measurement) -> bool:
    """Whether a registered run's segments are exactly what ``segments: auto`` reads.

    ``sample.yaml`` is where a person says "co-refine only these", so a run it
    lists with a segment left out must not be written as ``auto``, which would
    read every segment in the folder.
    """
    from nr_workbench.spec.resolve import segments_on_disk

    registered = {k: Path(v) for k, v in measurement.partials.items()}
    if any(path.parent != directory for path in registered.values()):
        return False
    on_disk = segments_on_disk(root / directory, measurement.run)
    return {k: [p.name for p in paths] for k, paths in on_disk.items()} == {
        k: [path.name] for k, path in registered.items()
    }


def _scaffold_document(
    sample: str, name: str, found, root: Path | None = None
) -> dict[str, Any]:
    """Build the scaffolded spec mapping.

    Args:
        sample: Sample identifier.
        name: Model name.
        found: The scan result for this sample.
        root: Project root, needed to read the data-file headers.
    """
    from nr_workbench.spec.authoring import placeholder_materials, placeholder_stack

    root = Path(root) if root is not None else Path.cwd()
    states = []
    blank_angles: list[str] = []
    steady_files: list[Path] = []

    # A time-resolved run is *also* reduced as a summed dataset into
    # data/steady, under the same run number. That file is the sum of the very
    # slices the series contributes, so including both would put the same
    # neutrons into the fit twice -- once whole, once in pieces -- and weight
    # that run roughly double. The series wins; the summed file stays on disk
    # and is where the series' incident angle is read from.
    series_runs = {s.run for s in found.series if s.run is not None}
    summed_series: list[int] = []

    for run in sorted(found.steady):
        if run in series_runs:
            summed_series.append(run)
            continue
        entry = found.steady[run]
        try:
            block, paths, missing = state_for_run(root, entry)
        except ValueError:
            continue
        states.append(block)
        steady_files.extend(paths)
        blank_angles.extend(missing)

    series = []
    for found_series in found.series:
        block: dict[str, Any] = {
            # A series with no resolvable run number is just "tnr"; the inner
            # fallback already covers that, so there is no outer default.
            "name": f"tnr{found_series.run or ''}",
            "run": found_series.run,
            "reduced_dir": found_series.directory,
        }
        if not _series_angle_is_recorded(root, found_series):
            # Nothing on disk records it: a blank for a person, not a guess.
            block["theta"] = None
            blank_angles.append(
                f"{Path(found_series.directory).name} (series; no summed dataset "
                "of its run in data/steady)"
            )
        block["time_from"] = (
            "filename" if found_series.kind == "time_binned" else "reduction_json"
        )
        if found_series.t_step is not None:
            block["select"] = {
                "t_start": found_series.t_start,
                "t_stop": found_series.t_stop,
                "t_step": found_series.t_step,
            }
        series.append(block)

    # A measured property of the reduction, read off the files like theta -- not
    # a default. See `_dq_is_fwhm_from_headers`.
    dq_is_fwhm, dq_notes = _dq_is_fwhm_from_headers(steady_files)
    for note in dq_notes:
        click.secho(f"  {note}", fg="yellow" if not dq_is_fwhm else None, err=True)

    document: dict[str, Any] = {
        "schema": "nrw-model/1",
        "name": name,
        "sample": sample,
        "description": f"TODO: describe {sample}.\n",
        # A minimal physically-sensible stack, deliberately obvious as a stub.
        "materials": placeholder_materials(),
        "stack": placeholder_stack(),
        "probe": {"resolution": "angular_only", "dq_is_fwhm": dq_is_fwhm},
    }
    if states:
        document["states"] = states
    if series:
        document["series"] = series

    # A path the series takes from a constraint must NOT also be declared free
    # there, or resolution rejects it as a double assignment. So structural
    # parameters are scoped to the states, and the constraint owns the series.
    # The scaffold has to validate and generate as written -- one that fails on
    # first contact teaches the pattern backwards.
    state_names = [s["name"] for s in states]
    constrained = bool(series and len(states) >= 2)

    thickness: dict[str, Any] = {
        "path": "Film.thickness",
        "range": [50, 200],
        "per": "state",
    }
    if constrained:
        thickness["in"] = state_names

    document["parameters"] = [
        thickness,
        {"path": "Film.rho", "range": [2, 6], "per": "model"},
        {"path": "probe.intensity", "value": 1.0, "pm": 0.1, "per": "state"},
    ]

    document["_nrw_blank_angles"] = blank_angles
    document["_nrw_summed_series"] = summed_series

    if constrained:
        document["constraints"] = [
            {
                "series": series[0]["name"],
                "form": "linear_in_time",
                "from": state_names[0],
                "to": state_names[-1],
                "paths": ["Film.thickness"],
            }
        ]
    elif series:
        # One state cannot anchor an interpolation, so give the series its own
        # value rather than emitting a constraint that would be rejected.
        document["parameters"].append(
            {
                "path": "Film.thickness",
                "range": [50, 200],
                "per": "state",
                "in": [s["name"] for s in series],
            }
        )
    return document


def run_deprecate(*, spec: str, reason: str | None = None, undo: bool = False) -> None:
    """Mark a spec as abandoned, in the spec, where it cannot be missed.

    Specs are never deleted --- the fits they produced are part of the record and
    a result whose model is gone is not reproducible. The cost is that an
    abandoned spec is indistinguishable from a live one, and the evidence sits in
    a ``NOTES.md`` in another directory. This writes it at the top of the file.

    Hash-neutral by construction: the banner is fenced and every digest of a spec
    is taken with the fence removed, so nothing generated from this spec changes
    state. See :mod:`nr_workbench.spec.deprecation`.

    Args:
        spec: Path to the spec YAML.
        reason: Why it was abandoned. Required unless undoing.
        undo: Remove an existing banner instead.

    Raises:
        click.ClickException: On a missing spec, a missing reason, or a spec that
            is already in the requested state.
    """
    from nr_workbench.spec.deprecation import banner, is_deprecated, strip

    path = Path(spec).resolve()
    if not path.is_file():
        raise click.ClickException(f"No spec at {spec}")
    layout = _layout(path.parent)
    text = path.read_text(encoding="utf-8")
    relative = path.relative_to(layout.root).as_posix()

    if undo:
        if not is_deprecated(text):
            raise click.ClickException(f"{relative} is not deprecated.")
        path.write_text(strip(text), encoding="utf-8")
        click.echo(f"  {relative} is live again.")
        return

    if is_deprecated(text):
        raise click.ClickException(
            f"{relative} is already deprecated. `--undo` first to change the reason."
        )
    if not (reason or "").strip():
        raise click.ClickException(
            "--reason is required. Why a model was abandoned is the part nobody "
            "can reconstruct later, and it is worth more than the fit."
        )

    path.write_text(banner(str(reason)) + text, encoding="utf-8")
    click.echo(f"  {relative} marked deprecated.")
    click.echo("  Its fits keep their provenance; `nrw model generate` now refuses it.")


def _schema_relative(layout, target: Path) -> str:
    """Relative path from a spec to the project's JSON Schema."""
    import os

    schema = layout.schema_dir / "nrw-model-1.json"
    return os.path.relpath(schema, target.parent)


def run_fork(*, spec: str, name: str | None = None, out: str | None = None) -> None:
    """Turn a generated script into a hand-owned one, keeping provenance.

    The escape hatch has to live *inside* the provenance system. If taking
    manual control of a script also meant losing the record of what produced a
    result, people would do it anyway and the record would quietly become
    fiction. A fork is legal, cheap, and permanently labelled.

    Args:
        spec: The spec whose generated script should be forked.
        name: Name for the forked script. Defaults to ``<spec>-fork``.
        out: Explicit output path.

    Raises:
        click.ClickException: If the script has not been generated yet, or the
            target already exists.
    """
    from datetime import datetime

    layout, path, model = _load(spec)
    generated = path.with_suffix(".py")
    if not generated.is_file():
        raise click.ClickException(
            f"{generated.name} does not exist yet. Run `nrw model generate {spec}` first."
        )

    fork_name = name or f"{model.name}-fork"
    target = Path(out) if out else generated.with_name(f"{fork_name}.py")
    if target.exists():
        raise click.ClickException(f"{target} already exists. Choose another --name.")

    source = generated.read_text(encoding="utf-8")
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    banner = "\n".join(
        [
            "# " + "-" * 74,
            "# HAND-OWNED SCRIPT -- edit this freely.",
            f"#   forked from: {path.relative_to(layout.root).as_posix()}",
            f"#   spec sha256: {_spec_sha256(path)}  (at the time of the fork)",
            f"#   forked:      {stamp}",
            "#",
            "# `nrw model generate` will not touch this file, and `nrw check` will not",
            "# compare it against the spec. It is yours.",
            "#",
            "# It is still fully tracked: `nrw fit run` records its hash, its inputs and",
            "# the environment exactly as for a generated script, so results stay",
            "# traceable. That is the point of forking rather than editing in place.",
            "# " + "-" * 74,
            "",
        ]
    )

    # Drop the generated header: its self-hash no longer applies, and leaving a
    # stale DO-NOT-EDIT banner on a file the user is meant to edit is worse
    # than having none.
    body = source
    if body.startswith("# ---"):
        marker = "# " + "-" * 74
        end = body.find(marker, body.find(marker) + 1)
        if end != -1:
            body = body[end + len(marker) :].lstrip("\n")

    target.write_text(banner + body, encoding="utf-8")

    click.echo(f"Forked to {target.relative_to(layout.root)}")
    click.echo("  edit it freely; `nrw model generate` will leave it alone")
    click.echo(f"\n  nrw fit run {target.relative_to(layout.root)}")


# --------------------------------------------------------------------------
# Emitting a spec someone will want to edit
# --------------------------------------------------------------------------

#: Section order and the comment introducing each. `yaml.safe_dump` writes
#: every mapping in block style, which turns a five-line stack into twenty and
#: loses the grouping that makes a spec readable. A spec is a file a scientist
#: edits by hand, so it is worth emitting deliberately.
_SECTIONS: tuple[tuple[str, str], ...] = (
    ("schema", ""),
    ("name", ""),
    ("sample", ""),
    ("description", ""),
    (
        "materials",
        "# SLD in 1e-6/A2. `nrw data features <file>` reports the\n"
        "# critical edge and the SLD it implies -- check the top layer.",
    ),
    (
        "stack",
        "# The beam enters through the LAST entry: a cell measured through the\n"
        "# wafer ends with the substrate, a film in air ends with the air.",
    ),
    ("probe", ""),
    ("states", "# Each file's incident angle is read from its own header."),
    (
        "series",
        "# A series' angle is read from its run's summed dataset in data/steady.",
    ),
    (
        "parameters",
        "# `per:` is the whole parameter-identity system:\n"
        "#   model       one value for the entire problem\n"
        "#   state       one per steady state\n"
        "#   measurement one per angle segment or slice\n"
        "# Angle segments within a state alias automatically.",
    ),
    (
        "constraints",
        "# The endpoints are the steady-state parameters, so a\n"
        "# constraint adds no new free parameters.",
    ),
    ("fit", ""),
)

#: Keys whose list items fit on one line and read better that way.
_FLOW_LISTS = frozenset({"stack", "parameters", "constraints", "states", "series"})


def _emit_spec(document: dict[str, Any]) -> str:
    """Render a spec as YAML a person would be happy to edit.

    Flow style for anything that fits on a line, block style for anything that
    does not, and a comment above each section. The result round-trips through
    ``yaml.safe_load`` unchanged -- there is a test.
    """
    import yaml

    lines: list[str] = []
    for key, comment in _SECTIONS:
        if key not in document:
            continue
        value = document[key]
        if value in ({}, [], None, ""):
            continue
        if lines:
            lines.append("")
        if comment:
            lines.extend(comment.splitlines())

        if isinstance(value, str) and "\n" in value:
            lines.append(f"{key}: >")
            lines.extend(f"  {line}" for line in value.strip().splitlines())
        elif isinstance(value, list):
            lines.append(f"{key}:")
            lines.extend(_emit_list_items(value, key, yaml))
        elif isinstance(value, dict):
            lines.append(f"{key}:")
            lines.extend(_emit_mapping(value, yaml))
        else:
            lines.append(yaml.safe_dump({key: value}, sort_keys=False).rstrip())

    remaining = [k for k in document if k not in dict(_SECTIONS)]
    for key in remaining:
        lines.append("")
        lines.append(yaml.safe_dump({key: document[key]}, sort_keys=False).rstrip())

    return "\n".join(lines) + "\n"


def _emit_list_items(value: list[Any], key: str, yaml: Any) -> list[str]:
    """Render a list, using flow style for short mappings."""
    lines: list[str] = []
    for item in value:
        rendered = _flow(item, yaml) if key in _FLOW_LISTS else None
        if rendered is not None:
            lines.append(f"  - {rendered}")
        else:
            block = yaml.safe_dump([item], sort_keys=False, default_flow_style=False)
            lines.extend(f"  {line}" for line in block.rstrip().splitlines())
    return lines


def _emit_mapping(value: dict[str, Any], yaml: Any) -> list[str]:
    """Render a mapping, using flow style for short nested mappings."""
    lines: list[str] = []
    for name, entry in value.items():
        rendered = _flow(entry, yaml) if isinstance(entry, dict) else None
        if rendered is not None:
            lines.append(f"  {name}: {rendered}")
        else:
            block = yaml.safe_dump({name: entry}, sort_keys=False)
            lines.extend(f"  {line}" for line in block.rstrip().splitlines())
    return lines


#: Longest a flow-style mapping may be before it goes back to block style.
_FLOW_WIDTH = 84


def _flow(item: Any, yaml: Any) -> str | None:
    """Render one mapping in flow style, or ``None`` if it is too long."""
    if not isinstance(item, dict):
        return None
    # A short list of scalars reads fine inline (`range: [50, 200]`); a nested
    # mapping or a list of mappings does not.
    for value in item.values():
        if isinstance(value, dict) and value:
            return None
        if isinstance(value, list) and any(
            isinstance(element, dict | list) for element in value
        ):
            return None
    text = yaml.safe_dump(
        item, sort_keys=False, default_flow_style=True, width=10_000
    ).strip()
    if text.startswith("{") and text.endswith("}") and len(text) <= _FLOW_WIDTH:
        return text
    return None


# --------------------------------------------------------------------------
# Filling in the physics
# --------------------------------------------------------------------------


def _print_agent_instructions(
    layout: ProjectLayout, sample: str, notes: str, target: Path
) -> None:
    """Print the instruction to hand a coding assistant."""
    from nr_workbench.spec.authoring import (
        agent_instructions,
        find_skills,
        missing_relevant,
        relevant_skills,
    )

    skills = find_skills(layout.root)
    chosen = relevant_skills(notes, skills)
    absent = missing_relevant(notes, skills)
    if absent:
        click.echo(
            "  These bundled skills match your notes but are not installed here.\n"
            "  Install them first so the assistant has them:\n"
            "      nrw skills sync\n" + "".join(f"      - {name}\n" for name in absent)
        )
    click.echo(
        agent_instructions(
            spec_path=str(target.relative_to(layout.root)),
            notes_path=str(
                (layout.sample(sample) / "sample.md").relative_to(layout.root)
            ),
            skills=chosen or ["nrw-model-spec"],
            sample=sample,
        )
    )


def _author_from_notes(
    *, layout: ProjectLayout, document: dict[str, Any], notes: str
) -> tuple[dict[str, Any], str]:
    """Ask a configured endpoint to propose the stack from the notes.

    Returns the document unchanged, with an explanation, when no endpoint is
    configured -- a missing endpoint is a normal state, not a failure, and the
    placeholder spec is still useful.

    Args:
        layout: The project.
        document: The scaffolded skeleton.
        notes: The ``sample.md`` text.

    Returns:
        ``(document, provenance_comment)``.
    """
    from nr_workbench.aure_adapter import AureUnavailableError, complete, llm_info
    from nr_workbench.spec.authoring import (
        AuthoringError,
        build_prompt,
        describe_measurements,
        find_skills,
        merge_proposal,
        missing_relevant,
        parse_proposal,
        relevant_skills,
    )

    info = llm_info()
    if not info.get("available"):
        from nr_workbench.aure_adapter import CLAUDE_CODE, claude_code_supported

        get_one = (
            f"    a placeholder. Either set LLM_PROVIDER={CLAUDE_CODE} to use the\n"
            "    Claude Code CLI you already have (no key needed), or set\n"
            "    LLM_PROVIDER and LLM_API_KEY, or run:\n"
            if claude_code_supported()
            else (
                "    a placeholder. Either set LLM_PROVIDER and LLM_API_KEY (or\n"
                "    LLM_BASE_URL for a local endpoint), or run:\n"
            )
        )
        click.echo(
            "  ! No language-model endpoint is configured, so the stack is still\n"
            + get_one
            + f"      nrw model new {document['sample']} --name {document['name']} "
            "--print-prompt\n"
            "    and hand that to the coding assistant already open on this repo.",
            err=True,
        )
        return document, ""

    skills = find_skills(layout.root)
    chosen = relevant_skills(notes, skills)

    # The skills a sample most needs are exactly the ones `nrw init` does not
    # seed, so asking without them is the common case and it costs answer
    # quality -- a project missing `solvent-contrast-matching` produced a THF
    # SLD of 4.3, which is neither the protiated 0.18 nor the deuterated 6.35.
    absent = missing_relevant(notes, skills)
    if absent:
        click.echo(
            "  ! These bundled skills match your notes but are not installed, so\n"
            "    the model is answering without them. Install and re-run:\n"
            "        nrw skills sync\n"
            + "".join(f"        - {name}\n" for name in absent),
            err=True,
        )

    system, user = build_prompt(
        skeleton=document,
        notes=notes,
        skills=skills,
        facts=_measured_facts(layout, document, notes),
        measurements=describe_measurements(document, notes),
    )

    click.echo(
        f"  asking {info.get('provider')}/{info.get('model')} ({len(chosen)} skill(s))..."
    )
    try:
        reply = complete(system, user)
        proposal = parse_proposal(reply)
    except (AureUnavailableError, AuthoringError) as exc:
        click.echo(f"  ! {exc}\n    Keeping the placeholder stack.", err=True)
        return document, ""

    merged = merge_proposal(document, proposal)
    if proposal.rejected:
        click.echo(
            f"  discarded {len(proposal.rejected)} key(s) the model is not allowed "
            f"to set: {', '.join(sorted(proposal.rejected))}"
        )
    if proposal.dropped_paths:
        click.echo(
            "  ! dropped from the constraint for having no declared range: "
            + ", ".join(sorted(set(proposal.dropped_paths)))
            + "\n    A `free` endpoint borrows the path's range, and inventing "
            "one would be a guess.\n    Declare it in `parameters` and re-run "
            "`nrw model generate`.",
            err=True,
        )
    if proposal.notes:
        click.echo(f"  model notes: {proposal.notes}")

    comment = (
        "#\n"
        f"# The stack below was PROPOSED by {info.get('provider')}/{info.get('model')}\n"
        "# from sample.md and the project's skills. It is a starting point, not a\n"
        "# measurement -- check every layer and range before fitting. States,\n"
        "# series and angles were read from the data files and were not proposed.\n"
    )
    return merged, comment


def _measured_facts(
    layout: ProjectLayout, document: dict[str, Any], notes: str = ""
) -> str:
    """Summarise what the data itself says, for the prompt.

    Two things the notes cannot supply and the data can:

    * the critical edge, which constrains the topmost SLD independently of
      anything anyone wrote down;
    * the tNR assessment's verdict, which already names the constraint form the
      trajectory calls for. `nrw tnr assess` decides that from a(t) and the
      change template -- a far better source than either the notes or a guess,
      and it was going unused.
    """
    from nr_workbench.aure_adapter import AureUnavailableError, extract_features

    substrate = _back_reflection_substrate(document, notes)

    lines: list[str] = []
    for state in document.get("states", []):
        data_dir = layout.root / str(state.get("data_dir", ""))
        run = state.get("run")
        # Both reduction dialects, via the one module that knows their names.
        candidates = find_segments(data_dir, run)[:1]
        for path in candidates:
            try:
                import numpy as np

                data = np.loadtxt(path, ndmin=2)
                features = extract_features(data[:, 0], data[:, 1], data[:, 2])
            except (AureUnavailableError, OSError, ValueError, IndexError):
                continue
            for edge in features.critical_edges[:1]:
                estimate = float(edge.get("estimated_SLD", 0.0))
                lines.append(
                    f"  {state['name']}: critical edge Qc={edge.get('Qc', 0):.5f}, "
                    f"implying an SLD of {estimate:.2f} against vacuum "
                    f"(confidence {edge.get('confidence', '?')})"
                    + _substrate_correction(estimate, substrate)
                )

    # Only where a correction was made, and in general terms: this sample's
    # numbers are on the lines above.
    if lines and substrate is not None and substrate.sld is not None:
        lines.append(
            "  In back reflection the corrected value, not the one against "
            "vacuum, is the SLD of the medium above the substrate. Where it and "
            "the notes disagree about that medium -- its deuteration, say -- "
            "the edge is the measurement."
        )

    lines.extend(_assessment_facts(layout, document))
    return "\n".join(lines)


def _assessment_facts(layout: ProjectLayout, document: dict[str, Any]) -> list[str]:
    """Quote the tNR assessment's verdict, which names the constraint form."""
    import json

    if not document.get("series"):
        return []

    sample = str(document.get("sample", ""))
    directory = layout.sample(sample) / "assessments"
    if not directory.is_dir():
        return [
            "  No tNR assessment has been run. `nrw tnr assess <series dir>` "
            "reports whether anything changed and which constraint form the "
            "trajectory calls for."
        ]

    lines: list[str] = []
    for payload_path in sorted(directory.glob("*/*assessment.json")):
        try:
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        label = payload_path.parent.name
        verdict = str(payload.get("verdict") or "").strip()
        if verdict:
            lines.append(f"  tNR assessment [{label}]: {verdict}")

        amplitude = payload.get("amplitude") or {}
        template = payload.get("template") or {}
        details = []
        if amplitude.get("trajectory"):
            details.append(f"a(t) is {amplitude['trajectory']}")
        if amplitude.get("max_significance") is not None:
            details.append(f"max |a/sigma| = {amplitude['max_significance']}")
        if template.get("classification"):
            details.append(
                f"template is {template['classification']}"
                + (
                    f" -> implies a {template['implied_change']} change"
                    if template.get("implied_change")
                    else ""
                )
            )
        if details:
            lines.append("    " + "; ".join(details))
    return lines


@dataclass(frozen=True)
class Substrate:
    """What the notes say the beam enters through, in back reflection.

    Attributes:
        named: Every substrate material the notes name, as
            :data:`nr_workbench.aure_setup.SUBSTRATE_ALIASES` names it.
        sld: Its SLD, when exactly one is named and this repository knows it.
    """

    named: tuple[str, ...]
    sld: float | None


def _back_reflection_substrate(
    document: dict[str, Any], notes: str
) -> Substrate | None:
    """The substrate the beam enters through, as the notes name it.

    AuRE reports a critical edge as the SLD it would imply for a beam arriving
    from vacuum. In back reflection the beam arrives through the substrate
    instead, so the contrast is ``rho_medium - rho_substrate`` and the medium
    above it sits at ``estimate + rho_substrate``. On the reference sample the
    raw estimate is 4.28, which is not any solvent; corrected against silicon
    it is 6.35, which is d8-THF exactly.

    The substrate comes from the notes, because this runs while building the
    prompt, before anything has proposed a stack: the document's stack is the
    scaffold's placeholder, and its silicon says nothing about the sample.

    Args:
        document: The scaffolded spec; only its probe's ``back_reflection`` is
            read.
        notes: The ``sample.md`` text.

    Returns:
        ``None`` unless the geometry is back reflection.
    """
    from nr_workbench.aure_adapter import substrate_sld
    from nr_workbench.aure_setup import named_substrates, reads_as_back_reflection

    probe = document.get("probe") or {}
    if not (probe.get("back_reflection") or reads_as_back_reflection(notes)):
        return None
    named = tuple(named_substrates(notes))
    return Substrate(
        named=named, sld=substrate_sld(named[0]) if len(named) == 1 else None
    )


def _substrate_correction(estimate: float, substrate: Substrate | None) -> str:
    """What back reflection does to one critical-edge estimate, in words."""
    if substrate is None:
        return ""
    if substrate.sld is not None:
        (name,) = substrate.named
        return (
            f" -- in back reflection the beam enters through the {name}, so the "
            f"medium above it is at {estimate + substrate.sld:.2f} ({estimate:.2f} "
            f"plus {substrate.sld:.2f} for the {name})"
        )
    if len(substrate.named) > 1:
        why = (
            f"the notes name {', '.join(substrate.named[:-1])} and "
            f"{substrate.named[-1]}, so this does not choose"
        )
    elif substrate.named:
        why = f"no SLD is known here for {substrate.named[0]}"
    else:
        why = "the notes do not say what the substrate is"
    return (
        " -- in back reflection the beam enters through the substrate, so the "
        f"medium above it is at this plus the substrate's SLD, and {why}"
    )
