"""Mock data for dashboard development and visual verification."""


MOCK_USER = {
    "login": "bernarduriza-visalaw",
    "avatar_url": "img/vair-logo.png",
}

MOCK_RUNS = [
    {
        "name": "AI Resolve — Autonomous Agent",
        "conclusion": "success",
        "status": "completed",
        "head_branch": "vair/vis-621",
        "path": ".github/workflows/ai-resolve.yml",
        "created_at": "2026-04-27T10:42:00Z",
        "run_started_at": "2026-04-27T10:42:08Z",
        "updated_at": "2026-04-27T10:49:40Z",
        "html_url": "https://github.com/Visalaw/.github/actions/runs/1",
    },
    {
        "name": "AI PR Review (GPT-5.4)",
        "conclusion": "in_progress",
        "status": "in_progress",
        "head_branch": "main",
        "path": ".github/workflows/ai-dispatch.yml",
        "created_at": "2026-04-27T10:38:00Z",
        "run_started_at": "2026-04-27T10:38:09Z",
        "updated_at": "2026-04-27T10:41:20Z",
        "html_url": "https://github.com/Visalaw/.github/actions/runs/2",
    },
    {
        "name": "Deploy Dashboard to GitHub Pages",
        "conclusion": "failure",
        "status": "completed",
        "head_branch": "main",
        "path": ".github/workflows/pages.yml",
        "created_at": "2026-04-27T10:20:00Z",
        "run_started_at": "2026-04-27T10:20:03Z",
        "updated_at": "2026-04-27T10:20:39Z",
        "html_url": "https://github.com/Visalaw/.github/actions/runs/3",
    },
]

MOCK_ISSUES = [
    {
        "identifier": "VIS-621",
        "title": "Improve VAIR operational dashboard reliability",
        "priorityColor": "#f97316",
        "priorityLabel": "High",
        "project": "AI Reviewer",
        "state": "Backlog",
    },
    {
        "identifier": "VIS-619",
        "title": "Add rate-limit visibility to GitHub tooling",
        "priorityColor": "#eab308",
        "priorityLabel": "Medium",
        "project": "Platform",
        "state": "Ready",
    },
]

MOCK_REVIEW_PRS = [
    {
        "number": 622,
        "title": "feat(vair): add dashboard health and audit trail",
        "draft": False,
        "created_at": "2026-04-27T10:45:00Z",
        "html_url": "https://github.com/Visalaw/frontend-core-2.0/pull/622",
        "user": {"login": "bernarduriza-visalaw"},
        "head": {"sha": "abc123"},
        "_repo_short": "frontend-core",
        "_full_repo": "Visalaw/frontend-core-2.0",
    },
    {
        "number": 1199,
        "title": "fix(api): tighten draft generation retry behavior",
        "draft": True,
        "created_at": "2026-04-27T10:28:00Z",
        "html_url": "https://github.com/Visalaw/visalaw-gen-backend/pull/1199",
        "user": {"login": "ashukla-texau"},
        "head": {"sha": "def456"},
        "_repo_short": "backend",
        "_full_repo": "Visalaw/visalaw-gen-backend",
    },
]

MOCK_BOT_PRS = [
    {
        "number": 621,
        "title": "fix(vair): resolve VIS-621 dashboard reliability",
        "draft": False,
        "merged_at": None,
        "created_at": "2026-04-27T10:51:00Z",
        "html_url": "https://github.com/Visalaw/frontend-core-2.0/pull/621",
        "user": {"login": "vair-visalaw-ai-reviewer[bot]"},
        "head": {"sha": "abc621"},
        "_repo": "frontend-core-2.0",
        "_full_repo": "Visalaw/frontend-core-2.0",
    },
]
