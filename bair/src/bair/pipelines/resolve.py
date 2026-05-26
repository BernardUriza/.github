"""Resolve pipeline — autonomous issue resolution via Claude Agent SDK.

Reads an issue from the tracker, creates a branch, runs Claude as an agent
to implement the changes, verifies the build, and opens a PR.

Phase 1 MVP: single issue, manual trigger, human reviews + merges.

DEPRECATED (run_resolve monolith) — to be removed when the work-side
counterpart of VAIR_REVIEW_MULTI_PERSPECTIVE flag policy is settled.

``run_resolve`` (line 260 below) has no production callers as of 2026-05-11.
``__main__.work`` routes exclusively through
``pipelines/resolve_via_executor.py:run_resolve_full_via_executor`` since PR #40.

The module remains importable because ``stages/resolve_postagent.py`` still
depends on helpers (``check_substance_gate``, ``commit_and_push``,
``create_pr``, ``verify_build``) re-exported from here. Those helpers
should move to ``stages/`` or ``pipelines/_helpers/`` before the file is
deleted.

Audit reference:
https://github.com/bernard-org/engineering-notes/blob/main/audits/bair-multi-perspective-2026-05-11/README.md#finding-1
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time

from .. import __version__

from ..config.resolve import ResolveConfig
from ..domain.agent_run import AgentRunOutcome
from ..domain.exceptions import ConfigError, ProviderError, ValidationError
from ..domain.models import Issue
from ..domain.resolve_prompts import RESOLVE_SYSTEM_PROMPT, build_resolve_prompt
from ..infra.container import Container
from ..contracts import GitHubClient
from ..log import logger

# Backward-compat aliases — los templates y builder se movieron a
# domain/resolve_prompts.py para que stages/resolve_bridge.py los consuma desde
# la capa correcta (sin importar de pipelines/, lo cual violaría layer purity).
# Estos aliases preservan callsites internos que importaban con underscore.
_RESOLVE_SYSTEM_PROMPT = RESOLVE_SYSTEM_PROMPT
_build_resolve_prompt = build_resolve_prompt


# ── Post-agent helpers ───────────────────────────────────────────────


# Files we refuse to count as "substantive agent work" when deciding whether
# to push and open a PR. The workflow's pre-pipeline `npm install` step
# (ai-resolve.yml step "Install target repo dependencies") mutates the
# lockfile's `peer: true` metadata even with `--ignore-scripts`. Without
# this filter, an agent that does zero Edits still triggers `git status`
# → 1 dirty file → commit → push → junk PR. See VIS-553 run 24914067494.
_METADATA_FILES_THAT_DONT_COUNT_AS_AGENT_WORK = frozenset({
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    ".DS_Store",
})


def check_substance_gate(
    edit_calls: int,
    workspace: str,
    base_branch: str,
    issue_id: str,
    tool_breakdown: tuple[tuple[str, int], ...] = (),
) -> None:
    """Abort before commit+push when the agent didn't produce real work.

    Catches the VIS-553 failure mode (run 24914067494): agent ran 47 turns
    with Read/Grep/Bash only, never invoked Edit/Write, and the pipeline
    still produced a PR whose only diff was `package-lock.json` churn from
    the workflow's pre-pipeline `npm install` step. Three checks:

    1. **Edit/Write tool count** — if 0, the agent never attempted any
       source change. Cleanly resolves "issue is already done" cases too
       (e.g. VIS-364): the run fails with a clear tracker comment instead
       of producing a junk PR that auto-transitions the issue to In Review
       on a fake fix.

    2. **Investigation evidence** — if the agent did Edit but never invoked
       any of Grep / Glob / Read / Bash, it edited without verifying the
       bug exists. Catches the VIS-296 failure mode (PR #536, 2026-04-23):
       agent "fixed" a non-existent Filters-button-onClick bug; Bernard
       closed the PR after grepping and finding the handler was already
       wired. When ``tool_breakdown`` is empty (e.g. legacy callers that
       didn't thread it), this check is skipped — the existing edit_calls
       gate still protects against the zero-Edit case.

    3. **Substantive diff** — even when Edits ran, if the resulting diff
       against `origin/<base>` contains only metadata files (lockfiles,
       OS turds), the agent's edits did not survive. Refuse to push.

    Raises ValidationError. Caller is expected to record `trace.fail` and
    update the tracker before re-raising.
    """
    if edit_calls == 0:
        raise ValidationError(
            f"Agent ran but never invoked Edit or Write — no source code "
            f"changes were attempted on {issue_id}. Refusing to push or "
            f"create a PR. If the issue is already resolved by existing "
            f"code, mark it Done manually; otherwise the issue stays open "
            f"for retry. This is the VIS-553 / VIS-364 failure mode."
        )

    if tool_breakdown:
        breakdown_map = {name: count for name, count in tool_breakdown}
        investigation_calls = sum(
            breakdown_map.get(tool, 0)
            for tool in ("Grep", "Glob", "Read", "Bash")
        )
        if investigation_calls == 0:
            raise ValidationError(
                f"Agent invoked Edit/Write {edit_calls} time(s) on {issue_id} "
                f"but ran ZERO investigation tool calls (no Grep, Glob, Read, "
                f"or Bash). Refusing to push — the agent edited without first "
                f"verifying the bug exists on this branch. This is the "
                f"VIS-296 / PR #536 failure mode (agent 'fixed' a non-existent "
                f"bug, Bernard had to close the PR after grepping and finding "
                f"the symptom was not real). Rule: "
                f"engineering-notes/ai-rules/rules/shared/code-quality.md "
                f"→ 'Reproduce / Verify The Bug Exists Before You Edit'."
            )

    diff_files: list[str] = []
    try:
        diff_files = subprocess.run(
            ["git", "diff", "--name-only", f"origin/{base_branch}...HEAD"],
            cwd=workspace, capture_output=True, text=True, timeout=10,
        ).stdout.strip().splitlines()
    except Exception as e:
        logger.warning(f"[SUBSTANCE] git diff failed (non-fatal): {e}")

    status_lines: list[str] = []
    try:
        status_lines = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=workspace, capture_output=True, text=True, timeout=10,
        ).stdout.strip().splitlines()
    except Exception as e:
        logger.warning(f"[SUBSTANCE] git status failed (non-fatal): {e}")

    # `git status --porcelain` lines are "XY <path>" — first 3 chars are
    # the porcelain prefix, the rest is the path.
    uncommitted_files = [line[3:].strip() for line in status_lines if line.strip()]
    all_files = sorted(set(diff_files) | set(uncommitted_files))

    substantive = [
        f for f in all_files
        if f not in _METADATA_FILES_THAT_DONT_COUNT_AS_AGENT_WORK
        and not f.startswith("node_modules/")
    ]

    if not substantive:
        raise ValidationError(
            f"Agent invoked Edit/Write {edit_calls} time(s) but the diff "
            f"against origin/{base_branch} contains only metadata files. "
            f"All files seen: {all_files}. The edits did not survive — "
            f"likely reverted, applied to ignored paths, or only mutated "
            f"the lockfile. Refusing to push or create a PR."
        )

    logger.info(
        f"[SUBSTANCE] Gate passed: {edit_calls} Edit/Write call(s), "
        f"{len(substantive)} substantive file(s): {substantive[:5]}"
        + (f" + {len(substantive) - 5} more" if len(substantive) > 5 else "")
    )


def verify_build(github: GitHubClient, workspace: str, repo: str) -> None:
    """Post-agent build gate, dispatched by repo language.

    The gate is language-aware so it never runs a TypeScript check against a
    non-TypeScript repo. Detection is by marker file in the workspace root:

    - ``tsconfig.json`` present  -> TypeScript gate (``npx tsc --noEmit``).
    - ``pyproject.toml`` / ``setup.py`` / ``setup.cfg`` / ``requirements.txt``
      present (and no ``tsconfig.json``) -> Python syntax gate
      (``python -m compileall``, stdlib-only, no extra deps).
    - neither -> no gate; log and pass.

    Incident 2026-05-19 (<ticket-id> / <ticket-id> on bernard-org-gen-standalone-services,
    a Python repo): the gate ran ``npx tsc --noEmit`` unconditionally, hit the
    "this is not the tsc command you are looking for" npx stub, raised
    ``ValidationError``, and skipped PR creation even though the agent's fix had
    already been committed and pushed. Two real fixes never became PRs because
    the build gate spoke the wrong language.
    """
    if os.path.exists(os.path.join(workspace, "tsconfig.json")):
        _verify_typescript_build(workspace)
        return

    python_markers = ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt")
    if any(os.path.exists(os.path.join(workspace, m)) for m in python_markers):
        _verify_python_build(workspace)
        return

    logger.info(
        "[VERIFY] No tsconfig.json or Python project markers in workspace — "
        "skipping build gate (unknown project type)."
    )


def _verify_python_build(workspace: str) -> None:
    """Python syntax gate: byte-compile the tree with stdlib ``compileall``.

    No third-party deps, no virtualenv assumptions — catches syntax errors the
    agent may have introduced without requiring the repo's full toolchain to be
    installed on the runner. Excludes vendored/build dirs to stay fast.
    """
    logger.info("[VERIFY] Running Python syntax check (compileall)...")
    result = subprocess.run(
        'python -m compileall -q -x "(\\.venv|venv|node_modules|\\.git|build|dist)" .',
        cwd=workspace, capture_output=True, text=True, timeout=120,
        shell=True,
    )
    if result.returncode != 0:
        errors = result.stdout or result.stderr
        logger.warning(f"[VERIFY] Python compile failed:\n{errors[:2000]}")
        raise ValidationError(f"Python syntax check failed:\n{errors[:500]}")
    logger.info("[VERIFY] Python syntax check passed")


def _verify_typescript_build(workspace: str) -> None:
    """Run tsc as a post-agent build gate.

    Belt-and-suspenders: re-run `npm install --ignore-scripts` before tsc to
    restore any node_modules mutations the agent may have made during its
    turns. Idempotent — seconds on an already-populated node_modules. See
    the commit that introduced this for the incident that motivated it
    (VIS-708 run 24907039027 — agent's Bash tool deleted packages that
    tsc then flagged as TS2307).
    """
    logger.info("[VERIFY] Restoring node_modules before type check...")
    restore = subprocess.run(
        "npm install --no-audit --no-fund --ignore-scripts",
        cwd=workspace, capture_output=True, text=True, timeout=300,
        shell=True,
    )
    if restore.returncode != 0:
        logger.warning(
            f"[VERIFY] node_modules restore failed (rc={restore.returncode}), "
            f"continuing to tsc anyway:\n{(restore.stderr or restore.stdout)[:500]}"
        )
    else:
        logger.info("[VERIFY] node_modules restored")

    logger.info("[VERIFY] Running type check...")
    result = subprocess.run(
        "npx tsc --noEmit",
        cwd=workspace, capture_output=True, text=True, timeout=120,
        shell=True,  # Required on Windows to find npx.cmd via PATH
    )
    if result.returncode != 0:
        # tsc writes errors to stdout, not stderr
        errors = result.stdout or result.stderr
        logger.warning(f"[VERIFY] tsc failed:\n{errors[:2000]}")
        raise ValidationError(f"TypeScript check failed:\n{errors[:500]}")
    logger.info("[VERIFY] tsc passed")


def commit_and_push(github: GitHubClient, workspace: str, issue: Issue) -> None:
    """Stage, commit (if needed), and push."""
    # Check if agent already committed
    status = github.run_git("status", "--porcelain", cwd=workspace).strip()
    if status:
        logger.info(f"[COMMIT] {len(status.splitlines())} uncommitted files, committing...")
        # -A stages tracked + untracked (new files created by the agent). The
        # injected .claude/rules/ dir is excluded via .git/info/exclude, set
        # by ai-resolve.yml before this pipeline runs, so -A won't pull it in.
        github.run_git("add", "-A", cwd=workspace)
        msg = f"feat({issue.team_key.lower()}): {issue.title} ({issue.identifier})"
        github.run_git("commit", "-m", msg, cwd=workspace)
    else:
        logger.info("[COMMIT] Agent already committed, nothing to stage")

    logger.info(f"[PUSH] Pushing {issue.git_branch_name}...")
    github.run_git("push", "-u", "origin", issue.git_branch_name, cwd=workspace)


def _build_pr_body(issue: Issue, outcome: AgentRunOutcome | None) -> str:
    """Compose the PR body for a resolve-pipeline draft PR.

    Shape when ``outcome`` is available:

        Closes <tracker-prefix>-XXX

        ## Fix
        <agent's own result_text — its narration after the last edit>

        ## Files changed
        - path/to/file1
        - path/to/file2

        ## Issue context
        <first 300 chars of issue description — enough to anchor, not paraphrase>

        <!-- bair: version=X turns=N tool_calls=M edit_calls=K -->

    Reviewer reads "Fix" + "Files changed" first — both come from the agent's
    actual work, not from re-stating the bug. The version stamp is moved to
    an HTML comment so it persists for audit without biasing humans toward
    light-touch review of bot PRs.

    When ``outcome`` is None (legacy callers not yet threaded), the body
    falls back to a header + issue context, still without the visible
    Automated-by-BAIR footer.
    """
    closes = f"Closes {issue.identifier}\n\n"
    issue_ctx = (
        f"## Issue context\n\n"
        f"{issue.description[:300].rstrip() if issue.description else '(no description)'}\n"
    )

    if outcome is None:
        audit = f"\n<!-- bair: version={__version__} -->\n"
        return closes + issue_ctx + audit

    fix_text = (outcome.result_text or "").strip()
    fix_section = (
        f"## Fix\n\n"
        f"{fix_text[:2000] if fix_text else '(agent produced no summary — see commits for the diff)'}\n\n"
    )

    if outcome.files_touched:
        files_section = (
            "## Files changed\n\n"
            + "\n".join(f"- `{p}`" for p in outcome.files_touched)
            + "\n\n"
        )
    else:
        files_section = ""

    audit = (
        f"\n<!-- bair: version={__version__} "
        f"turns={outcome.turns} "
        f"tool_calls={outcome.tool_calls} "
        f"edit_calls={outcome.edit_calls} -->\n"
    )
    return closes + fix_section + files_section + issue_ctx + audit


def create_pr(
    github: GitHubClient, issue: Issue, cfg: ResolveConfig,
    workspace: str = ".",
    outcome: AgentRunOutcome | None = None,
) -> str:
    """Create a draft PR via gh CLI.

    When ``outcome`` is provided, the PR body cites the agent's own summary
    (``result_text``) and the list of files it touched (``files_touched``).
    Replaces the legacy body shape, which only paraphrased the issue and
    added an ``Automated by BAIR`` footer — that shape gave reviewers
    nothing the issue itself did not already say and biased them toward
    skimming bot output.
    """
    # Sanitize title — quotes in issue titles break shell args
    safe_title = issue.title.replace('"', "'")
    title = f"feat({issue.team_key.lower()}): {safe_title} ({issue.identifier})"
    body = _build_pr_body(issue, outcome)
    # Write body to temp file — avoids shell escaping issues with quotes
    body_file = tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, encoding="utf-8",
    )
    body_file.write(body)
    body_file.close()

    # gh pr create warns about uncommitted changes (injected .claude/rules
    # and agent temp files). Use check=False and parse URL from output.
    result = subprocess.run(
        ["gh", "pr", "create",
         "--draft",
         "--base", cfg.base_branch,
         "--title", title,
         "--body-file", body_file.name,
         "--assignee", "@me",
         "--reviewer", "bernarduriza-bernard-org",
         "--repo", cfg.repo],
        capture_output=True, text=True, encoding="utf-8",
        cwd=workspace,
    )
    os.unlink(body_file.name)

    combined = (result.stdout or "") + "\n" + (result.stderr or "")
    urls = [l for l in combined.splitlines() if l.startswith("https://github.com/")]
    if not urls:
        logger.error(f"[PR] gh pr create produced no URL")
        logger.error(f"[PR] stdout: {result.stdout}")
        logger.error(f"[PR] stderr: {result.stderr}")
        logger.error(f"[PR] exit code: {result.returncode}")
        raise ProviderError("gh pr create failed — no PR URL in output")

    pr_url = urls[0]
    logger.info(f"[PR] Created: {pr_url}")
    return pr_url


def _update_tracker(
    container: Container, issue: Issue, pr_url: str,
) -> None:
    """Update the tracker: transition to In Review + attach PR."""
    if not container.tracker:
        return
    container.tracker.add_attachment(issue.id, pr_url, f"PR: {issue.identifier}")
    container.tracker.add_comment(
        issue.id,
        f"Automated PR created: [{pr_url}]({pr_url})",
    )
    container.tracker.transition_to(issue, "started")


# ── Pipeline entry point ─────────────────────────────────────────────


def run_resolve(container: Container, cfg: ResolveConfig) -> None:
    """Execute the full resolve pipeline. Synchronous (wraps async Agent SDK internally).

    Job Summary is rendered progressively: an initial header is written
    before the first stage, the trace is re-rendered (overwrite) after each
    ``trace.record`` / ``trace.fail``, and a final ``try/finally`` block
    guarantees the summary survives uncaught exceptions anywhere in the body.
    """
    c = container
    t_start = time.monotonic()

    # -- Trace collector (for Job Summary) -----------------------------
    from ..prompt.resolve_trace import (
        ResolveTrace,
        write_resolve_trace,
        write_resolve_trace_streaming,
    )
    import datetime as _dt
    trace = ResolveTrace(repo=cfg.repo, dry_run=cfg.dry_run)
    trace.identifier = cfg.issue_id  # placeholder until gather populates the real one
    started_at = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    trace.agent_result = (
        f"_Started {started_at} — streaming progress; refresh to update._"
    )

    # Initial header — gives the user 🟡 IN PROGRESS visibility from second 1.
    # If the pipeline crashes before any stage records, this is still what
    # the final summary shows (the try/finally below re-renders it).
    write_resolve_trace_streaming(trace, c.actions)

    try:
        logger.info("=" * 60)
        logger.info(f"RESOLVE PIPELINE — {cfg.issue_id} on {cfg.repo}")
        logger.info("=" * 60)

        # -- 1. Validate ---------------------------------------------------
        if not c.tracker:
            raise ConfigError(
                "Issue tracker not configured — set PLANE_API_KEY before "
                "running the resolve pipeline"
            )

        # -- 2. Gather issue context ---------------------------------------
        logger.info("[1/8] GATHER issue context")
        t0 = time.monotonic()
        issue = c.tracker.get_issue(cfg.issue_id)
        logger.info(f"  {issue.identifier}: {issue.title}")
        logger.info(f"  state={issue.state_name} branch={issue.git_branch_name}")
        logger.info(f"  project={issue.project_name} assignee={issue.assignee_name}")
        trace.record("gather", time.monotonic() - t0)

        trace.identifier = issue.identifier
        trace.title = issue.title
        trace.description = issue.description
        trace.assignee = issue.assignee_name
        trace.project = issue.project_name
        trace.labels = ", ".join(issue.labels) if issue.labels else ""
        trace.state_before = issue.state_name
        trace.branch = issue.git_branch_name
        write_resolve_trace_streaming(trace, c.actions)

        if not issue.git_branch_name:
            raise ConfigError(f"Issue {issue.identifier} has no git branch name in the tracker")

        # -- 3. Create branch ----------------------------------------------
        logger.info("[2/8] CREATE branch")
        t0 = time.monotonic()
        import os
        workspace = os.environ.get("WORKSPACE", os.environ.get("GITHUB_WORKSPACE", "."))
        c.github.run_git("fetch", "origin", cwd=workspace)
        try:
            c.github.run_git(
                "checkout", "-b", issue.git_branch_name,
                f"origin/{cfg.base_branch}", cwd=workspace,
            )
            logger.info(f"  created {issue.git_branch_name} from origin/{cfg.base_branch}")
        except Exception:
            # Branch might already exist
            c.github.run_git("checkout", issue.git_branch_name, cwd=workspace)
            logger.info(f"  checked out existing {issue.git_branch_name}")
        trace.record("branch", time.monotonic() - t0)
        write_resolve_trace_streaming(trace, c.actions)

        # -- 4. Build prompt -----------------------------------------------
        logger.info("[3/8] BUILD prompt")
        t0 = time.monotonic()
        work_prompt = _build_resolve_prompt(issue, cfg.base_branch)
        logger.debug(f"  prompt: {len(work_prompt)} chars")
        trace.record("prompt", time.monotonic() - t0)
        trace.agent_prompt = work_prompt
        write_resolve_trace_streaming(trace, c.actions)

        # -- 5. Run agent --------------------------------------------------
        # Agent invocation extracted to infra.agent_runner.ClaudeSDKAgentRunner —
        # it owns asyncio.run(), the ANTHROPIC_API_KEY env var dance, and the
        # destructive command detection. The runner returns AgentRunOutcome;
        # we unpack it into the locals the rest of run_resolve already expects.
        # See also: stages/agent_run.py for the same runner consumed as a Stage.
        logger.info("[4/8] RUN Claude Agent SDK")
        logger.info(f"  max_turns={cfg.max_agent_turns}")
        t0 = time.monotonic()

        from ..infra.agent_runner import ClaudeSDKAgentRunner

        runner = ClaudeSDKAgentRunner()
        outcome = runner.run(
            user_prompt=work_prompt,
            system_prompt=_RESOLVE_SYSTEM_PROMPT,
            cwd=workspace,
            max_turns=cfg.max_agent_turns,
        )

        turns = outcome.turns
        tool_calls = outcome.tool_calls
        edit_calls = outcome.edit_calls
        result_text = outcome.result_text
        destructive_calls = list(outcome.destructive_calls)

        # The runner returns error as a string (not the raw Exception, by design —
        # the runner is a Protocol-bound implementation, can't leak SDK exception
        # types upward). Wrap as a generic Exception so the rest of run_resolve's
        # logic continues to see "agent_error: Exception | None".
        agent_error: Exception | None = Exception(outcome.error) if outcome.error else None
        if agent_error is not None:
            trace.fail("agent", time.monotonic() - t0, outcome.error or "")
            trace.error = outcome.error or ""

        if agent_error is None:
            trace.record("agent", time.monotonic() - t0, f"✅ {turns} turns")
            trace.turns = turns
            trace.agent_result = result_text
            logger.info(
                f"  Agent finished: {turns} turns, {tool_calls} tool calls "
                f"({edit_calls} Edit/Write), result={len(result_text)} chars"
            )
            if destructive_calls:
                logger.warning(
                    f"  Agent ran {len(destructive_calls)} destructive command(s) "
                    f"that mutate node_modules/deps. The post-verify tsc failure "
                    f"is likely the agent's own doing. Commands:"
                )
                for cmd in destructive_calls:
                    logger.warning(f"    - {cmd}")
        write_resolve_trace_streaming(trace, c.actions)

        # -- 5b. Substance gate -------------------------------------------------
        # Before pushing, refuse if the agent didn't produce real source-code
        # changes. Skipped on dry-run and when the agent already crashed
        # (in the crash case, partial work — even Edits in flight — is still
        # preserved by the commit+push block below; the existing error is
        # re-raised after the trace is written).
        if not cfg.dry_run and agent_error is None:
            t0 = time.monotonic()
            try:
                check_substance_gate(
                    edit_calls=edit_calls,
                    workspace=workspace,
                    base_branch=cfg.base_branch,
                    issue_id=cfg.issue_id,
                    tool_breakdown=outcome.tool_breakdown,
                )
                trace.record("substance", time.monotonic() - t0)
                write_resolve_trace_streaming(trace, c.actions)
            except ValidationError as e:
                logger.warning(f"[SUBSTANCE] Gate failed: {e}")
                trace.fail("substance", time.monotonic() - t0, str(e))
                trace.error = str(e)
                # Capture diff stat for the failure trace before re-raising.
                try:
                    trace.files_changed = subprocess.run(
                        ["git", "diff", "--stat", f"origin/{cfg.base_branch}..HEAD"],
                        capture_output=True, text=True, cwd=workspace, timeout=10,
                    ).stdout.strip() or "(no changes)"
                except Exception:
                    pass
                update_tracker_failure(c, issue, str(e))
                # Job Summary handled by the outer ``finally`` block.
                raise

        # -- 6. Commit + Push (BEFORE verify — preserves work even if tsc fails
        #       or the agent crashed mid-run with dirty state) -----------------
        t0 = time.monotonic()
        if cfg.dry_run:
            logger.info("[5/8] COMMIT — skipped (dry run)")
            logger.info("[6/8] PUSH — skipped (dry run)")
            trace.record("push", 0.0, "⬚ dry run")
        else:
            logger.info("[5/8] COMMIT + PUSH (pre-verify, preserves work)")
            try:
                commit_and_push(c.github, workspace, issue)
                logger.info("[6/8] PUSH — done")
                trace.record("push", time.monotonic() - t0)
            except Exception as e:
                # Don't mask the original error if the agent already failed.
                # Artifact upload in ai-resolve.yml (if: always()) is the last-resort
                # safety net when push itself fails.
                trace.fail("push", time.monotonic() - t0, str(e))
                logger.warning(f"  Commit/push failed: {e} — relying on artifact upload")
        write_resolve_trace_streaming(trace, c.actions)

        # Capture files changed and commit log BEFORE any further raise so the
        # trace shows what the agent attempted, even on failure.
        try:
            trace.files_changed = subprocess.run(
                ["git", "diff", "--stat", f"origin/{cfg.base_branch}..HEAD"],
                capture_output=True, text=True, cwd=workspace, timeout=10,
            ).stdout.strip() or "(no changes)"
            trace.commit_log = subprocess.run(
                ["git", "log", "--oneline", f"origin/{cfg.base_branch}..HEAD"],
                capture_output=True, text=True, cwd=workspace, timeout=10,
            ).stdout.strip() or "(no commits)"
        except Exception:
            pass

        # If the agent itself crashed, re-raise now — work already preserved above.
        # Job Summary handled by the outer ``finally`` block.
        if agent_error is not None:
            raise ProviderError(f"Agent SDK failed: {agent_error}") from agent_error

        # -- 7. Verify build (post-push; failure no longer destroys work) ------
        logger.info("[7/8] VERIFY build")
        t0 = time.monotonic()
        try:
            verify_build(c.github, workspace, cfg.repo)
            trace.record("verify", time.monotonic() - t0)
            write_resolve_trace_streaming(trace, c.actions)
        except ValidationError as e:
            logger.warning(f"  Build failed: {e}")
            trace.fail("verify", time.monotonic() - t0, str(e))
            trace.error = str(e)
            if not cfg.dry_run:
                update_tracker_failure(c, issue, str(e))
            # Job Summary handled by the outer ``finally`` block.
            raise

        # -- 8. Create PR --------------------------------------------------
        pr_url = ""
        t0 = time.monotonic()
        if cfg.dry_run:
            logger.info("[8/8] PR — skipped (dry run)")
            trace.record("pr", 0.0, "⬚ dry run")
            trace.record("tracker", 0.0, "⬚ dry run")
        else:
            logger.info("[8/8] CREATE PR")
            pr_url = create_pr(c.github, issue, cfg, workspace, outcome=outcome)
            trace.record("pr", time.monotonic() - t0)
            trace.pr_url = pr_url
            write_resolve_trace_streaming(trace, c.actions)

            t0 = time.monotonic()
            try:
                _update_tracker(c, issue, pr_url)
                trace.record("tracker", time.monotonic() - t0)
                trace.state_after = "In Review"
            except Exception as e:
                trace.fail("tracker", time.monotonic() - t0, str(e))
                logger.warning(f"  Tracker update failed (non-fatal): {e}")

        # Pipeline ran to completion — flip status away from 🟡 IN PROGRESS so
        # the ``finally`` block renders ✅ SUCCESS / 🧪 DRY RUN.
        trace.in_progress = False

        # -- Summary (logs) ------------------------------------------------
        total = time.monotonic() - t_start
        logger.info("=" * 60)
        logger.info("RESOLVE COMPLETE")
        logger.info(f"  Issue:    {issue.identifier} — {issue.title}")
        logger.info(f"  Branch:   {issue.git_branch_name}")
        logger.info(f"  PR:       {pr_url or '(dry run)'}")
        logger.info(f"  Time:     {total:.1f}s")
        logger.info("=" * 60)
    finally:
        # Guarantees the Job Summary survives ANY exception path — uncaught
        # provider errors, container builder failures, bugs in _update_tracker,
        # or success. Defensive: a render failure here must not mask the
        # original exception that brought us to ``finally``.
        try:
            # If we reached ``finally`` with in_progress still True, the body
            # raised before flipping it. Surface an error status so the final
            # render reads ❌ FAILED instead of 🟡 IN PROGRESS.
            if trace.in_progress:
                trace.in_progress = False
                if not trace.error:
                    trace.error = "Pipeline aborted before completion (uncaught exception)"
            write_resolve_trace(trace, c.actions)
        except Exception as render_err:  # pragma: no cover — defensive
            logger.warning(f"[resolve-trace] final render failed (non-fatal): {render_err}")


def update_tracker_failure(container: Container, issue: Issue, error: str) -> None:
    """Comment on the issue about the failure."""
    if not container.tracker:
        return
    container.tracker.add_comment(
        issue.id,
        f"BAIR Bot failed on this issue:\n```\n{error[:500]}\n```",
    )
