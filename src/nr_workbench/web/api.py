"""The JSON API, mirroring :class:`~nr_workbench.web.project.ProjectData` 1:1.

Every route here is a one-line call onto ``ProjectData`` plus error mapping.
That is deliberate: an agent debugging a plot should be able to ``curl`` the
exact bytes the browser received, and the only way that stays true is if the
API adds nothing of its own.

Errors come back as ``{"error": ...}`` with an honest status code rather than
an HTML traceback, because the primary consumer is a script.
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, current_app, jsonify, request
from werkzeug.exceptions import HTTPException

from nr_workbench.web.project import ProjectData
from nr_workbench.web.user_session import refuse_unless_logged_in


def data() -> ProjectData:
    """Return the request's :class:`ProjectData`, held on the app config."""
    return current_app.config["NRW_DATA"]  # type: ignore[no-any-return]


api = Blueprint("api", __name__, url_prefix="/api")

# Once an identity provider is configured, every route here needs a logged-in
# user; without one, the API stays open as the local tool it was.
api.before_request(refuse_unless_logged_in)


@api.errorhandler(FileNotFoundError)
def _not_found(exc: FileNotFoundError) -> tuple[Any, int]:
    """Map a missing sample, series, or fit to a 404."""
    return jsonify({"error": str(exc)}), 404


@api.errorhandler(ValueError)
def _bad_request(exc: ValueError) -> tuple[Any, int]:
    """Map an ambiguous prefix or oversized request to a 400."""
    return jsonify({"error": str(exc)}), 400


@api.errorhandler(Exception)
def _unexpected(exc: Exception) -> tuple[Any, int]:
    """Report an unexpected failure as JSON, with the type named.

    A traceback in the terminal is useful to whoever is running the server; an
    HTML error page in a ``curl`` pipeline is not.
    """
    if isinstance(exc, HTTPException):
        raise exc
    current_app.logger.exception("Unhandled error in %s", request.path)
    return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500


@api.get("/overview")
def overview() -> Any:
    """Project summary and one card per sample."""
    return jsonify(data().overview())


@api.get("/samples/<sample_id>")
def sample(sample_id: str) -> Any:
    """One sample's prose, data register, models, and fits."""
    return jsonify(data().sample(sample_id))


@api.get("/samples/<sample_id>/curves")
def steady_curves(sample_id: str) -> Any:
    """Every steady-state measurement for a sample, as R(Q) curves."""
    return jsonify(data().steady_curves(sample_id))


@api.get("/samples/<sample_id>/series/<name>")
def series(sample_id: str, name: str) -> Any:
    """A time-resolved series as a time-Q fractional-residual map."""
    return jsonify(data().series_data(sample_id, name))


@api.get("/samples/<sample_id>/series/<name>/curves")
def series_curves(sample_id: str, name: str) -> Any:
    """A time-resolved series as one R(Q) curve per interval."""
    return jsonify(data().series_curves(sample_id, name))


@api.get("/samples/<sample_id>/assessments")
def assessments(sample_id: str) -> Any:
    """Assessment labels available for a sample."""
    return jsonify({"labels": data().assessment_labels(sample_id)})


@api.get("/samples/<sample_id>/assessments/<label>")
def assessment(sample_id: str, label: str) -> Any:
    """One tNR assessment, with its a(t) trajectory."""
    return jsonify(data().assessment(sample_id, label))


@api.get("/fits")
def fits() -> Any:
    """Fit records, newest first, optionally filtered by ``?sample=``."""
    return jsonify({"fits": data().fits(request.args.get("sample"))})


@api.get("/fits/<fit_id>")
def fit(fit_id: str) -> Any:
    """One fit: curves, profiles, parameters, and provenance."""
    return jsonify(data().fit(fit_id))


@api.get("/fits/<fit_id>/trajectory")
def trajectory(fit_id: str) -> Any:
    """Layer parameters against time, for a fit that includes a series."""
    return jsonify(data().trajectory(fit_id))
