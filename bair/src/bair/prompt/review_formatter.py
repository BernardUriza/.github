"""Format ReviewResponse into markdown and post to the PR."""

from __future__ import annotations

import json

from ..domain.models import PRIdentifier, ReviewResponse
from ..contracts import ActionsIO, GitHubClient
from ..log import logger


def check_existing_review(github: GitHubClient, pr: PRIdentifier) -> bool:
    """Return True if the bot already reviewed this commit — skip to avoid dupes."""
    try:
        latest_sha = github.run_gh(
            "api", f"repos/{pr.repo}/pulls/{pr.number}",
            "--jq", ".head.sha",
        ).strip()
    except Exception:
        return False

    if not latest_sha:
        return False

    try:
        existing = github.run_gh(
            "api", f"repos/{pr.repo}/pulls/{pr.number}/reviews",
            "--jq",
            f'[.[] | select((.user.login == "github-actions[bot]" or .user.login == "bair-bot[bot]") and .commit_id == "{latest_sha}")] | length',
        ).strip()
        return int(existing) > 0
    except Exception:
        return False


def format_review_body(
    review: ReviewResponse,
    pr: PRIdentifier,
    run_id: str,
    truncated: bool,
    max_diff_bytes: int,
) -> str:
    """Build the markdown body for the PR review comment."""
    run_url = f"https://github.com/{pr.repo}/actions/runs/{run_id}"
    count = len(review.findings)

    body_parts = [f"## {review.title}", "", review.summary]

    if truncated:
        body_parts.append("")
        body_parts.append(f"> Diff truncated (>{max_diff_bytes} bytes). Some files not reviewed.")

    if count > 0:
        body_parts.append("")
        for f in review.findings:
            body_parts.append(f"**{f.severity}** · `{f.file}:{f.line}` — {f.comment}")

    if review.highlights:
        body_parts.append("")
        for h in review.highlights:
            body_parts.append(f"✓ `{h.file}:{h.line}` — {h.comment}")

    body_parts.extend(["", "---", f"[Trace]({run_url}) · {count} findings"])
    return "\n".join(body_parts)


def _post_review_api(
    github: GitHubClient,
    endpoint: str,
    payload: dict,
) -> bool:
    """Post a review via gh api. Returns True only on actual success (response has 'id')."""
    result = github.run_gh(
        "api", endpoint,
        "--method", "POST", "--input", "-",
        check=False,
        input_data=json.dumps(payload),
    )
    output = result.strip()
    if not output:
        return False
    try:
        data = json.loads(output)
        if "id" in data:
            return True
        # Error response (e.g. {"message": "...", "documentation_url": "..."})
        msg = data.get("message", output[:200])
        logger.warning(f"  API rejected: {msg}")
        return False
    except (json.JSONDecodeError, ValueError):
        return False


def post_review(
    review: ReviewResponse,
    pr: PRIdentifier,
    run_id: str,
    truncated: bool,
    max_diff_bytes: int,
    actions: ActionsIO,
    github: GitHubClient,
) -> None:
    """Format and post the review to the PR."""
    body = format_review_body(review, pr, run_id, truncated, max_diff_bytes)
    count = len(review.findings)
    endpoint = f"repos/{pr.repo}/pulls/{pr.number}/reviews"

    if count == 0:
        # NEVER APPROVE — bot approvals count toward branch protection
        # and allow merging without human review. Always COMMENT.
        if _post_review_api(github, endpoint, {"body": body, "event": "COMMENT"}):
            logger.info(f"Posted clean review as COMMENT ({review.title})")
        else:
            actions.error("Failed to post review")
    else:
        comments = [
            {
                "path": f.file,
                "line": f.line,
                "body": f"**{f.severity.upper()}**: {f.comment}",
            }
            for f in review.findings
            if f.file and f.line > 0
        ]

        if _post_review_api(github, endpoint, {"body": body, "event": "COMMENT", "comments": comments}):
            logger.info(f"Posted with inline comments ({review.title})")
        else:
            actions.warning("Inline comments failed — posting body-only")
            if _post_review_api(github, endpoint, {"body": body, "event": "COMMENT"}):
                logger.info(f"Posted body-only ({review.title})")
            else:
                actions.error("Failed to post review — all attempts rejected")
