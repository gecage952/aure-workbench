"""The Experiment page's JSON API, mirroring :class:`~nr_workbench.web.experiment.ExperimentData`.

Every route is a one-line call plus error mapping, for the same reason as
:mod:`nr_workbench.web.api`: an agent debugging the page can ``curl`` exactly
what the browser received. This blueprint is the only place in the server that
accepts a write, and every write passes :func:`~nr_workbench.web.security.
refuse_unless_writer` first.

Status codes say whose problem it is: 400 the request was wrong, 403 it is not
allowed from here, 404 there is no such run, spec or job, 409 something changed
or must be resolved first -- or an nrw command declined, saying why -- 503 the
catalog cannot be read, 504 the data source, or an nrw command, did not answer.
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, current_app, jsonify, request
from werkzeug.exceptions import HTTPException

from nr_workbench.bounded import Busy, TimedOut
from nr_workbench.web import security
from nr_workbench.web.curation import CurationData
from nr_workbench.web.experiment import (
    ExperimentData,
    RequestError,
    RunNotListedError,
    WritesDisabledError,
)
from nr_workbench.web.isaac import IsaacData
from nr_workbench.web.jobs import CommandRefused, JobBusy, JobNotFound
from nr_workbench.web.llm_settings import MISSING, LlmSettingsData
from nr_workbench.web.models import ModelNotFound, ModelsData
from nr_workbench.web.settings import SettingsData, WriteFailedError

experiment_api = Blueprint("experiment_api", __name__, url_prefix="/api/experiment")


def data() -> ExperimentData:
    """The request's :class:`ExperimentData`, held on the app config."""
    return current_app.config["NRW_EXPERIMENT"]  # type: ignore[no-any-return]


def settings_data() -> SettingsData:
    """The request's :class:`SettingsData`, held on the app config."""
    return current_app.config["NRW_SETTINGS"]  # type: ignore[no-any-return]


def models_data() -> ModelsData:
    """The request's :class:`ModelsData`, held on the app config."""
    return current_app.config["NRW_MODELS"]  # type: ignore[no-any-return]


def llm_data() -> LlmSettingsData:
    """The request's :class:`LlmSettingsData`, held on the app config."""
    return current_app.config["NRW_LLM"]  # type: ignore[no-any-return]


def curation_data() -> CurationData:
    """The request's :class:`CurationData`, held on the app config."""
    return current_app.config["NRW_CURATION"]  # type: ignore[no-any-return]


def isaac_data() -> IsaacData:
    """The request's :class:`IsaacData`, held on the app config."""
    return current_app.config["NRW_ISAAC"]  # type: ignore[no-any-return]


#: The writes the Settings page makes, which ``USER_ALLOW_SETTINGS`` opens to a
#: logged-in user. Nothing else here changes with it.
SETTINGS_ENDPOINTS = frozenset(
    {
        "experiment_api.save_settings",
        "experiment_api.check_folder",
        "experiment_api.save_llm",
        "experiment_api.check_llm",
    }
)


@experiment_api.before_request
def _gate() -> None:
    security.refuse_unless_writer(for_settings=request.endpoint in SETTINGS_ENDPOINTS)


def _body() -> dict[str, Any]:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise RequestError("the request body must be a JSON object")
    return payload


def _error(exc: Exception, status: int) -> tuple[Any, int]:
    payload: dict[str, Any] = {"error": str(exc), "kind": type(exc).__name__}
    # What a person can do about it: the TOML to add by hand, or what to
    # confirm and send again.
    for detail in ("lines", "needs"):
        value = getattr(exc, detail, None)
        if value:
            payload[detail] = value
    return jsonify(payload), status


@experiment_api.errorhandler(Exception)
def _map(exc: Exception) -> tuple[Any, int]:
    """Turn each failure into an honest status code, as JSON.

    Only the errors nrw raises on purpose are mapped. Anything else is a bug,
    or a failure nobody anticipated: it is logged with its traceback and
    answered 500, never passed off as the caller's mistake.
    """
    from nr_workbench.experiment.adopt import AdoptRefused
    from nr_workbench.experiment.apply import ApplyError
    from nr_workbench.experiment.model import CatalogValidationError, RecordConflict
    from nr_workbench.experiment.render import SampleRenderError
    from nr_workbench.experiment.store import CatalogError
    from nr_workbench.project.envfile import EnvFileError, EnvValueError
    from nr_workbench.project.scaffold import LockProblemError
    from nr_workbench.project.settings import NeedsConfirmation, SettingsError
    from nr_workbench.project.tomlfile import TomlEditError
    from nr_workbench.provenance.curation import CurationRefused, NoSuchFit

    if isinstance(exc, HTTPException):
        return jsonify({"error": exc.description}), exc.code or 500
    for kinds, status in (
        ((RunNotListedError, JobNotFound, ModelNotFound, NoSuchFit), 404),
        ((WritesDisabledError,), 403),
        (
            (
                RecordConflict,
                ApplyError,
                AdoptRefused,
                LockProblemError,
                SampleRenderError,
                NeedsConfirmation,
                TomlEditError,
                Busy,
                CommandRefused,
                JobBusy,
                EnvFileError,
                CurationRefused,
            ),
            409,
        ),
        ((CatalogError,), 503),
        ((TimedOut,), 504),
        # Expected, and said: a read-only project, a full disk. Not the
        # generic 500 -- the person can do something about it.
        ((WriteFailedError,), 500),
        ((RequestError, CatalogValidationError, SettingsError, EnvValueError), 400),
    ):
        if isinstance(exc, kinds):
            return _error(exc, status)
    current_app.logger.exception("Unhandled error in %s", request.path)
    # Not str(exc): an unexpected error's text can carry paths and data, and
    # the log already has all of it.
    return (
        jsonify(
            {
                "error": "nrw hit an unexpected error; the server's log has the details.",
                "kind": "InternalError",
            }
        ),
        500,
    )


