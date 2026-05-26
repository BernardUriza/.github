"""VAIR Pull Requests panel — open PRs with filters, sorting, Vercel links."""

from browser import document, html

from .api import gh_get
from .filters import FilterBar
from .table import SortableTable


# Repos VAIR operates in — both PRs it opens AND human PRs it reviews/remedies.
_REPOS = [
    "Visalaw/frontend-core-2.0",
    "Visalaw/visalaw-gen-backend",
    "Visalaw/.github",
]
# Short display names. Keeps the repo cell narrow so the PR # alongside the
# badge stays scannable. The raw repo slug remains the filter key.
_REPO_FRIENDLY = {
    "frontend-core-2.0": "Frontend",
    "visalaw-gen-backend": "Backend",
    ".github": "VAIR",
}

# ── State ────────────────────────────────────────────────────────
_raw_prs: list = []
_pending: int = 0
_preview_pending: int = 0
_preview_urls: dict = {}
_table: SortableTable | None = None
_filter_bar: FilterBar | None = None

# ── Columns ──────────────────────────────────────────────────────
_COLUMNS = [
    {"label": "\u25b6", "key": "preview"},
    {"label": "Repo",    "key": "repo",    "sortable": True},
    {"label": "Title",   "key": "title",   "sortable": True},
    {"label": "Created", "key": "created", "sortable": True,
     "sort_value": lambda r: r.get("_created_raw", "")},
]

# ── Filters + presets ────────────────────────────────────────────
_FILTERS = [
    {"key": "repo", "label": "Repo",
     "options": [
         {"value": "frontend-core-2.0",   "label": "Frontend"},
         {"value": "visalaw-gen-backend",  "label": "Backend"},
         {"value": ".github",              "label": "VAIR"},
     ]},
    {"key": "status", "label": "Status",
     "options": [
         {"value": "draft", "label": "Draft"},
         {"value": "open",  "label": "Open"},
     ]},
]

_PRESETS = [
    {"label": "All",      "filters": {}},
    {"label": "Drafts",   "filters": {"status": "draft"}},
    {"label": "Frontend", "filters": {"repo": "frontend-core-2.0"}},
]


def _render_pr_row(row):
    """Render a single PR as a table row."""
    tr = html.TR()

    # Preview link \u2014 Vercel deploy URL if frontend PR, em-dash otherwise.
    # Inline styles previously here were lifted to .pr-preview-link /
    # .pr-preview-empty CSS classes so the markup stays declarative.
    preview = row.get("preview_url")
    if preview:
        tr <= html.TD(html.A("\u25b6", href=preview, target="_blank",
                             title="Vercel deployment", Class="pr-preview-link"))
    else:
        tr <= html.TD("\u2014", Class="pr-preview-empty")

    # Repo cell: status badge + clickable "<RepoShort> #<N>" link. The PR
    # number is the user-facing identifier, NOT a stable repo name — putting
    # it in the visible link text means every row is identifiable at a glance
    # and copyable from the DOM without hover-then-click.
    repo_cell = html.TD(Class="pr-repo-cell")
    repo_cell <= html.SPAN(row["status_label"], Class=f"badge {row['badge_cls']}")
    repo_label = f"{row['repo_short']} #{row['number']}"
    repo_cell <= html.A(repo_label, href=row["url"], target="_blank",
                        Class="pr-repo-link")
    tr <= repo_cell

    # Title cell with full title in DOM (CSS ellipsis truncates visually).
    # title= attribute gives hover preview + copy/paste of the full title.
    title_cell = html.TD(row["title"], title=row["title"])
    tr <= title_cell

    tr <= html.TD(row["created"], Class="pr-cell-muted")
    return tr


def _transform_pr(pr):
    """Transform a raw API PR into a flat display dict."""
    draft = pr.get("draft", False)
    merged = pr.get("merged_at") is not None
    if merged:
        badge_cls = "badge-pr-merged"
        status_label = "MERGED"
    elif draft:
        badge_cls = "badge-pr-draft"
        status_label = "DRAFT"
    else:
        badge_cls = "badge-pr-open"
        status_label = "OPEN"

    title = pr.get("title", "?")
    repo = pr.get("_repo", "")

    return {
        "number": pr["number"],
        "repo": repo,
        "repo_short": _REPO_FRIENDLY.get(repo, repo),
        "full_repo": pr.get("_full_repo", ""),
        "title": title,
        "draft": draft,
        "merged": merged,
        "badge_cls": badge_cls,
        "status_label": status_label,
        "_status": "draft" if draft else ("merged" if merged else "open"),
        "created": pr.get("created_at", "")[5:16].replace("T", " "),
        "_created_raw": pr.get("created_at", ""),
        "url": pr.get("html_url", "#"),
        "sha": pr.get("head", {}).get("sha", ""),
        "preview_url": None,
    }


