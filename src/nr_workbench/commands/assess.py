"""``nrw assess`` -- check a finished fit and write what was found.

The command runs the arithmetic checks always, adds a language model's reading
when one is configured, and appends the result to the fit's ``NOTES.md``.

That last step is the point. An assessment printed to a terminal is gone by the
next command; an assessment in the note is in the bundle, on the fit page, and
in front of whoever asks about this fit in a year. It also means the notebook
is never empty: every fit has at least the automatic section, so the human is
adding to a page rather than starting one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

from nr_workbench.fitting.assess import VERDICT_LISTS, Assessment, as_markdown, check
from nr_workbench.notes import NOTES_FILENAME
from nr_workbench.project.layout import ProjectLayout, ProjectNotFoundError
from nr_workbench.provenance.index import FitIndex
from nr_workbench.provenance.lookup import FitNotFoundError, resolve_fit
from nr_workbench.provenance.record import FitDirectory

_SEVERITY_COLOUR = {"blocker": "red", "warn": "yellow", "info": None}


def run_assess(
    *,
    fit_id: str,
    write: bool = True,
    use_llm: bool = True,
    as_json: bool = False,
) -> None:
    """Assess one fit.

    Args:
        fit_id: The fit, or a unique prefix.
        write: Append the assessment to the fit's NOTES.md.
        use_llm: Ask a language model too, when one is configured.
        as_json: Emit machine-readable JSON instead of prose.

    Raises:
        click.ClickException: If there is no project or no such fit.
    """
    try:
        layout = ProjectLayout.discover()
    except ProjectNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc

    index = FitIndex(layout.index_file)
    try:
        entry, directory = resolve_fit(layout, index, fit_id)
    except FitNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc

    manifest = FitDirectory(directory).read_manifest()
    assessment = check(directory, manifest)

    if use_llm:
        _add_judgement(assessment, layout, entry, directory, manifest)

    if as_json:
        click.echo(json.dumps(assessment.as_dict(), indent=2, default=str))
        return

    _print(assessment)

    if write:
        path = directory / NOTES_FILENAME
        if not path.is_file():
            FitDirectory(directory).write_notes_stub(fit_id=assessment.fit_id)
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n" + as_markdown(assessment))

        # Also as data, beside the prose. The markdown block is for a person;
        # `nrw report` needs the findings as records so it can work out which
        # concepts the analysis actually ran into and explain those, rather
        # than shipping a fixed syllabus nobody asked for.
        _write_assessment_json(directory, assessment)

        click.echo()
        click.echo(f"  written to {path.relative_to(layout.root)}")
        _nudge_if_unwritten(path, assessment.fit_id)


def _write_assessment_json(directory: Path, assessment: Assessment) -> None:
    """Persist the assessment beside the fit, for other commands to read.

    Failure here must not lose the assessment the user just watched print, so
    it is reported and swallowed rather than raised.
    """
    target = directory / "assessment.json"
    try:
        target.write_text(
            json.dumps(assessment.as_dict(), indent=2, default=str) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        click.secho(f"  ! could not write {target.name}: {exc}", fg="yellow")


def _nudge_if_unwritten(path: Path, fit_id: str) -> None:
    """Say so when the note still holds nothing a person wrote.

    `nrw assess --write` fills the generated block, and a note holding only that
    block *looks* written to anyone skimming it -- headings, prose, numbers.
    Every note in the reference sample was in exactly that state: three empty
    template sections with a machine-written assessment under them. `nrw ls`
    knew, because it strips the generated fence before asking, but the one
    command guaranteed to run right after a fit said nothing -- so the gap
    survived a whole unattended session.
    """
    from nr_workbench.notes import is_blank

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    if not is_blank(text):
        return

    click.echo()
    click.secho(
        "  ! That is what the checks found, not what you concluded. This note "
        "still has\n    nothing you wrote in it:\n"
        f'      nrw note {fit_id[-8:]} --why "..." --showed "..." --caveat "..."',
        fg="yellow",
    )


def _add_judgement(
    assessment: Assessment,
    layout: ProjectLayout,
    entry: dict[str, Any],
    directory: Path,
    manifest: dict[str, Any],
) -> None:
    """Attach a language model's reading, or record why there is none."""
    from nr_workbench.agent.guard import agent_is_driving
    from nr_workbench.aure_adapter import AureUnavailableError, llm_available

    if agent_is_driving():
        # You are the model. Sending this to a configured endpoint would
        # substitute a second, weaker model's verdict for yours -- and then
        # hand it back as though it were evidence.
        assessment.problems.append(
            "Judge these values yourself: are they physically sensible for "
            "this sample, and does anything above change what the fit can be "
            "said to show? Read the skills for the material. Record the answer "
            "with `nrw note`, including what you rejected."
        )
        return

    if not llm_available():
        from nr_workbench.aure_adapter import endpoint_hint

        assessment.problems.append(
            "No language-model endpoint configured, so nothing judged whether "
            "these values are physically sensible. "
            f"{endpoint_hint()} `nrw doctor` reports what it sees."
        )
        return

    from nr_workbench.aure_adapter import complete
    from nr_workbench.fitting import judge
    from nr_workbench.fitting.assess import (
        per_model_chisq,
        read_bounds,
        read_par,
        read_uncertainty,
    )
    from nr_workbench.notes import inert
    from nr_workbench.spec.authoring import find_skills

    if not isinstance(assessment.chisq, int | float):
        assessment.problems.append(
            "No chi-squared recorded, so the fit could not be judged."
        )
        return

    parameters = read_par(directory)
    if not parameters:
        assessment.problems.append(
            "No fitted values to judge, so no language model was asked."
        )
        return

    notes = _sample_notes(layout, entry.get("sample"))
    spec = _frozen_spec_text(directory)
    reference, unreadable = judge.reference_for(notes, spec, find_skills(layout.root))
    if unreadable:
        assessment.problems.append(
            f"Could not read the skill(s) {', '.join(unreadable)}, so the "
            "language model was asked without them."
        )

    system, user = judge.build_prompt(
        assessment=assessment,
        notes=notes,
        spec=spec,
        parameters=parameters,
        bounds=read_bounds(directory),
        uncertainty=read_uncertainty(directory),
        per_model=per_model_chisq(directory),
        method=str((manifest.get("params") or {}).get("method") or "unknown"),
        converged=(manifest.get("info") or {}).get("converged"),
        reference=reference,
    )
    try:
        reply = complete(system, user)
    except AureUnavailableError as exc:
        # The provider's own text: one inert line, as everything from outside is.
        assessment.problems.append(inert(exc, 2000))
        return

    verdict = judge.read_verdict(reply)
    if verdict is None:
        # Never a stand-in verdict: a heuristic written up under "What a
        # language model made of it" would read as the model's opinion.
        assessment.problems.append(
            "The language model answered, but not with a verdict that could be "
            "read, so nothing judged whether these values are physically sensible."
        )
        return
    assessment.judgement = verdict