def _http_json(exc: HTTPException) -> tuple[Any, int]:
    return jsonify({"error": exc.description}), exc.code or 500


# Code-specific, on the blueprint: Flask tries every level's handler *for the
# status code* before any level's class-based handler, so the app's HTML 403
# and 404 pages would otherwise answer this API's refusals -- and a script
# reading `response.json()["error"]` would get a page of markup instead.
for _code in (400, 403, 404, 405, 409, 413, 415):
    experiment_api.register_error_handler(_code, _http_json)


def _list_arg(name: str) -> list[str] | None:
    raw = request.args.get(name, "")
    return [part for part in raw.split(",") if part] or None


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


@experiment_api.get("")
def overview() -> Any:
    """Everything the page shows on load."""
    return jsonify(data().overview())


@experiment_api.get("/changes")
def changes() -> Any:
    """What changed since ``?since=<cursor>``."""
    return jsonify(data().changes(request.args.get("since")))


@experiment_api.get("/runs/<int:run>/curves")
def curves(run: int) -> Any:
    """One run's reflectivity, from the data source."""
    return jsonify(data().curves(run))


@experiment_api.get("/samples/<sample_id>/preview")
def preview(sample_id: str) -> Any:
    """The sample.md the catalog would write, and where the file stands."""
    return jsonify(data().sample_preview(sample_id))


@experiment_api.get("/apply")
def apply_plan() -> Any:
    """What applying would do: ``?samples=S1,S2&confirm=218386``."""
    return jsonify(data().apply_plan(_list_arg("samples"), _list_arg("confirm")))


@experiment_api.get("/samples/<sample_id>/adopt")
def adopt_plan(sample_id: str) -> Any:
    """What adopting, or pulling, a sample's sample.md would do."""
    return jsonify(data().adopt_plan(sample_id))


# ---------------------------------------------------------------------------
# Writing -- every one behind the gate above
# ---------------------------------------------------------------------------


@experiment_api.put("/runs")
def update_runs() -> Any:
    """Record run edits: ``{"changes": [{"run", "base_rev", "fields"}]}``."""
    return jsonify(data().update_runs(_body().get("changes")))


@experiment_api.put("/samples/<sample_id>")
def update_sample(sample_id: str) -> Any:
    """Record a sample's context: ``{"base_rev", "fields"}`` or ``{"delete": true}``."""
    return jsonify(data().update_sample(sample_id, _body()))


@experiment_api.post("/apply")
def apply() -> Any:
    """Carry out a reviewed plan: ``{"plan_id", "samples", "confirmed"}``."""
    body = _body()
    return jsonify(
        data().apply(body.get("plan_id"), body.get("samples"), body.get("confirmed"))
    )


@experiment_api.post("/samples/<sample_id>/adopt")
def adopt(sample_id: str) -> Any:
    """Adopt or pull a sample's sample.md: ``{"plan_id", "rewrite": bool}``."""
    body = _body()
    return jsonify(
        data().adopt(sample_id, body.get("rewrite", False), body.get("plan_id"))
    )


# ---------------------------------------------------------------------------
# Settings -- reading is open; saving, and checking a folder or the language
# model, are behind the gate
# ---------------------------------------------------------------------------


@experiment_api.get("/settings")
def settings() -> Any:
    """The IPTS, the data source and the watcher, as nrw.toml has them."""
    return jsonify(settings_data().settings())


@experiment_api.put("/settings")
def save_settings() -> Any:
    """Save settings: ``{"revision", "changes": {...}, "confirmed": [...]}``."""
    body = _body()
    return jsonify(
        settings_data().save_settings(
            body.get("revision"), body.get("changes"), body.get("confirmed")
        )
    )


@experiment_api.post("/settings/check")
def check_folder() -> Any:
    """What a folder holds, before choosing it: ``{"location", "ipts", "kind"}``."""
    body = _body()
    return jsonify(
        settings_data().check_folder(
            body.get("location"), body.get("ipts"), body.get("kind")
        )
    )


