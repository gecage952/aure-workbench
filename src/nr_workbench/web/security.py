"""Who may write through ``nrw serve``, and how that is checked.

Until the experiment page, the server only read. It had no authentication and
needed none beyond binding to loopback. Writes change that, and the threat is
not only a hostile website:

* **Other accounts on the same machine.** An SNS analysis node is shared, and
  so is its loopback interface: anyone logged in can reach ``127.0.0.1:8765``.
  A token embedded in the page would not help -- they can fetch the page too.
  So writes need a secret that is *not* on any page: ``nrw serve`` prints a
  one-time link (as Jupyter does); opening it sets an HttpOnly cookie, and
  only a browser holding that cookie can write.
* **Other websites in the scientist's browser.** A cross-site ``fetch`` with a
  JSON body needs a CORS preflight this server never grants, and a form cannot
  send JSON -- so writes must be ``application/json``, and a request carrying
  an ``Origin`` other than this server's own (port included: every localhost
  app is "same-site" to a browser) is refused.
* **DNS rebinding.** A hostile name that resolves to 127.0.0.1 makes its page
  same-origin with this server. The ``Host`` header still names the hostile
  name, so it is checked on *every* request, reads included.
* **Clickjacking.** A framed page carries the cookie and a correct Origin, so
  every HTML response forbids framing.

Reads stay open as they always were on loopback -- the existing pages are
unchanged, and ``curl /api/...`` still works for an assistant. Writes are
disabled outright when the server is bound to a non-loopback address, and when
it runs under ``NRW_AGENT``.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import secrets
import threading
from typing import Any

from flask import Flask, abort, current_app, request

#: The cookie a browser gets from the one-time link.
COOKIE = "nrw_session"

#: The header the page sends back with every write: a second, per-process
#: value that is only rendered into pages served to a cookie holder.
TOKEN_HEADER = "X-NRW-Token"

#: Largest write request accepted. Catalog edits are a few kilobytes.
MAX_WRITE_BYTES = 1024 * 1024

#: How long a browser keeps its session cookie. The session itself lives only
#: as long as the server process, so this only decides whether reopening the
#: browser needs the link again.
SESSION_MAX_AGE = 7 * 24 * 3600


class _RedactLink(logging.Filter):
    """Keep the one-time link out of the server's access log.

    Werkzeug logs every request line, ``GET /auth/<secret>`` included, to
    stderr -- which people redirect to files, into tmux logs, into CI
    captures. On a shared machine any of those can be readable by another
    account.
    """

    _PATTERN = re.compile(r"/auth/[^\s\"?]+")

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "/auth/" in message:
            record.msg = self._PATTERN.sub("/auth/[redacted]", message)
            record.args = ()
        return True


_REDACTOR = _RedactLink()

#: Host names that mean "this machine" when the server is bound to loopback.
_LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})

#: Methods that change nothing, and so need no write access.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def page_csp(nonce: str, *, plotly: bool = True) -> str:
    """The Content-Security-Policy for a page that runs the app's own scripts.

    Scripts only from this server, the page's nonce, and -- for a page that
    plots -- Plotly's CDN, with ``'unsafe-eval'`` because its WebGL traces
    (scattergl, which the reflectivity panel uses) compile their shaders
    through regl, which builds functions at run time. That admits no injected
    ``<script>`` tag and no inline handler: the nonce still stops both. A page
    that does not plot gets neither.

    Args:
        nonce: This response's nonce, also set on its inline scripts.
        plotly: Whether the page loads Plotly.
    """
    scripts = f"'self' 'nonce-{nonce}'"
    if plotly:
        scripts += " 'unsafe-eval' https://cdn.plot.ly"
    return (
        f"default-src 'self'; script-src {scripts}; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "img-src 'self' data:; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )


def is_loopback(address: str | None) -> bool:
    """Whether an address is this machine's loopback, including IPv4-mapped."""
    if not address:
        return False
    try:
        parsed = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return address in ("localhost",)
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    return parsed.is_loopback


def _hostname(host_header: str) -> str:
    """``localhost:8765`` -> ``localhost``; ``[::1]:8765`` -> ``::1``."""
    host = host_header.strip()
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    if host.count(":") == 1:
        return host.split(":", 1)[0]
    return host


def install(app: Flask, *, bound_host: str, writable: bool, token: str) -> None:
    """Attach the host check, the write gate and the response headers.

    Args:
        app: The application.
        bound_host: The interface the server listens on.
        writable: Whether writes are allowed at all.
        token: The per-process secret behind the one-time link and the cookie.
    """
    loopback_bind = is_loopback(bound_host)
    app.config.update(
        NRW_BOUND_HOST=bound_host,
        NRW_LOOPBACK_BIND=loopback_bind,
        NRW_WRITABLE=writable and loopback_bind,
        NRW_TOKEN=token,
        # Page token: a different value, so the one in the page is not the
        # one in the cookie.
        NRW_PAGE_TOKEN=secrets.token_urlsafe(24),
        MAX_CONTENT_LENGTH=MAX_WRITE_BYTES,
    )
    app.extensions["nrw_sessions"] = _Sessions()
    werkzeug = logging.getLogger("werkzeug")
    if _REDACTOR not in werkzeug.filters:
        werkzeug.addFilter(_REDACTOR)

    @app.before_request
    def _check_host() -> None:
        if not current_app.config["NRW_LOOPBACK_BIND"]:
            return  # bound to a real interface: names cannot be enumerated
        name = _hostname(request.host or "")
        if name == "0.0.0.0" or name not in _LOOPBACK_NAMES:
            abort(
                400,
                f"Host {request.host!r} is not this machine's loopback name. "
                "Open the page as http://127.0.0.1 or http://localhost.",
            )

    @app.after_request
    def _headers(response: Any) -> Any:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if response.mimetype == "text/html":
            response.headers.setdefault("X-Frame-Options", "DENY")
            response.headers.setdefault(
                "Content-Security-Policy", "frame-ancestors 'none'"
            )
        return response


