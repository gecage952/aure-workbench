"""Curating fits from the Fits and fit pages: star, finalize, discard, delete.

The rules are :mod:`nr_workbench.provenance.curation`'s -- the same the
``nrw fit`` commands and ``nrw promote`` follow -- so the page and the terminal
cannot disagree about what may be set aside or deleted. This adds only what a
page needs: the write gate, a fit named exactly (never a prefix, which a later
fit could come to match), and a confirmation that names the fit before its
files go.

Nothing here imports Flask; :mod:`nr_workbench.web.experiment_api` maps the
errors to status codes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nr_workbench.project.layout import ProjectLayout
from nr_workbench.web.experiment import RequestError, require_writable


class CurationData:
    """One project's fits, as the pages curate them.

    Args:
        root: Project root.
        writable: Whether writes are allowed at all.
        why_read_only: What a refused write says.
    """

    def __init__(
        self, root: Path, *, writable: bool = True, why_read_only: str = ""
    ) -> None:
        self.layout = ProjectLayout(root=Path(root))
        self.writable = writable
        self.why_read_only = why_read_only

    def star(self, fit_id: str, starred: Any) -> dict[str, Any]:
        """Star a fit, or take its star away: ``{"starred": true|false}``."""
        from nr_workbench.provenance import curation

        self._check(fit_id)
        if not isinstance(starred, bool):
            raise RequestError("starred must be true or false")
        curation.star(self.layout, fit_id, starred=starred)
        return self.state(fit_id)

    def discard(self, fit_id: str, reason: Any) -> dict[str, Any]:
        """Set a fit aside, with the reason: ``{"reason"}``."""
        from nr_workbench.provenance import curation

        self._check(fit_id)
        curation.discard(self.layout, fit_id, reason=_text(reason, "reason"))
        return self.state(fit_id)

    def restore(self, fit_id: str) -> dict[str, Any]:
        """Bring a discarded fit back."""
        from nr_workbench.provenance import curation

        self._check(fit_id)
        curation.restore(self.layout, fit_id)
        return self.state(fit_id)

    def delete(self, fit_id: str, confirm: Any) -> dict[str, Any]:
        """Delete a discarded fit's files: ``{"confirm": <the fit's id>}``.

        The fit's id, sent back as the confirmation, is what the page asked the
        person to agree to: a request that names no fit, or another, deletes
        nothing.
        """
        from nr_workbench.provenance import curation

        self._check(fit_id)
        if confirm != fit_id:
            raise RequestError(
                "confirm must be the id of the fit whose files are deleted"
            )
        removed = curation.delete_files(self.layout, fit_id)
        return {**self.state(fit_id), "removed": removed.as_posix()}

    def finalize(self, fit_id: str, reason: Any, force: Any = False) -> dict[str, Any]:
        """Make a fit its sample's final one: ``{"reason", "force"}``.

        ``force`` finalizes a fit whose inputs changed since it ran; without
        it, that is refused (409, ``needs: "force"``) and the page asks.
        """
        from nr_workbench.provenance import curation

        self._check(fit_id)
        if not isinstance(force, bool):
            raise RequestError("force must be true or false")
        promotion = curation.promote(
            self.layout, fit_id, reason=_text(reason, "reason"), force=force
        )
        return {**self.state(fit_id), "supersedes": promotion["supersedes"]}

    def state(self, fit_id: str) -> dict[str, Any]:
        """What has been said about one fit, as the pages show it.

        Returns:
            ``fit_id``, ``sample``, ``curation``; ``final``, the fit that is
            final for its sample now; and ``delete_refusal``, why its files
            cannot be deleted, for a discarded fit whose files are there.
        """
        from nr_workbench.provenance import curation
        from nr_workbench.provenance.index import FitIndex

        index = FitIndex(self.layout.index_file)
        entry = index.find(fit_id) or {}
        replayed = curation.replay(index.entries())
        state = replayed.of(fit_id)
        holder = replayed.holder(entry.get("sample"), curation.FINAL)
        refusal = None
        if state.discarded and not state.deleted:
            try:
                curation.deletable(self.layout, fit_id)
            except curation.CurationRefused as exc:
                refusal = str(exc)
        return {
            "fit_id": fit_id,
            "sample": entry.get("sample"),
            "curation": state.as_dict(),
            "final": str(holder["fit_id"]) if holder else None,
            "delete_refusal": refusal,
        }

    def _check(self, fit_id: str) -> None:
        """The gate, and a fit named whole: a prefix is the terminal's shorthand."""
        from nr_workbench.provenance.curation import NoSuchFit
        from nr_workbench.provenance.index import FitIndex

        require_writable(self.writable, self.why_read_only)
        if FitIndex(self.layout.index_file).find(fit_id) is None:
            raise NoSuchFit(f"No fit {fit_id!r} is recorded here.")


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise RequestError(f"{name} must be text")
    return value