def _sample_notes(layout: ProjectLayout, sample: Any) -> str:
    """What the scientist wrote in ``sample.md``, or ``""`` when it has none.

    Without the template's guidance: its examples describe other samples.
    """
    from nr_workbench.notes import strip_comments

    if not sample:
        return ""
    path = layout.sample(str(sample)) / "sample.md"
    return strip_comments(path.read_text(encoding="utf-8")) if path.is_file() else ""


def _frozen_spec_text(directory: Path) -> str:
    """The spec the fit froze, as written, or ``""`` for a fit without one."""
    path = directory / "spec.yaml"
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _print(assessment: Assessment) -> None:
    """Print the assessment."""
    click.echo(f"  fit       {assessment.fit_id}")
    if assessment.chisq is not None:
        click.echo(
            f"  chisq     {assessment.chisq:.4g}"
            + (f"   BIC {assessment.bic:.1f}" if assessment.bic is not None else "")
        )
    click.echo(f"  free      {assessment.n_free}   points {assessment.n_points}")
    click.echo()

    if not assessment.findings:
        click.secho("  Nothing flagged by the automatic checks.", fg="green")
    for finding in assessment.findings:
        click.secho(
            f"  {finding.severity:<7} {finding.message}",
            fg=_SEVERITY_COLOUR.get(finding.severity),
        )

    judgement = assessment.judgement
    if judgement:
        click.echo()
        click.echo(f"  model     {judgement.get('quality_assessment', 'unknown')}")
        for key, _ in VERDICT_LISTS:
            for item in judgement.get(key) or []:
                click.echo(f"    {key[:-1] if key.endswith('s') else key}: {item}")

    for problem in assessment.problems:
        click.secho(f"  ! {problem}", dim=True)
