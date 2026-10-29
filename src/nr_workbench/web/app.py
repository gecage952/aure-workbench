"""The Flask application: routing and nothing else.

Every view here resolves data through :class:`~nr_workbench.web.project.
ProjectData` and hands it to a template. There is no analysis in this module
and there should never be any -- if a view needs a number computed, the
computation belongs in ``ProjectData`` where it can be tested without a
request context.

The server is for ``localhost``. Every page and ``/api`` route only reads.
The one exception is the Experiment page's blueprint
(:mod:`nr_workbench.web.experiment_api`), whose writes are gated by
:mod:`nr_workbench.web.security`: loopback only, from a browser that opened
the one-time link ``nrw serve`` prints, and disabled entirely when bound
elsewhere or running under ``NRW_AGENT``.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any

from flask import (
    Flask,
    abort,
    make_response,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)

from nr_workbench.web import security, user_session
from nr_workbench.web.api import api
from nr_workbench.web.auth import OAUTH_PROVIDER, auth, init_oauth
from nr_workbench.web.curation import CurationData
from nr_workbench.web.experiment import ExperimentData
from nr_workbench.web.experiment_api import experiment_api
from nr_workbench.web.isaac import IsaacData
from nr_workbench.web.llm_settings import LlmSettingsData
from nr_workbench.web.models import ModelsData
from nr_workbench.web.project import ProjectData
from nr_workbench.web.settings import SettingsData

#: Settings never taken from the project's ``.env``: the identity provider and
#: its secret, and the directory whose groups a login is given.
PERSONAL_VARS = (
    "AURE_OAUTH_WELL_KNOWN_URL",
    "AURE_OAUTH_CLIENT_SECRET",
    "AURE_LDAP_URL",
    "AURE_LDAP_BASE_DN",
)


def create_app(
    root: Path,
    *,
    writable: bool = True,
    read_only_reason: str = "",
    bound_host: str = "127.0.0.1",
    token: str | None = None,
    autostart: bool = True,
) -> Flask:
    """Build the application for one project.

    Args:
        root: Project root, the directory holding ``nrw.toml``.
        writable: Whether the Experiment page may write at all. Also forced off
            when ``bound_host`` is not loopback.
        read_only_reason: What the page says when it may not.
        bound_host: The interface the server listens on.
        token: The secret behind the one-time link; generated if omitted.
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

    from nr_workbench.agent.guard import AGENT_ENV

    if os.environ.get(AGENT_ENV):
        # Here as well as in `nrw serve`: any other way of starting the app
        # (web.app.serve, a WSGI server) must be read-only for an agent too.
        writable = False
        read_only_reason = read_only_reason or (
            f"{AGENT_ENV} is set, so this server does not accept edits."
        )

    app = Flask(__name__)
    app.config["NRW_DATA"] = ProjectData(root)
    app.config["NRW_ROOT"] = root
    security.install(
        app,
        bound_host=bound_host,
        writable=writable,
        token=token or secrets.token_urlsafe(24),
    )
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

    # OAuth configuration for AuRE
    # authlib keeps the login's `state` in Flask's session, which cannot sign
    # anything without a key. It only has to outlive one round trip to the
    # identity provider, so a fresh key per process is enough -- restarting
    # the server abandons a half-finished login, nothing more.
    app.secret_key = secrets.token_hex(32)
    # Lax, not Strict: the identity provider's redirect back to /auth/callback
    # is a cross-site navigation, and must carry the cookie holding `state`.
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    # Where a logged-in user is kept between requests (web/user_session.py).
    user_session.install(app)
    # Read as an nrw command run in the project would -- the shell, then its
    # .env, then ~/.nrw and ~/.aure -- but without loading any of it into this
    # process, which would hand every job it starts the values as though the
    # shell had set them (see `where_set`).
    from nr_workbench.env import where_set

    oauth = where_set(root)
    # The provider and the secret come from the person's own settings only,
    # as ISAAC's portal and key do: the project's .env is written by anyone
    # who can write the project, and a provider named there would choose
    # where every login is sent.
    own = where_set(root, skip_project=True)
    app.config["NRW_PERSONAL_IGNORED"] = [
        name for name in PERSONAL_VARS if name in oauth and oauth[name] != own.get(name)
    ]
    app.config["AURE_OAUTH_WELL_KNOWN_URL"] = _setting(own, "AURE_OAUTH_WELL_KNOWN_URL")
    # authlib looks these up as f"{name}_{key}".upper(), so the prefix must be
    # upper-cased too: "aure_CLIENT_ID" is silently never read.
    prefix = OAUTH_PROVIDER.upper()
    app.config[f"{prefix}_CLIENT_ID"] = _setting(oauth, "AURE_OAUTH_CLIENT_ID")
    app.config[f"{prefix}_CLIENT_SECRET"] = _setting(own, "AURE_OAUTH_CLIENT_SECRET")
    app.config[f"{prefix}_CLIENT_KWARGS"] = {
        "scope": _setting(oauth, "AURE_OAUTH_SCOPES")
    }
    if app.config["AURE_OAUTH_WELL_KNOWN_URL"]:
        init_oauth(app)
    # The directory a logged-in user's groups are looked up in (web/ldap.py).
    # Personal too: a server named in the project's .env could put whoever it
    # liked in whichever group it liked.
    app.config["AURE_LDAP_URL"] = _setting(own, "AURE_LDAP_URL")
    app.config["AURE_LDAP_BASE_DN"] = _setting(own, "AURE_LDAP_BASE_DN")
    # Whether a logged-in user may change the project's settings without the
    # link `nrw serve` prints (security.refuse_unless_writer).
    app.config["USER_ALLOW_SETTINGS"] = _flag(_setting(oauth, "USER_ALLOW_SETTINGS"))

    app.register_blueprint(api)
    app.register_blueprint(experiment_api)
    app.register_blueprint(auth)

    @app.before_request
    def _only_the_experiment_blueprint_writes() -> None:
        # The second mechanism, beside the blueprint's own gate: an unsafe
        # method anywhere else is refused, so a write route added outside the
        # blueprint cannot slip past the gate by accident.
        if (
            request.method not in security.SAFE_METHODS
            and request.blueprint != "experiment_api"
        ):
            abort(405)

    app.jinja_env.filters["nrwjson"] = _compact_json
    app.jinja_env.filters["markdown"] = _render_markdown

    # A global rather than a per-route variable: the fit-story row is included
    # by two pages and would otherwise silently lose its meaning on whichever
    # route forgot to pass it -- an undefined name in Jinja is falsy, so every
    # change line would read as informative again with nothing to show for it.
    from nr_workbench.provenance.summary import FIRST_RUN

    app.jinja_env.globals["first_run"] = FIRST_RUN
    # Without a provider to send them to, a "Log in" link would only lead to
    # an error page, so the navbar offers it only when one is configured.
    app.jinja_env.globals["oauth_login"] = bool(app.config["AURE_OAUTH_WELL_KNOWN_URL"])

    _register_views(app)
    return app


