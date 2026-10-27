"""The person's Tiled server, and a client of it that asks no one to log in.

The server and its key are the person's: ``TILED_URL`` and ``TILED_API_KEY``
from the shell, ``~/.nrw`` or ``~/.aure``, never a project's ``.env``, which
anyone who can commit to the project writes and which would then choose where
the person's login goes (2026-10-21, the same rule as ``ISAAC_URL``). The
official client also reads several ``TILED_*`` variables itself -- the key,
where it keeps its tokens, whether to log them -- so each one nrw's own
``.env`` loading put in the environment is taken out before it starts.

Only ``nrw tiled login`` starts a login: Tiled's device-code flow, which
prints Globus's address and a code, and waits. The client keeps the login in
its own token cache, and renews it; every other command uses what it kept,
and says to log in when there is nothing.
"""

from __future__ import annotations

import os
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

#: The server nrw uses when the person names none: ORNL's.
DEFAULT_URL = "https://tiled.ornl.gov"

#: Where an experiment's own node is on it, by the experiment's IPTS. Another
#: process makes the node, and its ``autoreduction/``.
EXPERIMENT_PATH = "projects/isaac/{ipts}"


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
    """Why *url* is no server nrw sends a login to; ``None`` when it is one."""
    parts = urllib.parse.urlsplit(url)
    local = parts.hostname in ("localhost", "127.0.0.1", "::1")
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        return "is not an https:// address: plain http only to this machine."
    if not parts.hostname:
        return "names no server."
    if parts.username or parts.password or parts.query or parts.fragment:
        return "holds a user name, a query or a fragment: give the server alone."
    if parts.path not in ("", "/"):
        return f"has a path: give the server alone, such as {DEFAULT_URL}."
    return None


def _scrub_environment() -> None:
    """Take out every ``TILED_*`` variable nrw's ``.env`` loading put in the
    environment: the client reads several itself, and those files are not the
    person's alone. What the person's shell set stays."""
    from nr_workbench.env import loaded_from_files

    for name in loaded_from_files():
        if name.startswith("TILED_"):
            os.environ.pop(name, None)


class Library(NamedTuple):
    """The parts of the official client nrw uses: the one place it is loaded."""

    Context: Any
    from_context: Any
    Spec: Any


def library() -> Library:
    """The official client's parts, imported now.

    Raises:
        TiledSyncError: It is not installed.
    """
    try:
        from tiled.client.constructors import from_context
        from tiled.client.context import Context
        from tiled.structures.core import Spec
    except ImportError as exc:
        raise TiledSyncError(
            "The Tiled client is not installed here: pip install 'nr-workbench[tiled]'."
        ) from exc
    return Library(Context, from_context, Spec)


def connect(
    root: Path | None = None, *, login: bool = False, include_data_sources: bool = False
) -> Any:
    """A client of the person's server, at its root, acting as the person.

    With the key when one is set; otherwise with the login the client keeps.
    A login is started only with *login*. With *include_data_sources*, each
    node read says what files it is, and their sizes.

    Raises:
        TiledSyncError: Not logged in, the server refused or did not answer,
            or the client is not installed.
    """
    import httpx

    chosen = server(root)
    tiled = library()
    _scrub_environment()
    try:
        context, path = tiled.Context.from_any_uri(chosen.url, api_key=chosen.key)
        providers = context.server_info.authentication.providers
        if chosen.key is None and providers and not context.use_cached_tokens():
            if not login:
                raise TiledSyncError(
                    f"Not logged in to {chosen.url}: run `nrw tiled login`."
                )
            context.authenticate(remember_me=True)
        return tiled.from_context(
            context, node_path_parts=path, include_data_sources=include_data_sources
        )
    except TiledSyncError:
        raise
    except RuntimeError as exc:
        # The client's own: a server that takes keys alone, or a key and a
        # login both.
        raise TiledSyncError(f"{chosen.url}: {said(exc)}") from None
    except httpx.HTTPError as exc:
        raise TiledSyncError(
            f"{chosen.url} did not answer as asked: {said(exc)}"
        ) from None


def logout(root: Path | None = None) -> str:
    """End the login the client keeps for the person's server.

    Returns:
        What was done, for a person.

    Raises:
        TiledSyncError: The server could not be asked.
    """
    import httpx

    chosen = server(root)
    if chosen.key is not None:
        return (
            f"TILED_API_KEY, set in {chosen.key_source}, is what nrw sends to "
            f"{chosen.url}: there is no login to end. Remove the key to stop."
        )
    tiled = library()
    _scrub_environment()
    try:
        context, _ = tiled.Context.from_any_uri(chosen.url)
        if not context.use_cached_tokens():
            return f"Not logged in to {chosen.url}: nothing to end."
        context.logout()
    except httpx.HTTPError as exc:
        raise TiledSyncError(
            f"{chosen.url} did not answer as asked: {said(exc)}"
        ) from None
    return (
        f"Logged out of {chosen.url}: the session is ended there, and forgotten here."
    )


def kept_login(url: str) -> bool:
    """Whether the Tiled client keeps a login for the server at *url*, by its
    files alone: nothing is sent. Where the client itself looks for it."""
    try:
        import platformdirs
    except ImportError:  # it comes with the client
        return False
    _scrub_environment()
    cache = Path(
        os.environ.get("TILED_CACHE_DIR") or platformdirs.user_cache_dir("tiled")
    )
    api = urllib.parse.quote_plus(f"{url}/api/v1/")
    return (cache / "tokens" / api / "refresh_token").is_file()


def whoami(client: Any) -> str:
    """Whose the server takes the login to be, as ``id (provider)``."""
    principal = client.context.whoami() or {}
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
