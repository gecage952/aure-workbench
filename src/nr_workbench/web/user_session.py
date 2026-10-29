"""Who is logged in: the user record, and where it is kept between requests.

Everything else asks this module -- :func:`current_user`, :func:`log_in`,
:func:`log_out`, :func:`login_required` -- and never touches the storage
behind it. That storage is a :class:`UserStore`, chosen once per app by
:func:`install`. Today it is :class:`FlaskSessionStore`, which keeps the record
in Flask's signed session cookie; moving to a database means writing another
``UserStore`` and passing it to :func:`install`, with no caller changing.

Flask's session cookie is signed, not encrypted: the browser cannot alter the
record, but can read it. A user id and a list of IPTS numbers are fine to show
their own owner; anything secret must wait for a server-side store.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from functools import wraps
from typing import Any, Protocol

from flask import Flask, current_app, jsonify, session

#: Where :func:`install` files the store on the app.
_EXTENSION = "nrw_user_store"


@dataclass(frozen=True)
class SessionUser:
    """A logged-in user.

    Attributes:
        user_id: The identity provider's user id, without its ``@domain``.
        ipts_projects: The ``IPTS-*`` groups the directory lists them in.
        data_access: Whether the directory lists them in the ``data_access``
            group, which counts as membership of every IPTS.
    """

    user_id: str
    ipts_projects: list[str] = field(default_factory=list)
    data_access: bool = False


class UserStore(Protocol):
    """Keeps the logged-in user for the browser making the current request."""

    def load(self) -> SessionUser | None:
        """The user for this request, or ``None`` if nobody is logged in."""
        ...

    def save(self, user: SessionUser) -> None:
        """Remember *user* for this browser's later requests."""
        ...

    def clear(self) -> None:
        """Forget whoever this browser was logged in as."""
        ...


class FlaskSessionStore:
    """A :class:`UserStore` in Flask's signed session cookie.

    The cookie is signed with the app's ``secret_key``, which ``create_app``
    makes fresh each process: restarting the server logs everybody out.
    """

    #: The session key the record lives under.
    KEY = "nrw_user"

    def load(self) -> SessionUser | None:
        """The user for this request, or ``None`` if nobody is logged in."""
        record = session.get(self.KEY)
        if not isinstance(record, dict):
            return None
        try:
            return SessionUser(
                user_id=str(record["user_id"]),
                ipts_projects=[str(p) for p in record.get("ipts_projects", [])],
                # Absent from a login made before the field existed: no access.
                data_access=record.get("data_access") is True,
            )
        except (KeyError, TypeError):
            # A record from an older layout: treat it as logged out rather than
            # failing every request until the cookie expires.
            return None

    def save(self, user: SessionUser) -> None:
        """Remember *user* for this browser's later requests."""
        # A new session for a new login, so nothing set before it -- by
        # whoever had this browser, or planted in it -- carries over.
        session.clear()
        session[self.KEY] = asdict(user)

    def clear(self) -> None:
        """Forget whoever this browser was logged in as."""
        session.clear()


def install(app: Flask, store: UserStore | None = None) -> None:
    """Choose where *app* keeps its logged-in users.

    Args:
        app: The app.
        store: The storage; Flask's session cookie when not given.
    """
    app.extensions[_EXTENSION] = store if store is not None else FlaskSessionStore()


def _store() -> UserStore:
    return current_app.extensions[_EXTENSION]  # type: ignore[no-any-return]


def current_user() -> SessionUser | None:
    """The user logged in on this request's browser, if any."""
    return _store().load()


def log_in(user: SessionUser) -> None:
    """Log this request's browser in as *user*."""
    _store().save(user)


def log_out() -> None:
    """Log this request's browser out."""
    _store().clear()


def login_required_enabled() -> bool:
    """Whether logging in is required at all: only once a provider is set.

    Without an identity provider there is no way to log in, and ``nrw serve``
    stays the single-user local tool it has always been.
    """
    return bool(current_app.config.get("AURE_OAUTH_WELL_KNOWN_URL"))


def refuse_unless_logged_in() -> Any:
    """A 401 JSON response if login is required and nobody is logged in.

    Returns ``None`` when the request may go on, so it can be registered
    directly as a blueprint's ``before_request``.
    """
    if login_required_enabled() and current_user() is None:
        return jsonify({"error": "You must log in to use this API."}), 401
    return None


def login_required(view: Callable[..., Any]) -> Callable[..., Any]:
    """Restrict a single view to a logged-in user.

    For a whole blueprint, register :func:`refuse_unless_logged_in` as its
    ``before_request`` instead.
    """

    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        refused = refuse_unless_logged_in()
        if refused is not None:
            return refused
        return view(*args, **kwargs)

    return wrapper
