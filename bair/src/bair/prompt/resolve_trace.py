"""Write resolve pipeline trace to Job Summary."""

from __future__ import annotations

from dataclasses import dataclass, field

from .. import __version__
from ..infra.constants import TEMPLATES_DIR
from ..contracts import ActionsIO
from ..log import logger

_TEMPLATE = (TEMPLATES_DIR / "work-trace.md").read_text(encoding="utf-8")


@dataclass
class StageTimer:
    """Collects timing and status for each pipeline stage."""

    name: str
    duration: float = 0.0
    status: str = "⬚ skipped"

    @property
    def duration_str(self) -> str:
        if self.duration <= 0:
            return "—"
        if self.duration < 1:
            return f"{self.duration:.2f}s"
        return f"{self.duration:.1f}s"


@dataclass
class ResolveTrace:
    """Accumulates data throughout the resolve pipeline for the final summary."""

    stages: dict[str, StageTimer] = field(default_factory=dict)

    # Issue
    identifier: str = ""
    title: str = ""
    description: str = ""
    issue_url: str = ""
    assignee: str = ""
    project: str = ""
    labels: str = ""
    state_before: str = ""
    state_after: str = "In Review"

    # Pipeline
    repo: str = ""
    branch: str = ""
    pr_url: str = ""
    turns: int = 0
    agent_result: str = ""
    agent_prompt: str = ""
    files_changed: str = "(not captured)"
    commit_log: str = "(not captured)"
    dry_run: bool = False
    error: str = ""

    # True while the pipeline is running (set False after the final stage
    # records, or when an exception is recorded). Drives the 🟡 "in progress"
    # status shown by streaming partial renders.
    in_progress: bool = True

    def stage(self, name: str) -> StageTimer:
        """Get or create a stage timer by name."""
        if name not in self.stages:
            self.stages[name] = StageTimer(name=name)
        return self.stages[name]

    def record(self, name: str, duration: float, status: str = "✅") -> None:
        """Record a completed stage."""
        s = self.stage(name)
        s.duration = duration
        s.status = status

    def fail(self, name: str, duration: float, error: str = "") -> None:
        """Record a failed stage."""
        s = self.stage(name)
        s.duration = duration
        s.status = f"❌ {error[:60]}" if error else "❌"

    @property
    def total_time(self) -> float:
        return sum(s.duration for s in self.stages.values())

    @property
    def status(self) -> str:
        if self.error:
            return "FAILED"
        if self.in_progress:
            return "IN PROGRESS"
        if self.dry_run:
            return "DRY RUN"
        if self.pr_url:
            return "SUCCESS"
        return "UNKNOWN"

    @property
    def status_emoji(self) -> str:
        if self.error:
            return "❌"
        if self.in_progress:
            return "🟡"
        if self.dry_run:
            return "🧪"
        if self.pr_url:
            return "✅"
        return "❓"


def _get_stage(trace: ResolveTrace, name: str) -> tuple[str, str]:
    """Return (duration_str, status) for a stage, with defaults if missing."""
    s = trace.stages.get(name)
    if not s:
        return "—", "⬚ skipped"
    return s.duration_str, s.status


def _truncate(text: str, max_chars: int = 3000) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n\n... (truncated, {len(text)} total chars)"