class _Sessions:
    """The one-time link, and the sessions it has granted, for one server.

    The link's secret is printed to a terminal and travels in a URL, so it is
    exactly the kind of value that ends up somewhere it should not. It is
    therefore good once: redeeming it mints a fresh, unrelated session value
    for that browser's cookie, and a copy of the link found later -- in a
    log, in shell history, in scrollback -- opens nothing.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._used = False
        self._sessions: set[str] = set()

    def redeem(self, presented: str, expected: str) -> str | None:
        """A new session value for a correct, unused link; ``None`` otherwise."""
        with self._lock:
            if self._used or not expected:
                return None
            if not secrets.compare_digest(presented, expected):
                return None
            self._used = True
            session = secrets.token_urlsafe(32)
            self._sessions.add(session)
            return session

    @property
    def used(self) -> bool:
        with self._lock:
            return self._used

    def valid(self, presented: str) -> bool:
        with self._lock:
            return any(secrets.compare_digest(presented, s) for s in self._sessions)


def redeem_link(presented: str) -> str | None:
    """Exchange the one-time link's secret for a session value, once."""
    sessions: _Sessions = current_app.extensions["nrw_sessions"]
    return sessions.redeem(presented, current_app.config.get("NRW_TOKEN", ""))


def link_used() -> bool:
    """Whether the one-time link has already been opened."""
    return current_app.extensions["nrw_sessions"].used


def has_session() -> bool:
    """Whether this request comes from a browser that opened the one-time link."""
    presented = request.cookies.get(COOKIE, "")
    return bool(presented) and current_app.extensions["nrw_sessions"].valid(presented)


def user_may_change_settings() -> bool:
    """Whether a logged-in user stands in for the link, for settings only.

    ``USER_ALLOW_SETTINGS`` opts in; without it, settings need the link like
    every other write. Even then, only a member of the project's own IPTS
    qualifies -- or of the ``data_access`` group, which counts as membership
    of every IPTS. A project with no IPTS has no members: setting one up still
    takes the link.
    """
    if not current_app.config.get("USER_ALLOW_SETTINGS"):
        return False
    from nr_workbench.web.user_session import current_user

    user = current_user()
    if user is None:
        return False
    from nr_workbench.experiment.config import experiment_config_for
    from nr_workbench.project.experiment_schema import normalize_ipts

    # Read on every request, not once at startup: saving the settings can
    # change the IPTS, and the next request must be judged against the new one.
    ipts = experiment_config_for(current_app.config["NRW_ROOT"]).ipts
    if ipts is None:
        return False
    return user.data_access or ipts in {
        normalize_ipts(project) for project in user.ipts_projects
    }


def _authorized(for_settings: bool) -> bool:
    return has_session() or (for_settings and user_may_change_settings())


def can_write(*, for_settings: bool = False) -> bool:
    """Whether this request may be shown write controls.

    Args:
        for_settings: The controls change only the project's settings, which
            a logged-in user may when ``USER_ALLOW_SETTINGS`` is set.
    """
    return (
        bool(current_app.config.get("NRW_WRITABLE"))
        and is_loopback(request.remote_addr)
        and _authorized(for_settings)
    )


def refuse_unless_writer(*, for_settings: bool = False) -> None:
    """The write gate: abort unless every condition for a write holds.

    Called before every request that is not a safe method on the write
    blueprint, and -- as a second mechanism -- application-wide for any
    unsafe method on any other route.

    Args:
        for_settings: The request changes only the project's settings. A
            logged-in user may then stand in for the link when
            ``USER_ALLOW_SETTINGS`` is set; every other check still applies.
    """
    if request.method in SAFE_METHODS:
        return
    config = current_app.config
    if not config.get("NRW_WRITABLE"):
        abort(403, config.get("NRW_READ_ONLY_REASON") or "This server is read-only.")
    if not is_loopback(request.remote_addr):
        abort(403, "Writes are accepted only from this machine.")
    if not _authorized(for_settings):
        abort(
            403,
            "This browser has not opened the link `nrw serve` printed, so it "
            "cannot make changes. Open that link, then reload the page.",
        )
    presented = request.headers.get(TOKEN_HEADER, "")
    if not secrets.compare_digest(presented, config.get("NRW_PAGE_TOKEN", "")):
        abort(403, "The page's write token is missing or stale; reload the page.")
    origin = request.headers.get("Origin")
    if origin is not None and origin != f"{request.scheme}://{request.host}":
        abort(403, f"A request from {origin} may not write here.")
    fetch_site = request.headers.get("Sec-Fetch-Site")
    if fetch_site is not None and fetch_site != "same-origin":
        abort(403, "Only this page may write here.")
    if request.mimetype != "application/json":
        abort(415, "Writes must send a JSON body (Content-Type: application/json).")
