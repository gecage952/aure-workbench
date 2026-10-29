"""The fit record: what ran, on what, in what environment, and what came out.

A fit writes one immutable directory. Nothing in it is ever rewritten except
``NOTES.md``, which is explicitly the human's. That is the whole mechanism
behind "which script produced this figure" -- the answer is recorded at the
moment it is still true, rather than reconstructed later from filenames.

Layout::

    samples/<sample>/results/<fit_id>/
      manifest.json   ndip-tool-result/1 envelope + nested nrw-provenance/1
      model.py        frozen copy of the script that ran
      inputs.json     every input file with its sha256
      env/            versions.json, requirements.txt, project.patch (if dirty)
      fit/            bumps export: *.par, *-err.json, *.mc.gz, *-refl.dat, ...
      figures/        *.svg
      NOTES.md        the only mutable file here
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nr_workbench import __version__
from nr_workbench.provenance.env import Environment
from nr_workbench.provenance.hashing import (
    MODEL_ROLES,
    FileDigest,
    canonical_json,
    inputs_digest,
    sha256_bytes,
)

PROVENANCE_SCHEMA = "nrw-provenance/1"
RECORD_SCHEMA = "nrw-fit-record/1"

#: The one file inside a fit directory a human may edit.
NOTES_FILENAME = "NOTES.md"


def utc_now() -> datetime:
    """Return the current UTC time.

    Wrapped so tests can freeze it without patching :mod:`datetime` globally.

    Returns:
        Timezone-aware current UTC time.
    """
    return datetime.now(UTC)


def format_timestamp(moment: datetime) -> str:
    """Format a timestamp for records and identifiers.

    Args:
        moment: The moment to format.

    Returns:
        ISO-8601 UTC, second precision, e.g. ``2026-08-05T14:03:11Z``.
    """
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_fit_id(moment: datetime, identity_hash: str) -> str:
    """Build a fit identifier.

    Sortable by time *and* self-identifying by content: two fits of the same
    script and data share a suffix, so replicates are visible at a glance in a
    directory listing.

    Args:
        moment: When the fit started.
        identity_hash: Hex digest identifying the fit's inputs and settings.

    Returns:
        An identifier like ``20260805-140311Z-3f9a1c22``.
    """
    stamp = moment.astimezone(UTC).strftime("%Y%m%d-%H%M%SZ")
    return f"{stamp}-{identity_hash[:8]}"


@dataclass
class FitIdentity:
    """The digests that decide whether two fits are the same run.

    Attributes:
        script_sha256: Hash of the executed script.
        inputs_digest: One digest over every input file.
        settings_digest: Hash of the fit settings (method, steps, seed, ...).
        env_digest: Hash of the tracked package versions.
    """

    script_sha256: str
    inputs_digest: str
    settings_digest: str
    env_digest: str

    @property
    def run_key(self) -> str:
        """A single digest identifying this exact run.

        Two fits with the same run key differ in nothing that should change
        the answer, so the second is a replicate rather than new work.
        """
        return sha256_bytes(
            "\n".join(
                [
                    self.script_sha256,
                    self.inputs_digest,
                    self.settings_digest,
                    self.env_digest,
                ]
            ).encode("utf-8")
        )

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-serialisable form, including the derived run key."""
        return {
            "script_sha256": self.script_sha256,
            "inputs_digest": self.inputs_digest,
            "settings_digest": self.settings_digest,
            "env_digest": self.env_digest,
            "run_key": self.run_key,
        }