def render_resolve_trace(trace: ResolveTrace) -> str:
    """Render the work trace template against the current state of ``trace``.

    Safe to call at any point during the pipeline — stages that haven't been
    recorded yet render as ``⬚ skipped`` via ``_get_stage``. Used by both
    the streaming partial renders (``write_resolve_trace_streaming``) and the
    final batch write (``write_resolve_trace``).
    """
    pr_link = f"[{trace.pr_url.split('/')[-1]}]({trace.pr_url})" if trace.pr_url else "_(dry run)_"
    # The tracker is Plane since April 2026 — `trace.issue_url` is populated by the
    # fetch_issue stage. No hardcoded URL fallback (the old Linear fallback was
    # dropped in the tracker-agnostic rename).
    issue_url = trace.issue_url or f"https://app.plane.so/bernard-org-ai/browse/{trace.identifier}/"

    t_gather, s_gather = _get_stage(trace, "gather")
    t_branch, s_branch = _get_stage(trace, "branch")
    t_prompt, s_prompt = _get_stage(trace, "prompt")
    t_agent, s_agent = _get_stage(trace, "agent")
    t_verify, s_verify = _get_stage(trace, "verify")
    t_push, s_push = _get_stage(trace, "push")
    t_pr, s_pr = _get_stage(trace, "pr")
    t_tracker, s_tracker = _get_stage(trace, "tracker")

    total_secs = trace.total_time
    if total_secs >= 60:
        total_str = f"{int(total_secs // 60)}m {int(total_secs % 60)}s"
    else:
        total_str = f"{total_secs:.1f}s"

    # Agent result — quote as blockquote for readability
    agent_display = trace.agent_result.strip() if trace.agent_result else "_(no output captured)_"
    if trace.error:
        agent_display += f"\n\n> ⚠️ **Error:** {trace.error}"

    summary = _TEMPLATE.format(
        vair_version=__version__,
        identifier=trace.identifier,
        issue_url=issue_url,
        title=trace.title,
        repo=trace.repo,
        branch=trace.branch,
        pr_link=pr_link,
        turns=trace.turns,
        total_time=total_str,
        status_emoji=trace.status_emoji,
        status=trace.status,
        assignee=trace.assignee or "_(unassigned)_",
        project=trace.project or "_(none)_",
        labels=trace.labels or "_(none)_",
        state_before=trace.state_before,
        state_after=trace.state_after,
        t_gather=t_gather, s_gather=s_gather,
        t_branch=t_branch, s_branch=s_branch,
        t_prompt=t_prompt, s_prompt=s_prompt,
        t_agent=t_agent, s_agent=s_agent,
        t_verify=t_verify, s_verify=s_verify,
        t_push=t_push, s_push=s_push,
        t_pr=t_pr, s_pr=s_pr,
        t_tracker=t_tracker, s_tracker=s_tracker,
        files_changed=trace.files_changed,
        agent_result=agent_display,
        issue_description=_truncate(trace.description or "_(no description)_"),
        agent_prompt=_truncate(trace.agent_prompt or "_(not captured)_"),
        commit_log=trace.commit_log or "_(not captured)_",
    )

    return summary


def write_resolve_trace_streaming(trace: ResolveTrace, actions: ActionsIO) -> None:
    """Render the trace and OVERWRITE $GITHUB_STEP_SUMMARY.

    Safe to call after every ``trace.record`` / ``trace.fail`` to give the
    user progressive visibility in the GitHub Actions UI. Best-effort:
    swallows render errors so a transient bug here never masks the real
    pipeline outcome.
    """
    try:
        actions.replace_summary(render_resolve_trace(trace))
    except Exception as e:  # pragma: no cover — defensive
        logger.warning(f"[resolve-trace] streaming render failed (non-fatal): {e}")


def write_resolve_trace(trace: ResolveTrace, actions: ActionsIO) -> None:
    """Build the work trace markdown and write it to $GITHUB_STEP_SUMMARY.

    Uses overwrite semantics (``replace_summary``) so this is safe to call
    after one or more streaming partial renders — the final state wins.
    """
    summary = render_resolve_trace(trace)
    actions.replace_summary(summary)

    # Mirror to stdout for gh API visibility
    logger.info("=" * 60)
    logger.info("WORK TRACE (mirrored to stdout)")
    logger.info("=" * 60)
    logger.info(f"Issue:    {trace.identifier} — {trace.title}")
    logger.info(f"Repo:     {trace.repo}")
    logger.info(f"Branch:   {trace.branch}")
    logger.info(f"PR:       {trace.pr_url or '(dry run)'}")
    logger.info(f"Turns:    {trace.turns}")
    total_secs = trace.total_time
    if total_secs >= 60:
        total_str = f"{int(total_secs // 60)}m {int(total_secs % 60)}s"
    else:
        total_str = f"{total_secs:.1f}s"
    logger.info(f"Time:     {total_str}")
    logger.info(f"Status:   {trace.status}")
    logger.info(f"Files:    {trace.files_changed[:200]}")
    logger.info("=" * 60)
