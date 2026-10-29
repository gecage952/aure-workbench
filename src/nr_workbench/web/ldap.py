import ssl

from flask import current_app
from ldap3 import Connection, Server, Tls
from ldap3.utils.conv import escape_filter_chars

#: The group for facility staff with access to every IPTS. Recorded on the
#: user at login (``SessionUser.data_access``); nothing checks it yet.
DATA_ACCESS_GROUP = "data_access"


def posix_groups_for(uid: str) -> list[str]:
    """Return the cn of every posixGroup that lists ``uid`` as a memberUid.

    The server and search base come from the app's ``AURE_LDAP_URL`` and
    ``AURE_LDAP_BASE_DN``, so this must run inside a Flask app context.

    Raises:
        RuntimeError: If either setting is missing.
    """
    url = current_app.config.get("AURE_LDAP_URL")
    base_dn = current_app.config.get("AURE_LDAP_BASE_DN")
    if not url or not base_dn:
        raise RuntimeError(
            "No LDAP server is configured (AURE_LDAP_URL and AURE_LDAP_BASE_DN)."
        )

    # Verify the server cert, like ldapsearch does.
    tls = Tls(validate=ssl.CERT_REQUIRED)
    server = Server(url, use_ssl=True, tls=tls, connect_timeout=5)

    # Your ldapsearch uses -x with -D but no -w, which is an unauthenticated
    # simple bind. An anonymous bind (no user) gets the same result.
    with Connection(server, auto_bind=True, receive_timeout=10) as conn:
        conn.search(
            search_base=base_dn,
            search_filter=f"(&(objectClass=posixGroup)(memberUid={escape_filter_chars(uid)}))",
            attributes=["cn"],
        )
        return [str(entry.cn) for entry in conn.entries]


def ipts_projects_in(groups: list[str]) -> list[str]:
    """The ``IPTS-*`` groups among *groups*."""
    return [group for group in groups if group.startswith("IPTS-")]


def filter_ipts_projects(uid: str) -> list[str]:
    return ipts_projects_in(posix_groups_for(uid))
