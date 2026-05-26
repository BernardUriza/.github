"""VAIR Dashboard — entry point. Binds events and handles auto-login."""

from browser import ajax, document, timer, window

from .auth import open_login_modal, close_login_modal, on_overlay_click, try_login, logout
from .config import VERSION
from .state import state
from .ux import bind_topbar_search, render_control_panel, render_health_panel

document["version-tag"].text = f"v{VERSION} · internal tool · visalaw.ai"


# ── Event bindings ───────────────────────────────────────────────

def enter_dashboard(ev):
    """Landing 'Enter Dashboard' click.

    Use a saved token if one exists (skip re-pasting), otherwise open the
    token modal. We deliberately do NOT auto-login on page load anymore —
    the landing hub must always show first so the user can choose Bot
    Dashboard vs Quick Observability.
    """
    saved_tok = window.localStorage.getItem("vair_token")
    if saved_tok:
        try_login(saved_tok)
    else:
        open_login_modal(ev)


document["open-login-btn"].bind("click", enter_dashboard)
document["modal-close-btn"].bind("click", close_login_modal)
document["login-overlay"].bind("click", on_overlay_click)
document["logout-btn"].bind("click", logout)


def on_login_click(ev):
    token = document["token-input"].value.strip()
    if not token:
        from .auth import show_login_error
        show_login_error("Paste your GitHub token")
        return
    document["login-error"].style.display = "none"
    try_login(token)


def on_token_keypress(ev):
    if ev.key == "Enter":
        on_login_click(ev)


document["login-btn"].bind("click", on_login_click)
document["token-input"].bind("keypress", on_token_keypress)


_ux_shell_bound = False


def _mount_ux_shell():
    global _ux_shell_bound
    if _ux_shell_bound:
        return
    try:
        app_visible = "visible" in document["app"].attrs.get("class", "")
    except Exception:
        app_visible = False
    if not app_visible or not state.user:
        timer.set_timeout(_mount_ux_shell, 250)
        return
    render_control_panel()
    render_health_panel()
    bind_topbar_search()
    _ux_shell_bound = True

# ── Auto-login from localStorage (persists across browser restarts) ──
# One-time migration: drop the legacy `vair_linear_token` and `vair_plane_token`
# keys. Both were issue-tracker tokens used only for `/users/me/` validation
# (theatre security — issue data comes from the static snapshot anyway). The
# GitHub token survives untouched.

saved = window.localStorage.getItem("vair_token")
if not saved:
    saved = window.sessionStorage.getItem("vair_token")
    if saved:
        window.localStorage.setItem("vair_token", saved)
        window.sessionStorage.removeItem("vair_token")

# Drop legacy issue-tracker token storage (Linear + Plane)
for _legacy in ("vair_linear_token", "vair_plane_token", "vair_plane_token_meta"):
    if window.localStorage.getItem(_legacy):
        window.localStorage.removeItem(_legacy)
    if window.sessionStorage.getItem(_legacy):
        window.sessionStorage.removeItem(_legacy)

def _try_local_secret_helper():
    """Last-resort auto-login: ask local_secret_helper.py for a GitHub token.

    Only runs when the dashboard is loaded over http://localhost:8765 (the
    helper restricts CORS to that origin). Lets developers skip the modal
    entirely as long as they have ~/.secrets/github_pat.txt and the helper
    running on port 8766.
    """
    host = str(window.location.hostname) if hasattr(window, "location") else ""
    if host not in ("localhost", "127.0.0.1"):
        return

    def _on_gh(req):
        import json as _json
        if req.status != 200:
            return
        try:
            gh_token = (_json.loads(req.text) or {}).get("token", "")
        except Exception:
            gh_token = ""
        if not gh_token:
            return
        try_login(gh_token, auto=True)

    req = ajax.Ajax()
    req.open("GET", "http://127.0.0.1:8766/github-token", True)
    req.bind("complete", _on_gh)
    req.send()


# No auto-login on page load — the landing hub must always show first so
# the user can pick Bot Dashboard vs Quick Observability. A saved token is
# still honored when they click Enter Dashboard (see enter_dashboard), so
# they don't have to re-paste it. The local secret helper is localhost-only
# dev convenience and returns early on the deployed Pages site.
_try_local_secret_helper()

timer.set_timeout(_mount_ux_shell, 250)
