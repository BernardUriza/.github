"""Runs panel — workflow runs table with filters, presets, and sorting."""

from browser import document, html

from .api import gh_get
from .config import REPO, WORKFLOWS
from .filters import FilterBar
from .stats import calc_duration_secs, fmt_duration
from .table import SortableTable


# ── State ────────────────────────────────────────────────────────
_raw_runs: list = []
_table: SortableTable | None = None
_filter_bar: FilterBar | None = None

# ── Column definitions ───────────────────────────────────────────
_COLUMNS = [
    {"label": "",         "key": "badge"},
    {"label": "Workflow", "key": "workflow", "sortable": True},
    {"label": "Branch",   "key": "branch",   "sortable": True},
    {"label": "Duration", "key": "duration",  "sortable": True,
     "sort_value": lambda r: r.get("_dur_secs", 0)},
    {"label": "When",     "key": "created",   "sortable": True,
     "sort_value": lambda r: r.get("_created_raw", "")},
    {"label": "",         "key": "link"},
]

# ── Filters + presets ────────────────────────────────────────────
_FILTERS = [
    {"key": "workflow", "label": "Workflow",
     "options": [{"value": v, "label": n} for v, n in sorted(WORKFLOWS.items(), key=lambda x: x[1])]},
    {"key": "status", "label": "Status",
     "options": [
         {"value": "success",     "label": "Success"},
         {"value": "failure",     "label": "Failure"},
         {"value": "in_progress", "label": "Running"},
         {"value": "cancelled",   "label": "Cancelled"},
     ]},
]

_PRESETS = [
    {"label": "All",    "filters": {}},
    {"label": "Failed", "filters": {"status": "failure"}},
    {"label": "Active", "filters": {"status": "in_progress"}},
]


def _render_run_row(row):
    """Render a single run as a table row.

    Failed runs get a row-level class so the failure stands out in long
    lists \u2014 scanning a column of badges is fine, but a subtle row tint
    moves the signal off the dot and into the row itself. Branch and
    workflow truncate via CSS ellipsis (table-layout: fixed) so the full
    value lives in DOM and is available via the title= attribute on hover.
    """
    row_cls = "row-failure" if row["conclusion"] == "failure" else ""
    tr = html.TR(Class=row_cls) if row_cls else html.TR()
    tr <= html.TD(html.SPAN(row["badge_label"], Class=f"badge {row['badge_cls']}"))
    tr <= html.TD(row["workflow"], title=row["workflow"])
    tr <= html.TD(row["branch"], title=row["branch"])
    tr <= html.TD(row["duration"])
    tr <= html.TD(row["created"], Class="runs-cell-muted")
    tr <= html.TD(html.A("\u2197", href=row["url"], target="_blank"))
    return tr


def _transform_run(run):
    """Transform a raw API run into a flat display dict."""
    conclusion = run.get("conclusion") or run.get("status", "")
    badge_map = {"success": "badge-success", "failure": "badge-failure",
                 "in_progress": "badge-progress", "cancelled": "badge-cancelled"}
    name = run.get("name", "?")
    short = (name.replace("AI Resolve \u2014 Autonomous Agent", "Resolve")
                 .replace("AI PR Review (GPT-5.4)", "Review")
                 .replace("Deploy Dashboard to GitHub Pages", "Deploy Pages")
                 .replace("pages build and deployment", "Pages"))
    dur_secs = calc_duration_secs(run)
    wf_path = run.get("path", "")

    return {
        "conclusion": conclusion,
        "badge_cls": badge_map.get(conclusion, "badge-cancelled"),
        "badge_label": conclusion.replace("_", " ").title() if conclusion else "Running",
        "workflow": short,
        "_wf_path": wf_path,
        "branch": run.get("head_branch", "?"),
        "duration": fmt_duration(dur_secs),
        "_dur_secs": dur_secs or 0,
        "created": run.get("created_at", "")[5:16].replace("T", " "),
        "_created_raw": run.get("created_at", ""),
        "url": run.get("html_url", "#"),
    }


def _apply_filters(state):
    """Filter raw runs by current filter state, update stats + table."""
    filtered = _raw_runs
    wf = state.get("workflow", "all")
    if wf != "all":
        filtered = [r for r in filtered if r["_wf_path"].endswith(wf)]
    st = state.get("status", "all")
    if st != "all":
        filtered = [r for r in filtered if r["conclusion"] == st]
    # Update stats with filtered data (need raw API runs for stats)
    # Stats use the filtered display rows
    update_stats_from_display(filtered)
    _table.set_data(filtered)
    _table.render()


def update_stats_from_display(rows):
    """Update stat cards from display row dicts."""
    total = len(rows)
    successes = sum(1 for r in rows if r["conclusion"] == "success")
    failures = sum(1 for r in rows if r["conclusion"] == "failure")
    active = sum(1 for r in rows if r["conclusion"] == "in_progress")
    rate = f"{int(successes / total * 100)}%" if total else "0%"
    durations = [r["_dur_secs"] for r in rows if r["_dur_secs"]]
    avg = int(sum(durations) / len(durations)) if durations else 0
    avg_str = fmt_duration(avg)

    document["stat-total-val"].text = str(total)
    document["stat-total-sub"].text = f"{failures} failed"
    document["stat-success-val"].text = rate
    document["stat-success-sub"].text = f"{successes}/{total}"
    document["stat-time-val"].text = avg_str
    document["stat-time-sub"].text = f"across {len(durations)} runs"
    document["stat-active-val"].text = str(active)
    document["stat-active-sub"].text = "running now" if active else "all idle"


# ── Public API ───────────────────────────────────────────────────

def render_runs_panel():
    """Build the runs panel structure with filter bar."""
    global _table, _filter_bar
    panel = document["runs-panel"]
    panel.clear()

    header = html.DIV(Class="runs-header")
    header <= html.H3("Recent Runs")
    actions = html.DIV(Class="runs-header-actions")
    refresh = html.BUTTON("Refresh")
    refresh.bind("click", lambda ev: load_runs())
    actions <= refresh
    header <= actions
    panel <= header

    panel <= html.DIV(id="runs-filters")
    panel <= html.DIV(id="runs-body", Class="runs-scroll")

    _filter_bar = FilterBar("runs-filters", _FILTERS, _PRESETS, _apply_filters)
    _filter_bar._active_preset = 0  # "All" active by default
    _filter_bar.render()

    _table = SortableTable("runs-body", _COLUMNS, _render_run_row)
    _table.empty_msg = "No runs match filters."


def load_runs():
    """Fetch runs from GitHub API."""
    global _raw_runs
    _raw_runs = []
    body = document["runs-body"]
    body.clear()
    body <= html.DIV(html.SPAN(Class="spinner") + " Loading...", Class="loading-msg")
    gh_get(f"/repos/{REPO}/actions/runs?per_page=30", _on_runs_loaded)


def _on_runs_loaded(req):
    """Parse response, transform, apply current filters."""
    global _raw_runs
    import json as _json
    if req.status != 200:
        body = document["runs-body"]
        body.clear()
        body <= html.P(f"Error: {req.status}", Class="runs-error")
        return
    data = _json.loads(req.text)
    runs = data.get("workflow_runs", [])
    _raw_runs = [_transform_run(r) for r in runs]
    _apply_filters(_filter_bar.state if _filter_bar else {})
