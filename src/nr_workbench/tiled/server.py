"""The person's Tiled server, and a client of it that asks no one to log in.

The server and its key are the person's: ``TILED_URL`` and ``TILED_API_KEY``
from the shell, ``~/.nrw`` or ``~/.aure``, never a project's ``.env``, which
anyone who can commit to the project writes and which would then choose where
the person's login goes (2026-10-21, the same rule as ``ISAAC_URL``). The
official client also reads several ``TILED_*`` variables itself -- the key,
where it keeps its tokens, whether to log them -- so each one nrw's own
``.env`` loading put in the environment is taken out before it starts.

Only ``nrw tiled login`` starts a login: Tiled's device-code flow, which
asks at the terminal which login when the server offers several
(tiled.ornl.gov offers ``globus`` and ``password``), prints Globus's address
and a code, and waits. The client keeps the login in its own token cache, and
renews it; every other command uses what it kept, and says to log in when
there is nothing.
"""

from __future__ import annotations

import os
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

#: The server nrw uses when the person names none: ORNL's.
DEFAULT_URL = "https://tiled.ornl.gov"

#: Where an experiment's own node is on it, by the experiment's IPTS. Another
#: process makes the node, and its ``autoreduction/``.
EXPERIMENT_PATH = "projects/isaac/{ipts}"

#: How the client is installed. Not ``pip install 'nr-workbench[tiled]'``:
#: nr-workbench is not on PyPI, so that installs nothing, or someone else's.
INSTALL = (
    "re-run the nr-workbench installer with NRW_EXTRAS=tiled (docs/install.md), "
    "or `pip install -e '.[tiled]'` in a clone"
)


class TiledSyncError(Exception):
    """A Tiled command could not do what it was asked; the message says why."""


@dataclass(frozen=True)
class Server:
    """The server a command would use, and the key it would send.

    Attributes:
        url: The server's address, with no trailing ``/``.
        source: Where it was set, as a person reads it: ``~/.nrw``, or
            "nrw's default".
        key: ``TILED_API_KEY`` as the person set it, sent instead of a login;
            ``None`` when unset.
        key_source: Where the key was set.
    """

    url: str
    source: str
    key: str | None = None
    key_source: str = ""


def server(root: Path | None = None) -> Server:
    """The person's server and key, from the shell, ``~/.nrw`` or ``~/.aure``.

    Args:
        root: The project, whose own ``.env`` is never read for either.

    Raises:
        TiledSyncError: ``TILED_URL`` is not an address nrw sends a login to.
    """
    from nr_workbench.env import shown_source, where_set

    settings = where_set(root, skip_project=True)
    named = settings.get("TILED_URL")
    if named is not None and named.value.strip():
        url = named.value.strip().rstrip("/")
        source = shown_source(named.source, root)
    else:
        url, source = DEFAULT_URL, "nrw's default"
    problem = _url_problem(url)
    if problem is not None:
        # The address itself is not repeated: a key pasted into it would be.
        raise TiledSyncError(f"TILED_URL, set in {source}, {problem}")
    key = settings.get("TILED_API_KEY")
    if key is not None and key.value:
        return Server(url, source, key.value, shown_source(key.source, root))
    return Server(url, source)


def _url_problem(url: str) -> str | None:
    """Why *url* is no server nrw sends a login to; ``None`` when it is one.

    A user name, a query or a fragment is refused rather than printed back: a
    key pasted into the address would be. A path is refused because the
    client would take it for the place everything else is under.
    """
    parts = urllib.parse.urlsplit(url)
    local = parts.hostname in ("localhost", "127.0.0.1", "::1")
    if parts.scheme != "https" and not (
        parts.scheme == "http" and local and ALLOW_PLAIN_HTTP
    ):
        return "is not an https:// address."
    if not parts.hostname:
        return "names no server."
    if parts.username or parts.password or parts.query or parts.fragment:
        return "holds a user name, a query or a fragment: give the server alone."
    if parts.path not in ("", "/"):
        return f"has a path: give the server alone, such as {DEFAULT_URL}."
    return None


#: Plain http, to a server on this machine: for the contract tests' own
#: server alone. Another account on the machine could answer on that port.
ALLOW_PLAIN_HTTP = False

#: What else the client, or the network under it, reads from the environment:
#: where to send the requests, which certificates to trust, where to write
#: the TLS keys.
_NETWORK = (
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "SSLKEYLOGFILE",
)


def _scrub_environment() -> None:
    """Take out what nrw's ``.env`` loading put in the environment that the
    client reads itself: every ``TILED_*`` variable, and the network's. Those
    files are not the person's alone; what the person's shell set stays."""
    from nr_workbench.env import loaded_from_files

    for name in loaded_from_files():
        if name.startswith("TILED_") or name.upper() in _NETWORK:
            os.environ.pop(name, None)


