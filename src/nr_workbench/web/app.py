"""The Flask application: routing and nothing else.

Every view here resolves data through :class:`~nr_workbench.web.project.
ProjectData` and hands it to a template. There is no analysis in this module
and there should never be any -- if a view needs a number computed, the
computation belongs in ``ProjectData`` where it can be tested without a
request context.

The server is for ``localhost``. Every page and ``/api`` route only reads;
the Experiment page's blueprint (:mod:`nr_workbench.web.experiment_api`)
writes, unless the server is bound beyond loopback, which makes it read-only.
"""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from typing import Any

from flask import Flask, abort, render_template, send_from_directory

from nr_workbench.web.api import api
from nr_workbench.web.curation import CurationData
from nr_workbench.web.experiment import ExperimentData
from nr_workbench.web.experiment_api import experiment_api
from nr_workbench.web.isaac import IsaacData
from nr_workbench.web.llm_settings import LlmSettingsData
from nr_workbench.web.models import ModelsData
from nr_workbench.web.project import ProjectData
from nr_workbench.web.settings import SettingsData


def create_app(
    root: Path,
    *,
    writable: bool = True,
    read_only_reason: str = "",
    bound_host: str = "127.0.0.1",
    autostart: bool = True,
) -> Flask:
    """Build the application for one project.

    Args:
        root: Project root, the directory holding ``nrw.toml``.
        writable: Whether the Experiment page may write at all. Also forced off
            when ``bound_host`` is not loopback.
        read_only_reason: What the page says when it may not.
        bound_host: The interface the server listens on.
        autostart: Start polling the data source on first use.

    Returns:
        A configured Flask application.

    Raises:
        FileNotFoundError: If ``root`` is not a workbench project.
    """
    root = Path(root).resolve()
    if not (root / "nrw.toml").is_file():
        raise FileNotFoundError(
            f"{root} is not a workbench project (no nrw.toml). "
            "Run `nrw init` there first, or pass --root."
        )

    app = Flask(__name__)
    app.config["NRW_DATA"] = ProjectData(root)
    app.config["NRW_ROOT"] = root
    app.config["NRW_WRITABLE"] = writable and is_loopback(bound_host)
    app.config["NRW_READ_ONLY_REASON"] = read_only_reason or (
        "" if app.config["NRW_WRITABLE"] else _default_reason(bound_host)
    )
    experiment = ExperimentData(
        root,
        writable=app.config["NRW_WRITABLE"],
        why_read_only=app.config["NRW_READ_ONLY_REASON"],
        autostart=autostart,
    )
    app.config["NRW_EXPERIMENT"] = experiment
    app.config["NRW_MODELS"] = ModelsData(
        root,
        writable=app.config["NRW_WRITABLE"],
        why_read_only=app.config["NRW_READ_ONLY_REASON"],
    )
    app.config["NRW_ISAAC"] = IsaacData(
        root,
        writable=app.config["NRW_WRITABLE"],
        why_read_only=app.config["NRW_READ_ONLY_REASON"],
        # The page's one lane: an export is not run beside a fit.
        jobs=app.config["NRW_MODELS"].jobs,
    )
    app.config["NRW_CURATION"] = CurationData(
        root,
        writable=app.config["NRW_WRITABLE"],
        why_read_only=app.config["NRW_READ_ONLY_REASON"],
    )
    app.config["NRW_LLM"] = LlmSettingsData(
        root,
        writable=app.config["NRW_WRITABLE"],
        why_read_only=app.config["NRW_READ_ONLY_REASON"],
    )
    app.config["NRW_SETTINGS"] = SettingsData(
        root,
        writable=app.config["NRW_WRITABLE"],
        why_read_only=app.config["NRW_READ_ONLY_REASON"],
        count_runs=experiment.catalogued_runs,
        watching=experiment.watching,
        on_saved=experiment.reload,
    )
    app.register_blueprint(api)
    app.register_blueprint(experiment_api)

    app.jinja_env.filters["markdown"] = _render_markdown

    # A global rather than a per-route variable: the fit-story row is included
    # by two pages and would otherwise silently lose its meaning on whichever
    # route forgot to pass it -- an undefined name in Jinja is falsy, so every
    # change line would read as informative again with nothing to show for it.
    from nr_workbench.provenance.summary import FIRST_RUN

    app.jinja_env.globals["first_run"] = FIRST_RUN

    _register_views(app)
    return app


def is_loopback(address: str | None) -> bool:
    """Whether an address is this machine's loopback, including IPv4-mapped."""
    if not address:
        return False
    try:
        parsed = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return address == "localhost"
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    return parsed.is_loopback


def _render_markdown(text: str | None) -> Any:
    """Render markdown for the rail, marked safe for direct inclusion."""
    from markupsafe import Markup

    from nr_workbench.web.prose import render

    return Markup(render(text))  # noqa: S704 - the project's own notes, as written