def _setting(settings: dict[str, Any], name: str) -> str | None:
    """A variable's value from :func:`~nr_workbench.env.where_set`, if set."""
    setting = settings.get(name)
    return setting.value if setting else None


def _flag(value: str | None) -> bool:
    """A yes/no setting: ``1``, ``true``, ``yes`` or ``on``, in any case."""
    return (value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _inert(response: Any) -> Any:
    """A project file, served so that nothing in it runs as this server.

    Figures and results are the project's files, and anyone who can write the
    project can put an HTML or SVG file among them. Opened directly, a script
    in it would run with this server's origin: it could read a page's write
    token and send writes that pass every check the write gate makes. The
    sandbox renders it as an inert document; an image is unaffected.
    """
    response.headers["Content-Security-Policy"] = (
        "sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'"
    )
    return response


def _compact_json(value: Any) -> str:
    """Serialise for embedding inside a ``<script>`` block.

    ``</`` is escaped because the browser's HTML tokeniser finds ``</script>``
    inside a string literal and ends the block there -- a run label or a note
    containing that text would otherwise truncate the page's JavaScript and
    spill the rest as visible markup. ``json.dumps`` does not do this on its
    own, and Jinja's autoescaping does not apply inside a script tag.
    """
    text = json.dumps(value, separators=(",", ":"), default=str)
    # Every character that can change how the HTML parser reads a script
    # block, as a JSON escape. `</` alone is not enough: `<!--<script>` puts
    # the tokeniser into a state where the block's own `</script>` no longer
    # closes it, and the rest of the page becomes script.
    for char, escape in (
        ("<", "\\u003c"),
        (">", "\\u003e"),
        ("&", "\\u0026"),
        ("\u2028", "\\u2028"),
        ("\u2029", "\\u2029"),
    ):
        text = text.replace(char, escape)
    return text


def _render_markdown(text: str | None) -> Any:
    """Render markdown for the rail, marked safe for direct inclusion."""
    from markupsafe import Markup

    from nr_workbench.web.prose import render

    return Markup(render(text))  # noqa: S704 - render() disables raw HTML


def _register_views(app: Flask) -> None:
    """Attach the HTML routes."""

    def data() -> ProjectData:
        return app.config["NRW_DATA"]  # type: ignore[no-any-return]

    @app.get("/")
    def index() -> str:
        """Home: the way into the live monitor and into getting data."""
        return render_template("index.html")

    @app.get("/samples")
    def samples() -> str:
        """Project overview: one card per sample."""
        return render_template("samples.html", overview=data().overview())

    @app.get("/live-monitor")
    def live_monitor() -> str:
        """The live monitor (placeholder)."""
        return render_template("live_monitor.html")

    @app.get("/get-data")
    def get_data() -> str:
        """Getting data (placeholder)."""
        return render_template("get_data.html")

    @app.get("/profile")
    def profile() -> Any:
        """The logged-in user: their IPTS projects and every group they are in."""
        user = user_session.current_user()
        if user is None:
            if not user_session.login_required_enabled():
                abort(404, "Nobody can log in: no identity provider is configured.")
            return redirect(url_for("auth.login"))
        from ldap3.core.exceptions import LDAPException

        from nr_workbench.web import ldap

        # Looked up live rather than kept from the login: a user can be in
        # more groups than a session cookie holds, and this page is the one
        # place they are all wanted.
        groups: list[str] | None = None
        try:
            groups = sorted(ldap.posix_groups_for(user.user_id))
        except (RuntimeError, LDAPException) as exc:
            app.logger.error("Group lookup for %s failed: %s", user.user_id, exc)
        return render_template("profile.html", user=user, groups=groups)

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
        """A page that curates fits: the write token for a writer, and the
        policy that keeps any script but the page's own from reading it."""
        nonce = secrets.token_urlsafe(16)
        writer = security.can_write()
        page = render_template(
            template,
            page_token=app.config["NRW_PAGE_TOKEN"] if writer else "",
            writer=writer,
            csp_nonce=nonce,
            read_only_reason=app.config["NRW_READ_ONLY_REASON"],
            **context,
        )
        response = make_response(page)
        response.headers["Content-Security-Policy"] = security.page_csp(nonce)
        return response

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
        """Serve a figure from an assessment directory.

        ``send_from_directory`` rejects paths that escape the directory, which
        is what keeps a crafted ``filename`` from reading the rest of the disk.
        The sample and label are resolved through the layout rather than joined
        as strings for the same reason.
        """
        project = data()
        directory = (project.layout.sample(sample_id) / "assessments" / label).resolve()
        if not directory.is_dir():
            abort(404)
        try:
            directory.relative_to(project.root)
        except ValueError:
            abort(404)
        return _inert(send_from_directory(directory, filename))

    @app.get("/results/<sample_id>/<fit_id>/<path:filename>")
    def result_file(sample_id: str, fit_id: str, filename: str) -> Any:
        """Serve a file from a fit's immutable result directory."""
        project = data()
        directory = (project.layout.sample(sample_id) / "results" / fit_id).resolve()
        if not directory.is_dir():
            abort(404)
        try:
            directory.relative_to(project.root)
        except ValueError:
            abort(404)
        return _inert(send_from_directory(directory, filename))

    @app.get("/auth/<token>")
    def authorize(token: str) -> Any:
        """The one-time link `nrw serve` prints: grants this browser write access.

        Good once. The browser gets a fresh session value in an HttpOnly
        cookie -- not the link's secret -- and is redirected to a clean URL, so
        the secret does not linger in the address bar, leak in a Referer, or
        stay useful to anyone who later finds it in a log.
        """
        if not (
            app.config.get("NRW_WRITABLE") and security.is_loopback(request.remote_addr)
        ):
            abort(403, "This link is not valid for this server.")
        # Where to land, decided before the link is spent: a project nobody has
        # set up yet goes straight to Settings, and so does one whose nrw.toml
        # cannot be read -- which must not cost the only link there is.
        try:
            setup = app.config["NRW_SETTINGS"].needs_setup()
        except Exception:  # noqa: BLE001 - see above
            setup = True
        landing = "settings_page" if setup else "experiment"
        session = security.redeem_link(token)
        if session is None:
            if security.link_used():
                abort(
                    403,
                    "This link has already been used; it works once. Restart "
                    "`nrw serve` for a new one.",
                )
            abort(403, "This link is not valid for this server.")
        response = make_response(redirect(url_for(landing), code=303))
        response.set_cookie(
            security.COOKIE,
            session,
            httponly=True,
            samesite="Strict",
            path="/",
            max_age=security.SESSION_MAX_AGE,
        )
        return response

    @app.get("/experiment")
    def experiment() -> Any:
        """Every run of the experiment, organized into samples."""
        nonce = secrets.token_urlsafe(16)
        writer = security.can_write()
        page = render_template(
            "experiment.html",
            payload=app.config["NRW_EXPERIMENT"].overview(),
            page_token=app.config["NRW_PAGE_TOKEN"] if writer else "",
            writer=writer,
            csp_nonce=nonce,
        )
        response = make_response(page)
        response.headers["Content-Security-Policy"] = security.page_csp(nonce)
        return response

    @app.get("/settings")
    def settings_page() -> Any:
        """The IPTS, where the data is, and how new runs are noticed."""
        nonce = secrets.token_urlsafe(16)
        writer = security.can_write(for_settings=True)
        page = render_template(
            "settings.html",
            payload=app.config["NRW_SETTINGS"].settings(),
            page_token=app.config["NRW_PAGE_TOKEN"] if writer else "",
            writer=writer,
            csp_nonce=nonce,
        )
        response = make_response(page)
        response.headers["Content-Security-Policy"] = security.page_csp(
            nonce, plotly=False
        )
        return response

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
    if not security.is_loopback(bound_host):
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
    app = create_app(root, bound_host=host)
    app.run(host=host, port=port, debug=debug)
