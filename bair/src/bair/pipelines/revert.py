"""Revert pipeline — emergency rollback of every commit authored by the BAIR App.

Triggered by `/ai-revert` on a PR comment. No Claude, no LLM, no token spend
beyond the GitHub App token already minted by the calling workflow. Pure git.

Procedure:
  1. Resolve PR head + base via the GitHub API.
  2. List every commit on `base..head` whose author login is the BAIR bot.
  3. For each BAIR commit (in reverse chronological order — newest first), run
     `git revert --no-edit <sha>` so the working tree returns to the state
     before the remedy.
  4. Push the revert commits to the PR head branch with `--force-with-lease`
     keyed to the captured head SHA (so a parallel push by anyone else aborts
     us instead of clobbering them).
  5. Post a comment on the PR summarising what was reverted (commit subjects
     and SHAs).

Why revert instead of reset+force-push:
  - Audit trail: GitHub history shows what was reverted and when.
  - Recoverable: if you decide later to re-apply, you cherry-pick the original
    commits back.
  - Doesn't rewrite history, so other clones don't desync.

Limitations:
  - If a non-BAIR commit was made AFTER a BAIR commit and modifies the same
    region, the revert may conflict. We surface the conflict as a comment and
    STOP — Bernard resolves manually.
  - Author detection uses the literal login `bair-bot[bot]`.
    If the bot identity ever changes, update VAIR_BOT_LOGINS below.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from typing import List

from ..infra.container import Container
from ..log import logger


VAIR_BOT_LOGINS = (
    "bair-bot[bot]",
    "bair-ai[bot]",  # legacy app — leave for safety, harmless if absent
)


def _run(cmd: List[str], cwd: str | None = None, check: bool = True) -> str:
    """Run a shell command, return stdout, raise on non-zero unless check=False."""
    logger.info(f"[revert] $ {' '.join(cmd)}{f' (cwd={cwd})' if cwd else ''}")
    result = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, check=False, encoding="utf-8"
    )
    if result.stdout:
        logger.info(result.stdout.rstrip())
    if result.stderr:
        logger.info(f"[stderr] {result.stderr.rstrip()}")
    if check and result.returncode != 0:
        raise RuntimeError(
            f"command failed (exit {result.returncode}): {' '.join(cmd)}\n"
            f"stderr: {result.stderr.strip()}"
        )
    return result.stdout


def _post_pr_comment(container: Container, repo: str, pr_num: str, body: str) -> None:
    container.github.run_gh(
        "api", f"repos/{repo}/issues/{pr_num}/comments",
        "--method", "POST", "-f", f"body={body}",
        check=False,
    )


def run_revert(container: Container, repo: str, pr_num: str) -> None:
    """Revert every BAIR-authored commit on the PR's head branch."""
    if not repo or not pr_num:
        raise RuntimeError("REPO and PR_NUM env vars are required for /ai-revert")

    pr_view = container.github.run_gh(
        "pr", "view", pr_num,
        "--repo", repo,
        "--json", "headRefName,baseRefName,headRefOid",
    )
    pr = json.loads(pr_view)
    head_ref: str = pr["headRefName"]
    base_ref: str = pr["baseRefName"]
    head_sha: str = pr["headRefOid"]

    logger.info(f"[revert] PR #{pr_num}: head={head_ref} ({head_sha[:7]}) base={base_ref}")

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("No GH_TOKEN/GITHUB_TOKEN in env — workflow must mint BAIR app token first")

    with tempfile.TemporaryDirectory(prefix="ai-revert-") as tmpdir:
        clone_url = f"https://x-access-token:{token}@github.com/{repo}.git"
        _run(["git", "clone", "--branch", head_ref, "--depth", "100", clone_url, tmpdir])

        _run(["git", "config", "user.name",  "bair-bot[bot]"], cwd=tmpdir)
        _run(
            ["git", "config", "user.email",
             "120941174+bair-bot[bot]@users.noreply.github.com"],
            cwd=tmpdir,
        )

        _run(["git", "fetch", "origin", base_ref], cwd=tmpdir)
        author_filters: List[str] = []
        for login in VAIR_BOT_LOGINS:
            author_filters.extend(["--author", login.replace("[bot]", r"\[bot\]")])

        log_out = _run(
            ["git", "log", f"origin/{base_ref}..HEAD",
             "--reverse", "--format=%H%x09%s"] + author_filters,
            cwd=tmpdir,
        )

        commits: List[tuple[str, str]] = []
        for line in log_out.strip().splitlines():
            if "\t" in line:
                sha, subj = line.split("\t", 1)
                commits.append((sha.strip(), subj.strip()))

        if not commits:
            logger.info("[revert] No BAIR commits found on this branch — nothing to revert.")
            _post_pr_comment(
                container, repo, pr_num,
                "⏮️ `/ai-revert` ran, but found **no commits authored by "
                "`bair-bot[bot]`** on this branch. Nothing to do.",
            )
            return

        logger.info(f"[revert] Found {len(commits)} BAIR commit(s) to revert:")
        for sha, subj in commits:
            logger.info(f"  - {sha[:7]} {subj}")

        for sha, subj in reversed(commits):
            logger.info(f"[revert] Reverting {sha[:7]} ({subj})")
            try:
                _run(["git", "revert", "--no-edit", sha], cwd=tmpdir)
            except RuntimeError as exc:
                _run(["git", "revert", "--abort"], cwd=tmpdir, check=False)
                _post_pr_comment(
                    container, repo, pr_num,
                    f"⏮️ `/ai-revert` **stopped on conflict** while reverting "
                    f"`{sha[:7]}` ({subj}). Resolve manually:\n\n"
                    f"```\ngit revert {sha}\n# resolve conflicts\ngit revert --continue\n```\n\n"
                    f"Already reverted: "
                    + (", ".join(f"`{s[:7]}`" for s, _ in commits[: commits.index((sha, subj))])
                       or "_(none)_"),
                )
                raise RuntimeError(f"revert conflict at {sha}") from exc

        push_args = ["git", "push", "origin", f"HEAD:{head_ref}",
                     f"--force-with-lease=refs/heads/{head_ref}:{head_sha}"]
        _run(push_args, cwd=tmpdir)

        body_lines = [
            f"⏮️ **Reverted {len(commits)} commit(s)** authored by "
            f"`bair-bot[bot]` on this branch.",
            "",
            "| sha | subject |",
            "|-----|---------|",
        ]
        for sha, subj in commits:
            body_lines.append(f"| `{sha[:7]}` | {subj} |")
        body_lines.append("")
        body_lines.append(
            "Branch is now at the pre-remedy state. Re-run `/ai-remedy` to "
            "re-apply (with potentially different findings), or close this PR "
            "if the remedy was the wrong direction."
        )
        _post_pr_comment(container, repo, pr_num, "\n".join(body_lines))

    logger.info(f"[revert] Done — reverted {len(commits)} commit(s) and posted summary.")
