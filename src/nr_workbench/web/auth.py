from authlib.integrations.flask_client import OAuth
from flask import Blueprint, abort, current_app, redirect, url_for
from ldap3.core.exceptions import LDAPException

from nr_workbench.web.ldap import DATA_ACCESS_GROUP, ipts_projects_in, posix_groups_for
from nr_workbench.web.user_session import SessionUser, current_user, log_in, log_out

OAUTH_PROVIDER = "aure"
USER_ID_CLAIM = "user_id"
#: Where authlib's ``init_app`` files the registry on the app.
_EXTENSION = "authlib.integrations.flask_client"


def init_oauth(app):
    # One registry per app, not a module-level one: authlib caches each client
    # on its registry, so a shared registry would hand every app after the
    # first the client -- and the client id -- of the first.
    oauth = OAuth(app)
    oauth.register(
        OAUTH_PROVIDER, server_metadata_url=app.config["AURE_OAUTH_WELL_KNOWN_URL"]
    )


def _client():
    """The OAuth client registered on the app serving this request."""
    oauth = current_app.extensions.get(_EXTENSION)
    if oauth is None:
        abort(404, "No identity provider is configured (AURE_OAUTH_WELL_KNOWN_URL).")
    return oauth.create_client(OAUTH_PROVIDER)


auth = Blueprint("auth", __name__, url_prefix="/auth")


@auth.app_context_processor
def _user() -> dict:
    # Every page's navbar shows who is logged in, or the "Log in" link.
    return {"current_user": current_user()}


@auth.get("/login")
def login():
    redirect_uri = url_for("auth.callback", _external=True)
    return _client().authorize_redirect(redirect_uri)


@auth.get("/callback")
def callback():
    token = _client().authorize_access_token()
    full_user_id = token.get("userinfo", {}).get(USER_ID_CLAIM)
    user_id = full_user_id.split("@")[0] if full_user_id else None
    if not user_id:
        abort(403, f"The identity provider sent no {USER_ID_CLAIM!r} claim.")
    try:
        # One lookup for both: the IPTS groups, and whether they have data access.
        groups = posix_groups_for(user_id)
    except (RuntimeError, LDAPException) as exc:
        # Refused, not logged in with no projects: an empty list would look
        # exactly like a user who has none.
        current_app.logger.error("IPTS lookup for %s failed: %s", user_id, exc)
        abort(503, "Could not look up your IPTS projects; try again later.")
    log_in(
        SessionUser(
            user_id=user_id,
            ipts_projects=ipts_projects_in(groups),
            data_access=DATA_ACCESS_GROUP in groups,
        )
    )
    return redirect("/")


@auth.get("/logout")
def logout():
    log_out()
    return redirect("/")
