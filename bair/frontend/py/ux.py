"""UX helpers for dashboard health, global search, mode, and action history."""

from browser import ajax, document, html, window
from datetime import datetime
import json as _json

from .config import API, VERSION
from .state import state


_search_callbacks = []
_search_query = ""
_events = []
_refresh_callbacks = []
_EVENTS_KEY = "vair_action_audit"


def now_label():
    """Return a compact local timestamp."""
    return datetime.now().strftime("%H:%M:%S")


def register_search(callback):
    """Register a panel callback that receives global search text."""
    _search_callbacks.append(callback)


def register_refresh(callback):
    """Register a panel refresh callback for Refresh All."""
    _refresh_callbacks.append(callback)


def current_search():
    return _search_query


def _notify_search():
    for callback in list(_search_callbacks):
        try:
            callback(_search_query)
        except Exception:
            pass


def _on_search(ev):
    global _search_query
    _search_query = ev.target.value.strip().lower()
    _notify_search()


def render_control_panel():
    """Render search/mode controls and persistent action feedback."""
    load_events()
    panel = document["control-panel"]
    panel.clear()

    header = html.DIV(Class="runs-header")
    header <= html.H3("Control Center")
    panel <= header

    body = html.DIV(Class="control-body")
    refresh = html.BUTTON("Refresh all", Class="dispatch-btn refresh-all-btn")
    refresh.bind("click", lambda ev: refresh_all())
    body <= refresh

    body <= html.LABEL("Recent actions")
    events = html.DIV(id="event-log", Class="event-log")
    body <= events
    panel <= body
    render_events()


def bind_topbar_search():
    document["global-search"].bind("input", _on_search)


def add_event(title, detail="", status="info", url=""):
    """Add a persistent action/status entry."""
    _events.insert(0, {
        "title": title,
        "detail": detail,
        "status": status,
        "url": url,
        "time": now_label(),
    })
    del _events[5:]
    save_events()
    render_events()


def load_events():
    """Load local action audit from localStorage."""
    global _events
    try:
        raw = window.localStorage.getItem(_EVENTS_KEY)
        _events = _json.loads(raw) if raw else []
    except Exception:
        _events = []


def save_events():
    try:
        window.localStorage.setItem(_EVENTS_KEY, _json.dumps(_events))
    except Exception:
        pass


def refresh_all():
    """Refresh every registered panel."""
    add_event("Refresh all requested", "Dashboard panels", "info")
    _refresh_registered_panels()


def _refresh_registered_panels():
    """Refresh panels without adding another audit event."""
    for callback in list(_refresh_callbacks):
        try:
            callback()
        except Exception:
            pass
    refresh_rate_limit()



def render_events():
    try:
        box = document["event-log"]
    except Exception:
        return
    box.clear()
    if not _events:
        box <= html.P("No actions yet.", Class="muted-copy")
        return
    # Status icons paired with each event level — small visual cue inline
    # with the row so the state is readable without relying purely on the
    # left border-color.
    # Status icons keyed by every value add_event() can carry. `failure` is
    # the legacy alias some callers still use — keep it equivalent to `error`.
    _STATUS_ICONS = {
        "success": "✓",
        "warn": "!",
        "error": "✕",
        "failure": "✕",
        "info": "•",
    }
    for event in _events:
        status = event["status"]
        row = html.DIV(Class=f"event-row event-{status}")
        row <= html.SPAN(_STATUS_ICONS.get(status, "•"), Class=f"event-status-icon event-status-{status}")
        content = html.DIV(Class="event-content")
        content <= html.DIV(event["title"], Class="event-title")
        if event["detail"]:
            content <= html.DIV(event["detail"], Class="event-detail")
        row <= content
        row <= html.DIV(event["time"], Class="event-time")
        if event.get("url"):
            row <= html.A("Open", href=event["url"], target="_blank", Class="event-link")
        box <= row


def render_health_panel():
    """Render operational health for auth, integrations, and static versioning."""
    panel = document["health-panel"]
    panel.clear()
    header = html.DIV(Class="runs-header")
    header <= html.H3("System Health")
    panel <= header

    body = html.DIV(Class="health-body")
    body <= _health_row("Mode", "Mock data" if state.mock_mode else "Live APIs", True, warn=state.mock_mode)
    body <= _health_row("GitHub", "Connected" if state.token else "Missing", bool(state.token) or state.mock_mode, warn=state.mock_mode)
    body <= _health_row("Rate limit", _rate_limit_text(), _rate_limit_ok(), warn=_rate_limit_warn())
    body <= _health_row("Version", f"v{VERSION}", True)
    body <= _health_row("CSS cache", f"v{VERSION}", True)
    body <= _health_row("Session", state.user.get("login", "Unknown") if state.user else "Signed out", bool(state.user))
    panel <= body


def refresh_rate_limit():
    """Fetch GitHub API rate limit and refresh health panel."""
    if state.mock_mode:
        state.rate_limit = {"remaining": 4976, "limit": 5000, "reset": "mock"}
        render_health_panel()
        return
    if not state.token:
        render_health_panel()
        return

    req = ajax.Ajax()
    req.open("GET", f"{API}/rate_limit", True)
    req.set_header("Authorization", f"token {state.token}")
    req.set_header("Accept", "application/vnd.github+json")
    req.bind("complete", _on_rate_limit)
    req.send()


def _on_rate_limit(req):
    if req.status == 200:
        try:
            data = _json.loads(req.text)
            core = data.get("resources", {}).get("core", {})
            state.rate_limit = {
                "remaining": core.get("remaining"),
                "limit": core.get("limit"),
                "reset": core.get("reset"),
            }
        except Exception:
            state.rate_limit = {"error": "parse failed"}
    else:
        state.rate_limit = {"error": f"HTTP {req.status}"}
    render_health_panel()


def _rate_limit_text():
    rl = state.rate_limit or {}
    if state.mock_mode and not rl:
        return "mock"
    if rl.get("error"):
        return rl["error"]
    remaining = rl.get("remaining")
    limit = rl.get("limit")
    if remaining is None or limit is None:
        return "Unknown"
    return f"{remaining}/{limit}"


def _rate_limit_ok():
    rl = state.rate_limit or {}
    if rl.get("error"):
        return False
    remaining = rl.get("remaining")
    if remaining is None:
        return state.mock_mode
    return remaining > 250


def _rate_limit_warn():
    rl = state.rate_limit or {}
    remaining = rl.get("remaining")
    return remaining is not None and remaining <= 250


def _health_row(label, value, ok, warn=False):
    row = html.DIV(Class="health-row")
    dot_class = "health-dot ok" if ok else ("health-dot warn" if warn else "health-dot bad")
    row <= html.SPAN(Class=dot_class)
    row <= html.SPAN(label, Class="health-label")
    value_cell = html.SPAN(Class="health-value")
    value_cell <= value
    row <= value_cell
    return row


def confirm_action(title, lines):
    """Confirm a dispatch/review with enough context to avoid wrong-target runs."""
    message = title + "\n\n" + "\n".join(lines)
    return window.confirm(message)