@experiment_api.get("/settings/llm")
def llm_settings() -> Any:
    """The language model the page's jobs use, and where each part is set."""
    return jsonify(llm_data().llm())


@experiment_api.put("/settings/llm")
def save_llm() -> Any:
    """Choose it, in the project's .env: ``{"revision", "provider", "model"}``."""
    body = _body()
    return jsonify(
        llm_data().save_llm(
            body.get("revision"), body.get("provider", MISSING), body.get("model", "")
        )
    )


@experiment_api.post("/settings/llm/check")
def check_llm() -> Any:
    """Make one real call to it, as ``nrw check-llm --endpoint`` does."""
    _body()
    return jsonify(llm_data().check_llm())


# ---------------------------------------------------------------------------
# Curating fits -- what is said about a fit is read with the fit; every change
# is behind the gate
# ---------------------------------------------------------------------------


@experiment_api.post("/fits/<fit_id>/star")
def star_fit(fit_id: str) -> Any:
    """Star a fit, or take its star away: ``{"starred": true|false}``."""
    return jsonify(curation_data().star(fit_id, _body().get("starred")))


@experiment_api.post("/fits/<fit_id>/discard")
def discard_fit(fit_id: str) -> Any:
    """Set a fit aside, every file kept: ``{"reason"}``."""
    return jsonify(curation_data().discard(fit_id, _body().get("reason")))


@experiment_api.post("/fits/<fit_id>/restore")
def restore_fit(fit_id: str) -> Any:
    """Bring a discarded fit back."""
    _body()
    return jsonify(curation_data().restore(fit_id))


@experiment_api.post("/fits/<fit_id>/delete")
def delete_fit(fit_id: str) -> Any:
    """Delete a discarded fit's files: ``{"confirm": <its id>}``."""
    return jsonify(curation_data().delete(fit_id, _body().get("confirm")))


@experiment_api.post("/fits/<fit_id>/finalize")
def finalize_fit(fit_id: str) -> Any:
    """Make a fit its sample's final one: ``{"reason", "force"}``."""
    body = _body()
    return jsonify(
        curation_data().finalize(fit_id, body.get("reason"), body.get("force", False))
    )


@experiment_api.get("/fits/<fit_id>/isaac")
def isaac_status(fit_id: str) -> Any:
    """Where a fit stands with ISAAC: the tools, the portal, its records."""
    return jsonify(isaac_data().status(fit_id))


@experiment_api.post("/fits/<fit_id>/isaac/<step>")
def isaac_step(fit_id: str, step: str) -> Any:
    """Export, validate or push a final fit's records, as a job."""
    body = _body()
    return jsonify(
        isaac_data().start(fit_id, step, body.get("confirm"), body.get("host"))
    ), 202


# ---------------------------------------------------------------------------
# Models -- listing is open; writing a spec is behind the gate
# ---------------------------------------------------------------------------


@experiment_api.get("/samples/<sample_id>/models")
def sample_models(sample_id: str) -> Any:
    """One sample's specs, and whether a new one can be written."""
    return jsonify(models_data().models(sample_id))


@experiment_api.post("/samples/<sample_id>/models")
def create_model(sample_id: str) -> Any:
    """Write a spec from the notes and the data, as a job: ``{"name"}``."""
    return jsonify(models_data().create(sample_id, _body().get("name"))), 202


@experiment_api.post("/samples/<sample_id>/models/<name>/fit")
def fit_model(sample_id: str, name: str) -> Any:
    """Fit a spec in the background: ``{"method", "steps", "samples", "burn", ...}``."""
    return jsonify(models_data().fit(sample_id, name, _body())), 202


@experiment_api.get("/jobs/current")
def current_job() -> Any:
    """The job running, or the last one, and its output from ``?offset=<bytes>``.

    Without an offset, the last part of the output: what a page opening onto a
    long DREAM log shows first.
    """
    raw = request.args.get("offset")
    offset = None
    if raw is not None:
        # ASCII digits, and few enough of them: "²" is a digit to isdigit(),
        # and a number past 4300 digits is refused by int() itself.
        if not (raw.isascii() and raw.isdigit() and len(raw) <= 18):
            raise RequestError(f"offset must be a byte count, not {raw[:40]!r}.")
        offset = int(raw)
    return jsonify(models_data().job_status(offset))


@experiment_api.post("/jobs/<job_id>/cancel")
def cancel_job(job_id: str) -> Any:
    """Stop the running job."""
    return jsonify(models_data().cancel(job_id))


@experiment_api.post("/samples/<sample_id>/models/quick-fit")
def quick_fit(sample_id: str) -> Any:
    """A quick fit with AuRE, recorded as a fit of the spec it proposes: ``{"name", "run"}``."""
    body = _body()
    return jsonify(
        models_data().quick_fit(sample_id, body.get("name"), body.get("run"))
    ), 202