def _register_views(app: Flask) -> None:
    """Attach the HTML routes."""

    def data() -> ProjectData:
        return app.config["NRW_DATA"]  # type: ignore[no-any-return]

    @app.get("/")
    def index() -> str:
        """Project overview: one card per sample."""
        return render_template("index.html", overview=data().overview())

    @app.get("/s/<sample_id>")
    def sample(sample_id: str) -> str:
        """The sample landscape -- every measurement on one page."""
        project = data()
        try:
            detail = project.sample(sample_id)
        except FileNotFoundError:
            abort(404, f"No sample {sample_id!r} in this project.")

        curves = project.steady_curves(sample_id)
        problems = list(detail["problems"]) + list(curves["problems"])

        # The first plottable series and the first assessment are loaded
        # eagerly so the page is useful without a click. Anything further is
        # fetched from the API on demand.
        series_payload = None
        plottable = [s for s in detail["series"] if s.get("plottable")]
        if plottable:
            name = plottable[0]["name"]
            try:
                series_payload = project.series_data(sample_id, name)
                problems += series_payload.get("problems", [])
            except (FileNotFoundError, ValueError) as exc:
                problems.append({"scope": f"series:{name}", "message": str(exc)})

        assessment = None
        labels = detail["assessments"]
        if labels:
            try:
                assessment = project.assessment(sample_id, labels[0])
                problems += assessment.get("problems", [])
            except (FileNotFoundError, ValueError) as exc:
                problems.append(
                    {"scope": f"assessment:{labels[0]}", "message": str(exc)}
                )

        return render_template(
            "sample.html",
            sample=detail,
            curves=curves["curves"],
            series=series_payload,
            assessment=assessment,
            problems=problems,
        )

    def curating(template: str, **context: Any) -> Any:
        """A page that curates fits: its controls are offered to a writer."""
        return render_template(
            template,
            writer=app.config["NRW_WRITABLE"],
            read_only_reason=app.config["NRW_READ_ONLY_REASON"],
            **context,
        )

    @app.get("/f/<fit_id>")
    def fit(fit_id: str) -> Any:
        """One fit in detail, with its provenance, and what was said about it."""
        try:
            detail = data().fit(fit_id)
        except FileNotFoundError as exc:
            abort(404, str(exc))
        except ValueError as exc:
            abort(400, str(exc))
        trajectory = data().trajectory(fit_id)
        said = app.config["NRW_CURATION"].state(detail["fit_id"])
        return curating("fit.html", fit=detail, trajectory=trajectory, said=said)

    @app.get("/fits")
    def fits() -> Any:
        """Every fit in the project, newest first."""
        return curating("fits.html", fits=data().fits(), overview=data().overview())

    @app.get("/figures/<sample_id>/<label>/<path:filename>")
    def figure(sample_id: str, label: str, filename: str) -> Any:
        """Serve a figure from an assessment directory."""
        directory = data().layout.sample(sample_id) / "assessments" / label
        if not directory.is_dir():
            abort(404)
        return send_from_directory(directory, filename)

    @app.get("/results/<sample_id>/<fit_id>/<path:filename>")
    def result_file(sample_id: str, fit_id: str, filename: str) -> Any:
        """Serve a file from a fit's immutable result directory."""
        directory = data().layout.sample(sample_id) / "results" / fit_id
        if not directory.is_dir():
            abort(404)
        return send_from_directory(directory, filename)

    @app.get("/experiment")
    def experiment() -> Any:
        """Every run of the experiment, organized into samples."""
        return render_template(
            "experiment.html", payload=app.config["NRW_EXPERIMENT"].overview()
        )

    @app.get("/settings")
    def settings_page() -> Any:
        """The IPTS, where the data is, and how new runs are noticed."""
        return render_template(
            "settings.html", payload=app.config["NRW_SETTINGS"].settings()
        )

    @app.errorhandler(403)
    def forbidden(error: Any) -> tuple[str, int]:
        """Render a 403 in the site's own layout."""
        return render_template("error.html", code=403, message=error.description), 403

    @app.errorhandler(404)
    def not_found(error: Any) -> tuple[str, int]:
        """Render a 404 in the site's own layout."""
        return render_template("error.html", code=404, message=error.description), 404

    @app.errorhandler(400)
    def bad_request(error: Any) -> tuple[str, int]:
        """Render a 400 in the site's own layout."""
        return render_template("error.html", code=400, message=error.description), 400


def _default_reason(bound_host: str) -> str:
    if not is_loopback(bound_host):
        return (
            f"The server is bound to {bound_host}, not loopback, so it only "
            "shows the experiment. Run `nrw serve` without --host to edit it."
        )
    return "This server was started read-only."


def serve(
    root: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    debug: bool = False,
) -> None:
    """Run the development server.

    Args:
        root: Project root.
        host: Interface to bind. Defaults to loopback.
        port: Port to listen on.
        debug: Enable Flask's reloader and debugger.

    Raises:
        FileNotFoundError: If ``root`` is not a workbench project.
    """
    run_app(create_app(root, bound_host=host), host=host, port=port, debug=debug)


def run_app(app: Flask, *, host: str, port: int, debug: bool) -> None:
    """Serve *app* without Flask's own loading of ``.env`` files.

    ``Flask.run`` loads the nearest ``.env`` and ``.flaskenv`` into this
    process's environment, where a value would pass to every job the server
    starts and win over the project's ``.env`` there. ``FLASK_SKIP_DOTENV`` is
    set as well, because Flask lets it say "load" over ``load_dotenv=False``.

    Args:
        app: The application.
        host: Interface to bind.
        port: Port to listen on.
        debug: Enable Flask's reloader and debugger.
    """
    os.environ["FLASK_SKIP_DOTENV"] = "1"
    app.run(host=host, port=port, debug=debug, load_dotenv=False)