def _apply_filters(state):
    """Filter PRs by current filter state, update table."""
    filtered = _raw_prs
    repo = state.get("repo", "all")
    if repo != "all":
        filtered = [r for r in filtered if r["repo"] == repo]
    status = state.get("status", "all")
    if status != "all":
        filtered = [r for r in filtered if r["_status"] == status]
    _table.set_data(filtered)
    _table.render()


# ── Vercel URL resolution ────────────────────────────────────────

def _fetch_preview_urls():
    """For each frontend PR, fetch the Vercel deploy URL from commit statuses."""
    global _preview_pending
    frontend_prs = [pr for pr in _raw_prs if pr["repo"] == "frontend-core-2.0" and pr["sha"]]
    if not frontend_prs:
        _apply_filters(_filter_bar.state if _filter_bar else {})
        return

    _preview_pending = len(frontend_prs)
    for pr in frontend_prs:
        gh_get(f"/repos/{pr['full_repo']}/statuses/{pr['sha']}",
               lambda req, n=pr["number"]: _on_status_loaded(req, n))


def _on_status_loaded(req, pr_number):
    """Extract Vercel URL from commit statuses."""
    global _preview_pending
    if req and req.status == 200:
        import json as _json
        statuses = _json.loads(req.text)
        for s in statuses:
            if "vercel" in s.get("context", "").lower():
                url = s.get("target_url", "")
                if url:
                    # Patch into the row
                    for pr in _raw_prs:
                        if pr["number"] == pr_number:
                            pr["preview_url"] = url
                            break
                    break

    _preview_pending -= 1
    if _preview_pending <= 0:
        _apply_filters(_filter_bar.state if _filter_bar else {})


# ── Public API ───────────────────────────────────────────────────

def render_pulls_panel():
    """Build the pulls panel structure with filter bar."""
    global _table, _filter_bar
    panel = document["pulls-panel"]
    panel.clear()

    header = html.DIV(Class="runs-header")
    header <= html.H3("VAIR Pull Requests")
    actions = html.DIV(Class="runs-header-actions")
    refresh = html.BUTTON("Refresh", title="Refetch open PRs from GitHub")
    refresh.bind("click", lambda ev: load_pulls())
    actions <= refresh
    header <= actions
    panel <= header

    panel <= html.DIV(id="pulls-filters")
    panel <= html.DIV(id="pulls-body")

    _filter_bar = FilterBar("pulls-filters", _FILTERS, _PRESETS, _apply_filters)
    _filter_bar._active_preset = 0
    _filter_bar.render()

    _table = SortableTable("pulls-body", _COLUMNS, _render_pr_row)
    _table.empty_msg = "No VAIR PRs match filters."


def load_pulls():
    """Fetch open PRs from all repos."""
    global _raw_prs, _pending
    _raw_prs = []
    _pending = len(_REPOS)
    body = document["pulls-body"]
    body.clear()
    body <= html.DIV(html.SPAN(Class="spinner") + " Loading...", Class="loading-msg")
    for repo in _REPOS:
        gh_get(f"/repos/{repo}/pulls?state=open&per_page=30&sort=created&direction=desc",
               lambda req, r=repo: _on_pulls_loaded(req, r))


def _on_pulls_loaded(req, repo):
    """Accumulate PRs from each repo, then fetch preview URLs."""
    global _raw_prs, _pending
    import json as _json
    if req.status == 200:
        prs = _json.loads(req.text)
        # No bot-author filter. VAIR-touched PRs include human-authored PRs
        # that VAIR reviews/remedies — not just bot-opened ones. The user's
        # own open PRs are the most actionable items on this dashboard;
        # excluding them made the panel render zero useful rows.
        for pr in prs:
            pr["_repo"] = repo.split("/")[1]
            pr["_full_repo"] = repo
        _raw_prs.extend([_transform_pr(pr) for pr in prs])

    _pending -= 1
    if _pending <= 0:
        _raw_prs.sort(key=lambda pr: pr.get("_created_raw", ""), reverse=True)
        _fetch_preview_urls()