@dataclass
class FitRecord:
    """Everything recorded about one fit.

    Attributes:
        fit_id: The run's identifier.
        sample: Sample the fit belongs to, or ``None`` for a project-level run.
        model: Model name, normally the script stem.
        script_origin: ``"generated"`` when produced from a spec, ``"script"``
            when a hand-written script was run directly. M1 only produces the
            latter; keeping the field from the start means records written now
            stay readable once the generator exists.
        identity: The digests that define this run.
        inputs: Every input file with its digest.
        settings: Fit settings as passed.
        environment: The captured environment.
        started_at: ISO-8601 UTC start time.
        finished_at: ISO-8601 UTC finish time, if the fit completed.
        status: ``ok``, ``failed``, or ``dry-run``.
        chisq: Reduced chi-squared, if the fit produced one.
        n_free: Number of free parameters.
        n_points: Number of data points.
        converged: Whether the sampler reported convergence; ``None`` when the
            fitter does not test it. Recorded because a non-converged fit can
            have the best chi-squared of a set, so this is the caveat most
            likely to be lost and most needed.
        artifacts: Named output files, relative to the fit directory.
        models: Export position to model name, so a consumer can tie
            ``<basename>-3-refl.dat`` back to the measurement it came from
            rather than inferring it from build order.
        command: The command line that produced this record.
        note: Optional free-text note supplied at run time.
        stack: The layer stack as ``THF|Cu|Ti|Si``. Recorded rather than
            derived on demand because the index outlives the result directory
            --- a listing still shows a fit whose artifacts were deleted, and
            the structure is the one thing that makes such a row mean anything.
        error: Failure message when ``status`` is ``failed``.
    """

    fit_id: str
    sample: str | None
    model: str
    script_origin: str
    identity: FitIdentity
    inputs: list[FileDigest]
    settings: dict[str, Any]
    environment: Environment
    started_at: str
    finished_at: str | None = None
    status: str = "ok"
    chisq: float | None = None
    n_free: int | None = None
    n_points: int | None = None
    converged: bool | None = None
    artifacts: dict[str, str] = field(default_factory=dict)
    models: list[dict[str, Any]] = field(default_factory=list)
    command: str = ""
    note: str | None = None
    stack: str = ""
    error: str | None = None

    def data_digest(self) -> str:
        """Digest the measurements, excluding the script that read them.

        Returns:
            A digest that changes only when the data does.
        """
        return inputs_digest(d for d in self.inputs if d.role not in MODEL_ROLES)

    def provenance_block(self) -> dict[str, Any]:
        """Return the ``nrw-provenance/1`` block nested inside the manifest."""
        return {
            "schema": PROVENANCE_SCHEMA,
            "fit_id": self.fit_id,
            "sample": self.sample,
            "model": self.model,
            "script_origin": self.script_origin,
            "identity": self.identity.as_dict(),
            "inputs_count": len(self.inputs),
            "env": self.environment.as_dict(),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "command": self.command,
            "note": self.note,
            "nr_workbench": __version__,
        }

    def index_entry(self) -> dict[str, Any]:
        """Return the one-line summary appended to ``.nrw/index.jsonl``.

        Kept small deliberately: the index is scanned by every ``ls`` and
        ``whence``, and the full record is one file read away.
        """
        return {
            "schema": RECORD_SCHEMA,
            "fit_id": self.fit_id,
            "sample": self.sample,
            "model": self.model,
            "script_origin": self.script_origin,
            "status": self.status,
            "chisq": self.chisq,
            "n_free": self.n_free,
            "converged": self.converged,
            "method": self.settings.get("method"),
            # The whole settings dict, not just the method: it is a handful of
            # scalars, and without it a listing can say that a run differs but
            # not that it differs by `steps 2000 -> 20000`, which is the only
            # form of that answer anyone can use.
            "settings": {k: v for k, v in self.settings.items() if v is not None},
            "run_key": self.identity.run_key,
            "inputs_digest": self.identity.inputs_digest,
            # The measurements alone. `inputs_digest` covers the script too, so
            # it cannot answer "did the data change?" -- editing the model moves
            # it, and calling that a data change is the one confusion `nrw diff`
            # exists to prevent.
            "data_digest": self.data_digest(),
            "script_sha256": self.identity.script_sha256,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "note": self.note,
            # One short string, and the only field here a person reads as a
            # description of the science rather than of the run.
            "stack": self.stack,
        }


def settings_digest(settings: dict[str, Any]) -> str:
    """Hash the fit settings.

    Args:
        settings: Method, steps, burn, seed and so on.

    Returns:
        Lowercase hex digest over the canonical JSON.
    """
    return sha256_bytes(canonical_json(settings).encode("utf-8"))


def env_digest(environment: Environment) -> str:
    """Hash the parts of the environment that can change a result.

    Only the tracked package versions and the Python version -- not the
    platform string or interpreter path, which differ between machines without
    implying a different answer.

    Args:
        environment: The captured environment.

    Returns:
        Lowercase hex digest.
    """
    payload = {"python": environment.python, "packages": environment.packages}
    if environment.aure_commit:
        payload["aure_commit"] = environment.aure_commit
    return sha256_bytes(canonical_json(payload).encode("utf-8"))


