"""``nrw fit star | unstar | discard | restore | delete`` -- a person's word on a fit.

The terminal's side of what the Fits page offers. The rules -- what may be
discarded, what must be discarded before its files go, what nothing may use
if they do -- are :mod:`nr_workbench.provenance.curation`'s, so the two cannot
disagree.
"""

from __future__ import annotations

import click

from nr_workbench.project.layout import ProjectLayout, ProjectNotFoundError


def run_curate(
    action: str, fit_id: str, *, reason: str | None = None, yes: bool = False
) -> None:
    """Star, unstar, discard, restore or delete one fit.

    Args:
        action: ``star``, ``unstar``, ``discard``, ``restore`` or ``delete``.
        fit_id: The fit, or a unique prefix of its id.
        reason: Why it is discarded; required for ``discard``.
        yes: For ``delete``, skip the confirmation.

    Raises:
        click.ClickException: No project here, or the action was refused.
    """
    from nr_workbench.provenance import curation

    try:
        layout = ProjectLayout.discover()
    except ProjectNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc
    try:
        if action in ("star", "unstar"):
            changed = curation.star(layout, fit_id, starred=action == "star")
            said = "Starred" if action == "star" else "Unstarred"
        elif action == "discard":
            changed = curation.discard(layout, fit_id, reason=reason or "")
            said = "Discarded"
        elif action == "restore":
            changed = curation.restore(layout, fit_id)
            said = "Restored"
        elif action == "delete":
            _delete(layout, fit_id, yes=yes)
            return
        else:  # pragma: no cover - the CLI offers only the five
            raise click.ClickException(f"No such action: {action!r}.")
    except curation.CurationRefused as exc:
        raise click.ClickException(str(exc)) from exc
    # The whole id: a prefix typed names the fit, and the reply says which.
    click.echo(
        f"{said} {changed}." if changed else f"Nothing to do: {fit_id} is so already."
    )


def _delete(layout: ProjectLayout, fit_id: str, *, yes: bool) -> None:
    """Delete a discarded fit's files, after saying what goes."""
    from nr_workbench.provenance import curation

    # Every refusal before the question: never ask about what will not happen.
    directory = curation.deletable(layout, fit_id)
    if not yes:
        files = [p for p in directory.rglob("*") if p.is_file()]
        size = sum(p.stat().st_size for p in files) / 1e6
        notes = (
            " -- its NOTES.md, what was written about it, included"
            if (directory / "NOTES.md").is_file()
            else ""
        )
        click.echo(
            f"  This deletes {directory.relative_to(layout.root).as_posix()}/: "
            f"{len(files)} file(s), {size:.1f} MB{notes}.\n  The record that the "
            "fit ran stays in the index."
        )
        if not click.confirm("  Delete them?", default=False):
            click.echo("  Nothing deleted.")
            return
    try:
        removed = curation.delete_files(layout, fit_id)
    except curation.CurationRefused as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Deleted {removed.as_posix()}/.")
