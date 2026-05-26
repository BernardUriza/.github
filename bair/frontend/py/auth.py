"""Authentication — PAT login, org membership check, session management.

Tokens are persisted in localStorage so the session survives browser
restarts (the Chrome debug instance on port 9222 is long-lived).

Plane token is NOT required: issue data is read from the static
`data/issues.json` snapshot baked into the dashboard build, and dispatch
runs on GitHub workflows authenticated by the GitHub token. The legacy
`vair_plane_token` localStorage key (if present from older sessions) is
cleared on next load.

Edge cases handled:
- Expired/revoked token on auto-login: clears localStorage, shows modal with error
- Network error on auto-login: keeps tokens, shows modal with retry hint
- Double-click on Sign In: button disabled during request
- Token with leading/trailing whitespace: trimmed
"""

from browser import document, ajax, window, timer

from .config import API, ORG
from .state import state

_STORAGE_GH = "vair_token"
_LEGACY_STORAGE_PLANE = "vair_plane_token"
_LEGACY_STORAGE_PLANE_META = "vair_plane_token_meta"

# Track whether login was triggered automatically (from localStorage)
# vs manually (user clicked Sign In). Affects error handling behavior.
_auto_login = False


def open_login_modal(ev=None):
    document["login-overlay"].classList.add("visible")
    document["token-input"].focus()


def close_login_modal(ev=None):
    document["login-overlay"].classList.remove("visible")
    document["login-error"].style.display = "none"


def on_overlay_click(ev):
    if ev.target == document["login-overlay"]:
        close_login_modal()


def _set_login_busy(busy):
    """Disable/enable Sign In button to prevent double-click."""
    btn = document["login-btn"]
    if busy:
        btn.attrs["disabled"] = "disabled"
        btn.text = "Signing in..."
        btn.style.opacity = "0.6"
        btn.style.pointerEvents = "none"
    else:
        if "disabled" in btn.attrs:
            del btn.attrs["disabled"]
        btn.text = "Sign in"
        btn.style.opacity = ""
        btn.style.pointerEvents = ""


def try_login(token, auto=False):
    """Start the login flow: validate GitHub token + org membership.

    ``auto=True`` means this was triggered from localStorage on page load,
    not by the user clicking Sign In. Affects error display behavior.
    """
    global _auto_login
    _auto_login = auto

    token = token.strip()

    if not token:
        show_login_error("Paste your GitHub token")
        return

    state.token = token
    _set_login_busy(True)

    req = ajax.Ajax()
    req.open("GET", f"{API}/user", True)
    req.set_header("Authorization", f"token {token}")
    req.set_header("Accept", "application/vnd.github+json")
    req.bind("complete", _on_user_response)
    req.send()


def _on_user_response(req):
    import json as _json

    if req.status == 401:
        # Token is definitely bad — clear from storage to stop retry loops
        window.localStorage.removeItem(_STORAGE_GH)
        _fail_login("GitHub token is invalid or expired — generate a new one")
        return

    if req.status == 0:
        # Network error / CORS — don't clear token, it might be temporary
        _fail_login("Cannot reach GitHub — check your network and retry")
        return

    if req.status != 200:
        _fail_login(f"GitHub API error (HTTP {req.status}) — try again")
        return

    user = _json.loads(req.text)
    state.user = user

    membership_req = ajax.Ajax()
    membership_req.open("GET", f"{API}/orgs/{ORG}/members/{user['login']}", True)
    membership_req.set_header("Authorization", f"token {state.token}")
    membership_req.set_header("Accept", "application/vnd.github+json")
    membership_req.bind("complete", _on_membership_check)
    membership_req.send()


def _on_membership_check(req):
    if req.status == 0:
        _fail_login("Cannot reach GitHub — check your network and retry")
        return

    if req.status != 204:
        # Definitely not a member — clear token to stop retry loops
        window.localStorage.removeItem(_STORAGE_GH)
        login_name = state.user["login"] if state.user else "unknown"
        _fail_login(f"'{login_name}' is not a member of the {ORG} org")
        return

    # GitHub is good — persist it
    window.localStorage.setItem(_STORAGE_GH, state.token)
    _finish_login()


def _fail_login(msg):
    """Login failed — show error, re-enable button, open modal if auto-login."""
    _set_login_busy(False)
    state.token = ""

    err = document["login-error"]
    err.text = msg
    err.style.display = "block"
    err.style.color = "var(--red, #f87171)"

    # If this was auto-login, the modal isn't visible yet — open it
    # so the user can see WHY auto-login failed and re-enter tokens
    if _auto_login:
        open_login_modal()


def show_login_error(msg):
    """Show a blocking error — login fails. Use for manual-only pre-checks."""
    _fail_login(msg)


def show_login_warning(msg):
    """Show a non-blocking warning — login proceeds after delay."""
    err = document["login-error"]
    err.text = msg
    err.style.display = "block"
    err.style.color = "var(--yellow, #fbbf24)"


def _finish_login():
    """Login succeeded — transition to the app."""
    _set_login_busy(False)
    close_login_modal()
    document["landing"].classList.add("hidden")
    window.stopParticles()
    timer.set_timeout(_show_app, 600)


def _show_app():
    from .bento import render_bento
    document["landing"].style.display = "none"
    document["app"].classList.add("visible")
    document.body.style.overflow = "hidden"
    document["user-name"].text = state.user["login"]
    document["user-avatar"].attrs["src"] = state.user["avatar_url"]
    render_bento()


def logout(ev=None):
    """Clear all tokens from state and localStorage, return to landing.

    Includes the OpenAI BYOK token: switching BAIR users should not leave
    the previous user's OpenAI key reachable to the next one (different
    person = different billing). The OpenAI key is stored in localStorage
    under `vair_openai_token`.

    Also resets the chat session (in-memory conversation, snapshot cache)
    so the next user starts fresh.
    """
    from . import chat
    chat.reset_session()

    window.localStorage.removeItem(_STORAGE_GH)
    window.localStorage.removeItem(_LEGACY_STORAGE_PLANE)
    window.localStorage.removeItem(_LEGACY_STORAGE_PLANE_META)
    window.localStorage.removeItem("vair_openai_token")
    state.token = ""
    state.openai_token = ""
    state.user = None
    document["app"].classList.remove("visible")
    document["landing"].classList.remove("hidden")
    document["landing"].style.display = ""
    document.body.style.overflow = "hidden"
    window.startParticles()
    # Clear input fields so stale tokens don't linger
    document["token-input"].value = ""
    document["login-error"].style.display = "none"
    _set_login_busy(False)