class FitDirectory:
    """Creates and populates one immutable fit directory."""

    def __init__(self, path: Path) -> None:
        """Bind to a fit directory path.

        Args:
            path: Where the fit directory lives.
        """
        self.path = Path(path)

    def create(self) -> None:
        """Create the directory, refusing to reuse an existing one.

        Raises:
            FileExistsError: If the directory already exists. Fit directories
                are write-once; reusing one would silently destroy a record.
        """
        self.path.mkdir(parents=True, exist_ok=False)
        (self.path / "env").mkdir()
        (self.path / "fit").mkdir()
        (self.path / "figures").mkdir()

    def freeze_script(self, script: Path) -> None:
        """Copy the executed script, and the spec it came from, into the record.

        The spec matters as much as the script. It is the thing a human edits
        and the only place the *intent* is written down -- which layers are
        tied, what the constraint asserts, which endpoints are anchored. A
        record holding only the generated Python can be re-run but not
        re-reasoned about, and anything reading the model back (the trajectory
        view, `nrw diff`) has to reconstruct from generated code instead.

        A hand-written script has no spec, and that is normal rather than an
        error -- running one unchanged is a supported path.

        Args:
            script: The script that was run.
        """
        shutil.copy2(script, self.path / "model.py")

        spec = script.with_suffix(".yaml")
        if spec.is_file():
            shutil.copy2(spec, self.path / "spec.yaml")

        explanation = script.with_suffix(".md")
        if explanation.is_file():
            shutil.copy2(explanation, self.path / "model.md")

    def write_inputs(self, inputs: list[FileDigest]) -> None:
        """Write ``inputs.json``.

        Args:
            inputs: Every input file with its digest.
        """
        payload = {
            "schema": "nrw-inputs/1",
            "inputs_digest": inputs_digest(inputs),
            "inputs": [d.as_dict() for d in inputs],
        }
        _write_json(self.path / "inputs.json", payload)

    def write_environment(self, environment: Environment) -> None:
        """Write the environment capture, including a patch when git is dirty.

        Args:
            environment: The captured environment.
        """
        _write_json(self.path / "env" / "versions.json", environment.as_dict())
        if environment.requirements:
            (self.path / "env" / "requirements.txt").write_text(
                "\n".join(environment.requirements) + "\n", encoding="utf-8"
            )
        if environment.git.patch:
            # Without this, a fit run from a dirty tree records a commit that
            # does not describe the code that actually ran.
            (self.path / "env" / "project.patch").write_text(
                environment.git.patch, encoding="utf-8"
            )

    def write_manifest(self, record: FitRecord) -> dict[str, Any]:
        """Write ``manifest.json`` and return it.

        The outer envelope is ``ndip-tool-result/1``, produced by the vendored
        writer, so any orchestrator that already drives nr-analyzer or
        data-assembler can drive this unchanged. Our detail nests inside.

        Args:
            record: The fit record.

        Returns:
            The manifest that was written.
        """
        from nr_workbench.provenance.result_manifest import build_manifest

        manifest = build_manifest(
            "nrw-fit-run",
            record.status,
            params=dict(record.settings),
            artifacts=dict(record.artifacts),
            info={
                "chisq": record.chisq,
                "converged": record.converged,
                "n_free": record.n_free,
                "n_points": record.n_points,
                "models": record.models,
                "error": record.error,
            },
            exit_code=0 if record.status == "ok" else 1,
        )
        manifest["provenance"] = record.provenance_block()
        _write_json(self.path / "manifest.json", manifest)
        return manifest

    def write_notes_stub(
        self, fit_id: str = "", description: str = "", why: str = ""
    ) -> None:
        """Create ``NOTES.md``, the only mutable file in the record.

        The template asks questions rather than granting permission. Its
        predecessor was two HTML comments saying which file you were allowed
        to edit, and across a real 25-fit beamtime not one copy was ever
        written in: a blank page with no question is a file you close again.

        ``why`` is filed under the heading that asks for it rather than left in
        the blockquote. Someone who ran ``--note "testing a thicker oxide"``
        *did* say why, and the blockquote is scaffolding that
        :func:`~nr_workbench.notes.is_blank` skips by design --- so that
        sentence used to be written, stored, and then counted as nothing.

        Args:
            fit_id: Used as the heading, so the file identifies itself.
            description: A one-line reminder of what the run was.
            why: The reason the run was launched, for "Why this run".
        """
        from nr_workbench.notes import NOTES_TEMPLATE, SECTIONS, write_section

        text = NOTES_TEMPLATE.format(
            fit_id=fit_id or self.path.name, description=description
        )
        if why.strip():
            text = write_section(text, SECTIONS["why"], why)
        (self.path / NOTES_FILENAME).write_text(text, encoding="utf-8")

    def read_manifest(self) -> dict[str, Any]:
        """Read this fit's manifest.

        Returns:
            The parsed manifest.

        Raises:
            FileNotFoundError: If there is no manifest.
            json.JSONDecodeError: If it is not valid JSON.
        """
        return json.loads((self.path / "manifest.json").read_text(encoding="utf-8"))

    def read_inputs(self) -> list[dict[str, Any]]:
        """Read this fit's recorded inputs.

        Returns:
            The input entries, or an empty list if none were recorded.
        """
        path = self.path / "inputs.json"
        if not path.is_file():
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
        entries = payload.get("inputs")
        return entries if isinstance(entries, list) else []


#: How many same-second replicates to disambiguate before giving up.
_MAX_COLLISION_SUFFIX = 100


def create_unique(parent: Path, fit_id: str) -> tuple[FitDirectory, str]:
    """Create a fit directory, disambiguating an identifier collision.

    A fit_id is a second-resolution timestamp plus a content hash, so two
    forced replicates of the same run started in the same second collide.
    ``mkdir`` is atomic, so the loop is also what makes concurrent fits safe:
    whichever process loses the race simply takes the next suffix.

    Args:
        parent: Directory that will hold the fit directory.
        fit_id: The preferred identifier.

    Returns:
        The created directory and the identifier actually used.

    Raises:
        FileExistsError: If no free identifier is found.
    """
    for attempt in range(_MAX_COLLISION_SUFFIX):
        candidate = fit_id if attempt == 0 else f"{fit_id}-{attempt + 1}"
        directory = FitDirectory(parent / candidate)
        try:
            directory.create()
        except FileExistsError:
            continue
        return directory, candidate

    raise FileExistsError(
        f"Could not find a free fit directory under {parent} for {fit_id} "
        f"after {_MAX_COLLISION_SUFFIX} attempts."
    )


def _write_json(path: Path, payload: Any) -> None:
    """Write JSON with a trailing newline, creating parents as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
