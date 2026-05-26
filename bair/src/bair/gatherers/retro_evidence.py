"""Gather all evidence from a PR for retrospective analysis.

Collects: AI reviews, human comments, PR metadata, thread state.
No LLM calls — pure data fetching.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..domain.models import PRIdentifier
from ..contracts import GitHubClient


@dataclass
class ReviewEvidence:
    """A single AI review that was posted on the PR."""
    review_id: int
    body: str
    state: str
    submitted_at: str
    inline_comments: list[dict] = field(default_factory=list)


@dataclass
class CommentEvidence:
    """A human comment on the PR (issue comment or review reply)."""
    author: str
    body: str
    created_at: str
    is_bot: bool = False


@dataclass
class RetroEvidence:
    """All evidence collected from a PR for retrospective analysis."""
    pr: PRIdentifier
    pr_title: str
    pr_state: str  # open, closed, merged
    pr_merged_by: str
    pr_additions: int
    pr_deletions: int
    pr_changed_files: int
    diff: str
    ai_reviews: list[ReviewEvidence] = field(default_factory=list)
    human_comments: list[CommentEvidence] = field(default_factory=list)
    thread_count: int = 0
    resolved_threads: int = 0

    def to_llm_context(self) -> str:
        """Render evidence as structured text for the LLM prompt."""
        parts = [
            f"## PR #{self.pr.number}: {self.pr_title}",
            f"State: {self.pr_state} | +{self.pr_additions}/-{self.pr_deletions} | {self.pr_changed_files} files",
            "",
        ]

        if self.ai_reviews:
            parts.append("## AI Reviews")
            for i, rev in enumerate(self.ai_reviews, 1):
                parts.append(f"### Review {i} ({rev.state}, {rev.submitted_at})")
                parts.append(rev.body)
                if rev.inline_comments:
                    parts.append(f"\nInline comments ({len(rev.inline_comments)}):")
                    for c in rev.inline_comments:
                        parts.append(f"  - {c.get('path', '?')}:{c.get('line', '?')} — {c.get('body', '')[:200]}")
                parts.append("")

        if self.human_comments:
            parts.append("## Human Comments")
            for c in self.human_comments:
                parts.append(f"**{c.author}** ({c.created_at}):")
                parts.append(c.body[:500])
                parts.append("")

        parts.append(f"## Thread Status: {self.resolved_threads}/{self.thread_count} resolved")

        return "\n".join(parts)


def gather_retro_evidence(pr: PRIdentifier, github: GitHubClient) -> RetroEvidence:
    """Collect all evidence from a PR for retrospective analysis."""

    # PR metadata — fetch fields individually for reliability
    title = github.run_gh("api", f"repos/{pr.repo}/pulls/{pr.number}", "--jq", ".title", check=False).strip()
    state = github.run_gh("api", f"repos/{pr.repo}/pulls/{pr.number}", "--jq", ".state", check=False).strip()
    merged = github.run_gh("api", f"repos/{pr.repo}/pulls/{pr.number}", "--jq", ".merged", check=False).strip()
    merged_by = github.run_gh("api", f"repos/{pr.repo}/pulls/{pr.number}", "--jq", ".merged_by.login // \"\"", check=False).strip()
    additions = int(github.run_gh("api", f"repos/{pr.repo}/pulls/{pr.number}", "--jq", ".additions", check=False).strip() or "0")
    deletions = int(github.run_gh("api", f"repos/{pr.repo}/pulls/{pr.number}", "--jq", ".deletions", check=False).strip() or "0")
    changed_files = int(github.run_gh("api", f"repos/{pr.repo}/pulls/{pr.number}", "--jq", ".changed_files", check=False).strip() or "0")

    pr_state = "merged" if merged == "true" else state

    # Diff
    diff = github.run_gh("pr", "diff", str(pr.number), "--repo", pr.repo, check=False)

    # AI reviews (from github-actions[bot])
    # Capture reviews from any bot account — BAIR posts as
    # "bair-bot[bot]", not "github-actions[bot]".
    # Using .user.type == "Bot" is more resilient to renames.
    reviews_raw = github.run_gh(
        "api", f"repos/{pr.repo}/pulls/{pr.number}/reviews",
        "--jq", '[.[] | select(.user.type == "Bot") | {id: .id, body: .body, state: .state, submitted_at: .submitted_at, author: .user.login}]',
        check=False,
    ).strip()

    ai_reviews = []
    for rev in json.loads(reviews_raw or "[]"):
        # Fetch inline comments for each review
        inline_raw = github.run_gh(
            "api", f"repos/{pr.repo}/pulls/{pr.number}/reviews/{rev['id']}/comments",
            "--jq", "[.[] | {path: .path, line: .line, body: .body}]",
            check=False,
        ).strip()
        inline = json.loads(inline_raw or "[]")

        ai_reviews.append(ReviewEvidence(
            review_id=rev["id"],
            body=rev.get("body", ""),
            state=rev.get("state", ""),
            submitted_at=rev.get("submitted_at", ""),
            inline_comments=inline,
        ))

    # Human comments (issue comments, excluding bots)
    comments_raw = github.run_gh(
        "api", f"repos/{pr.repo}/issues/{pr.number}/comments",
        "--jq", '[.[] | {author: .user.login, body: .body, created_at: .created_at, is_bot: (.user.type == "Bot")}]',
        check=False,
    ).strip()

    human_comments = []
    for c in json.loads(comments_raw or "[]"):
        human_comments.append(CommentEvidence(
            author=c.get("author", ""),
            body=c.get("body", ""),
            created_at=c.get("created_at", ""),
            is_bot=c.get("is_bot", False),
        ))

    # Thread resolution status
    try:
        threads_raw = github.run_gh(
            "api", "graphql",
            "-f", f'query={{ repository(owner: "{pr.owner}", name: "{pr.name}") {{ pullRequest(number: {pr.number}) {{ reviewThreads(last: 50) {{ nodes {{ isResolved }} }} }} }} }}',
            "--jq", ".data.repository.pullRequest.reviewThreads.nodes",
            check=False,
        ).strip()
        threads = json.loads(threads_raw or "[]")
        thread_count = len(threads)
        resolved_threads = sum(1 for t in threads if t.get("isResolved"))
    except Exception:
        thread_count = 0
        resolved_threads = 0

    return RetroEvidence(
        pr=pr,
        pr_title=title,
        pr_state=pr_state,
        pr_merged_by=merged_by,
        pr_additions=additions,
        pr_deletions=deletions,
        pr_changed_files=changed_files,
        diff=diff[:80_000],  # cap diff to avoid token explosion
        ai_reviews=ai_reviews,
        human_comments=human_comments,
        thread_count=thread_count,
        resolved_threads=resolved_threads,
    )
