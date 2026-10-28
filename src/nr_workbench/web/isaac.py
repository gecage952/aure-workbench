"""Publishing a finalized fit to ISAAC from its page: export, validate, push.

Each step is the command a person would type, run as a job
(:mod:`nr_workbench.web.jobs`): ``nrw isaac export``, then ``nrw isaac push
--validate-only``, then ``nrw isaac push``. Push sends the records the export
wrote, unchanged, so what the server validated is what is published. The rules
are the commands' own -- only the final fit of a sample is published, and each
push is recorded in the fit index -- and the page asks only for the final fit.

The key never passes through here: this says only where the portal and the
key are set, and the host the portal is.

Nothing here imports Flask; :mod:`nr_workbench.web.experiment_api` maps the
errors to status codes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nr_workbench.project.layout import ProjectLayout
from nr_workbench.web.experiment import RequestError, require_writable
from nr_workbench.web.jobs import JobRunner

#: The tools the export drives, as `nrw isaac export` finds them.
TOOLS = ("data-assembler", "nr-isaac-format")


class IsaacData:
    """The ISAAC panel of one project's fit pages.

    Args:
        root: Project root.
        writable: Whether writes are allowed at all.
        why_read_only: What a refused write says.
        jobs: The job runner the page's fits run in: one job at a time.
    """

    def __init__(
        self,
        root: Path,
        *,
        writable: bool,
        why_read_only: str = "",
        jobs: JobRunner,
    ) -> None:
        self.root = Path(root)
        self.layout = ProjectLayout(root=self.root)
        self.writable = writable
        self.why_read_only = why_read_only
        self.jobs = jobs

    def status(self, fit_id: str) -> dict[str, Any]:
        """Where one fit stands with ISAAC, and what publishing it needs.

        Returns:
            Whether it is final; which tools are installed; the portal's host
            and where ``ISAAC_URL`` is set; whether ``ISAAC_KEY`` is set and
            where -- never its value; the records its export wrote, and when;
            and each push so far.

        Raises:
            NoSuchFit: No fit is recorded under that id.
        """
        from nr_workbench.commands.isaac_cmd import (
            INSTALL,
            portal_host,
            portal_settings,
        )
        from nr_workbench.env import shown_source
        from nr_workbench.provenance.curation import (
            NoSuchFit,
            publish_refusal,
            replay,
        )
        from nr_workbench.provenance.index import FitIndex
        from nr_workbench.provenance.lookup import fit_dir

        index = FitIndex(self.layout.index_file)
        entry = index.find(fit_id)
        if entry is None:
            raise NoSuchFit(f"No fit {fit_id!r} is recorded here.")
        state = replay(index.entries()).of(fit_id)
        directory = fit_dir(self.layout, entry)
        records = (
            sorted((directory / "isaac" / "records").glob("*.json"))
            if directory is not None
            else []
        )
        url, key = portal_settings(self.root)
        return {
            "fit_id": fit_id,
            "sample": entry.get("sample"),
            "final": publish_refusal(entry, state) is None,
            "tools": {name: _installed(name) for name in TOOLS},
            "install": INSTALL,
            "portal": {
                "host": portal_host(url.value) or None if url else None,
                "from": shown_source(url.source, self.root) if url else None,
            },
            "key": {
                "set": key is not None,
                "from": shown_source(key.source, self.root) if key else None,
            },
            "records": [path.name for path in records],
            "exported_at": _when(max(p.stat().st_mtime for p in records))
            if records
            else None,
            "published": list(state.published),
            "writable": self.writable,
        }

    def start(self, fit_id: str, step: str, confirm: Any = None) -> dict[str, Any]:
        """Start one step as a job: ``export``, ``validate`` or ``push``.

        Args:
            fit_id: The fit, whole: a prefix is the terminal's shorthand.
            step: Which.
            confirm: For ``push``, the fit's id, as the person agreed to it.

        Returns:
            ``{"job": ...}``, the job started.

        Raises:
            WritesDisabledError: The server was started read-only.
            RequestError: No such step, or a push not confirmed.
            NoSuchFit: No fit is recorded under that id.
            CurationRefused: It is not final, or there is nothing to send yet.
            JobBusy: A job is running already.
        """
        from nr_workbench.provenance.curation import CurationRefused

        require_writable(self.writable, self.why_read_only)
        if step not in ("export", "validate", "push"):
            raise RequestError(f"step must be export, validate or push, not {step!r}")
        status = self.status(fit_id)
        if not status["final"]:
            raise CurationRefused(
                f"{fit_id} is not the final fit of its sample, and only a "
                "finalized fit is published. Finalize it first."
            )
        if step != "export":
            if not status["records"]:
                raise CurationRefused(
                    f"{fit_id} has no ISAAC records to send yet: export them first."
                )
            if not status["portal"]["host"] or not status["key"]["set"]:
                raise CurationRefused(
                    "Set ISAAC_URL and ISAAC_KEY to send records to the portal."
                )
        if step == "push" and confirm != fit_id:
            raise RequestError("confirm must be the id of the fit being published")
        # `--` before the id: never read as an option, whatever it is.
        steps = {
            "export": ["isaac", "export", "--", fit_id],
            "validate": ["isaac", "push", "--validate-only", "--", fit_id],
            # --yes: the page asked, naming the fit and the portal.
            "push": ["isaac", "push", "--yes", "--", fit_id],
        }
        labels = {
            "export": "ISAAC export",
            "validate": "ISAAC validation",
            "push": "ISAAC push",
        }
        job = self.jobs.start(
            label=f"{labels[step]} of {fit_id}",
            sample=str(status["sample"] or ""),
            model=fit_id,
            steps=[steps[step]],
        )
        return {"job": job.as_dict()}


def _installed(name: str) -> bool:
    """Whether `nrw isaac export` would find the tool."""
    from nr_workbench.commands.isaac_cmd import tool_installed

    return tool_installed(name)


def _when(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