class Library(NamedTuple):
    """The parts of the official client nrw uses: the one place it is loaded."""

    Context: Any
    from_context: Any
    Spec: Any
    CannotRefresh: type[Exception]


def library() -> Library:
    """The official client's parts, imported now, once the environment holds
    nothing of a project's for it: it reads some of it as it is imported.

    Raises:
        TiledSyncError: It is not installed.
    """
    _scrub_environment()
    try:
        from tiled.client.auth import CannotRefreshAuthentication
        from tiled.client.constructors import from_context
        from tiled.client.context import Context
        from tiled.structures.core import Spec
    except ImportError as exc:
        raise TiledSyncError(
            f"The Tiled client is not installed here: {INSTALL}."
        ) from exc
    return Library(Context, from_context, Spec, CannotRefreshAuthentication)


@contextmanager
def talking(url: str, tiled: Library) -> Iterator[None]:
    """Every way the client, *tiled*, fails while it talks to *url*, said as
    nrw's: the one place they are caught, for every command.

    Raises:
        TiledSyncError: Instead of the client's own error.
    """
    import httpx

    try:
        yield
    except TiledSyncError:
        raise
    except httpx.HTTPError as exc:
        raise TiledSyncError(f"{url} did not answer as asked: {said(exc)}") from None
    except tiled.CannotRefresh:
        raise TiledSyncError(
            f"The login for {url} has run out: run `nrw tiled login`."
        ) from None


def connect(root: Path | None = None, *, login: bool = False) -> Any:
    """A client of the person's server, at its root, acting as the person.

    With the key when one is set; otherwise with the login the client keeps.
    A login is started only with *login*.

    Raises:
        TiledSyncError: Not logged in, the server refused or did not answer,
            or the client is not installed.
    """
    chosen = server(root)
    tiled = library()
    with talking(chosen.url, tiled):
        try:
            context, path = tiled.Context.from_any_uri(chosen.url, api_key=chosen.key)
            providers = context.server_info.authentication.providers
            if chosen.key is None and providers and not context.use_cached_tokens():
                if not login:
                    raise TiledSyncError(
                        f"Not logged in to {chosen.url}: run `nrw tiled login`."
                    )
                context.authenticate(remember_me=True)
            return tiled.from_context(context, node_path_parts=path)
        except RuntimeError as exc:
            # The client's own, on connecting: a server that takes keys
            # alone, or a key and a login both.
            raise TiledSyncError(f"{chosen.url}: {said(exc)}") from None


def logout(root: Path | None = None) -> str:
    """End the login the client keeps for the person's server.

    Returns:
        What was done, for a person.

    Raises:
        TiledSyncError: The server could not be asked.
    """
    chosen = server(root)
    if chosen.key is not None:
        return (
            f"TILED_API_KEY, set in {chosen.key_source}, is what nrw sends to "
            f"{chosen.url}: there is no login to end. Remove the key to stop."
        )
    tiled = library()
    with talking(chosen.url, tiled):
        context, _ = tiled.Context.from_any_uri(chosen.url)
        if not context.use_cached_tokens():
            return f"Not logged in to {chosen.url}: nothing to end."
        context.logout()
    return (
        f"Logged out of {chosen.url}: the session is ended there, and forgotten here."
    )


def whoami(client: Any) -> str:
    """Whose the server takes the login to be, as ``id (provider)``."""
    context = client.context
    if not context.server_info.authentication.links:
        # A server with only a key, as a test's is, names no one.
        return "no one named: the server takes keys alone"
    principal = context.whoami() or {}
    identities = [
        f"{identity.get('id')} ({identity.get('provider')})"
        for identity in principal.get("identities") or ()
        if isinstance(identity, dict)
    ]
    return ", ".join(said(text) for text in identities) or said(
        str(principal.get("type") or "no one named")
    )


def experiment_node(client: Any, ipts: str) -> Any | None:
    """The experiment's own node; ``None`` when it is not there, or not
    shared with this login: the server says the same for both."""
    try:
        return client[EXPERIMENT_PATH.format(ipts=ipts)]
    except KeyError:
        return None


def tags_of(node: Any) -> list[str]:
    """The access tags *node* has: what everything nrw makes in it is given."""
    try:
        tags = node.access_blob.get("tags")
    except AttributeError:
        return []
    return [tag for tag in tags or () if isinstance(tag, str)]


def said(value: Any) -> str:
    """A server's text, safe to print: escaped where it holds what a terminal
    would act on, or what no one would see."""
    from nr_workbench.problems import one_line

    return one_line(str(value))
