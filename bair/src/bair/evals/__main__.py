"""python -m bair.evals — run the gatekeeper over labeled cases and score it.

Run from (or point ``--repo-dir`` at) a full-history checkout of the cases' repo.
Every (case, context mode, run) goes through ``gatekeep.review`` in its own git
worktree at the case's head, exactly like the live gate reviews a PR checkout.

  python -m bair.evals --runs 3 --context full,slice --split all --out results.jsonl

Held-out cases are reported only in aggregate, never case by case: reading them
while editing the prompt turns them into dev cases (backlog 01, step 6).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from importlib import resources
from pathlib import Path

from ..pipelines import gatekeep
from ..prompts import load_prompt
from .stats import discordant, summarize, wilson


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout


def _load_cases(path: str | None) -> dict:
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    return json.loads(resources.files("bair.evals").joinpath("cases_server_bot.json").read_text(encoding="utf-8"))


def _localized(decision: gatekeep.GatekeepDecision, fix_files: list[str]) -> bool:
    """Did any issue point at a file the real fix touched? Greptile's bar: a catch
    counts when the comment lands where the bug is, not just a nervous verdict."""
    text = " ".join(str(i.get("message", "")) for i in decision.issues)
    return any(f in text or Path(f).name in text for f in fix_files)


def _run_one(
    case: dict, ctx: str, model: str, run: int, tree: Path, diff: str, repo: str, fix_files: list[str], meta: dict
) -> dict:
    started = time.monotonic()
    d = gatekeep.review(
        diff, str(tree), repo, case["id"], context_mode=ctx, model=model,
        base_sha=case["base"], head_sha=case["head"],
    )
    return {
        **meta,
        "case": case["id"],
        "label": case["label"],
        "split": case["split"],
        "context": ctx,
        "model": model,
        "config": f"{ctx}@{model}",
        "run": run,
        "verdict": d.verdict,
        "severity": d.severity,
        "would_block": d.would_block,
        "provider": d.provider,
        "issues": [
            {
                "type": i.get("type"),
                "rule": i.get("rule"),
                "severity": i.get("severity"),
                # the claim itself, for label audits — dev only, like the summary
                **({"message": str(i.get("message", ""))[:600]} if case["split"] == "dev" else {}),
            }
            for i in d.issues
        ],
        # None when there is nothing to localize against: clean cases, and latent
        # defects (found by the suite, no fix commit yet).
        "localized": _localized(d, fix_files) if case["label"] == "defect" and fix_files else None,
        "summary": d.summary if case["split"] == "dev" else "",
        "seconds": round(time.monotonic() - started, 1),
    }


def _fmt_rate(k: int, n: int) -> str:
    lo, hi = wilson(k, n)
    return f"{k}/{n} ({lo:.0%}–{hi:.0%})"


def _report(rows: list[dict], meta: dict) -> str:
    lines = [
        "# BAIR eval",
        "",
        f"prompt `{meta['prompt_sha'][:12]}` · bair `{meta['bair_ref']}` · "
        f"{len(rows)} runs",
        "",
        "Effective verdict = majority over runs; a shadow-mode `would_block` counts as BLOCK. "
        "Intervals are 95% Wilson.",
    ]
    issues = [i for r in rows for i in r.get("issues") or []]
    valid = sum(1 for i in issues if i.get("rule") in ("data_plane", "general"))
    lines += ["", f"Issues carrying a valid `rule`: {valid}/{len(issues)}"]
    for split in ("dev", "heldout"):
        part = [r for r in rows if r["split"] == split]
        if not part:
            continue
        summary = summarize(part)
        lines += ["", f"## {split}", "", "| config | defects BLOCKed | defects flagged (≥WARN) | localized | clean falsely BLOCKed | clean ≥WARN | flipped cases | unavailable |", "|---|---|---|---|---|---|---|---|"]
        for ctx, s in sorted(summary.items()):
            lines.append(
                f"| {ctx} | {_fmt_rate(len(s['blocked']), s['defects'])} | {_fmt_rate(len(s['flagged']), s['defects'])} | "
                f"{len(s['localized'])}/{s['localizable']} | {_fmt_rate(len(s['false_block']), s['cleans'])} | "
                f"{_fmt_rate(len(s['false_alarm']), s['cleans'])} | {len(s['flips'])} | {len(s['unavailable'])} |"
            )
        ctxs = sorted(summary)
        if len(ctxs) == 2:
            diff = discordant(summary, *ctxs)
            lines += ["", f"Paired discordant cases ({ctxs[0]} → {ctxs[1]}): {len(diff)}"]
            if split == "dev":
                lines += [f"- `{c}`: {a} → {b}" for c, a, b in diff]
        if split == "dev":
            lines += ["", "| case | label | " + " | ".join(ctxs) + " |", "|---|---|" + "---|" * len(ctxs)]
            label = {r["case"]: r["label"] for r in part}
            for cid in sorted(label):
                cells = []
                for ctx in ctxs:
                    v, flipped = summary[ctx]["per_case"].get(cid, ("—", False))
                    cells.append(v + (" ↯" if flipped else ""))
                lines.append(f"| `{cid}` | {label[cid]} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bair.evals")
    ap.add_argument("--cases", help="cases JSON (default: the packaged server-bot set)")
    ap.add_argument("--repo-dir", default=".", help="full-history checkout of the cases' repo")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--context", default="slice", help="comma list of full|slice|none")
    ap.add_argument("--models", default="", help="comma list of Claude model ids (default: the gate's own)")
    ap.add_argument("--split", default="all", choices=("dev", "heldout", "all"))
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", default="bair-eval.jsonl")
    args = ap.parse_args(argv)

    spec = _load_cases(args.cases)
    repo_dir = Path(args.repo_dir).resolve()
    cases = [c for c in spec["cases"] if args.split == "all" or c["split"] == args.split]
    contexts = [c.strip() for c in args.context.split(",") if c.strip()]
    models = [m.strip() for m in args.models.split(",") if m.strip()] or [gatekeep._claude_model(None)]
    prompt = load_prompt("gatekeep_system")
    meta = {
        "prompt_sha": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "bair_ref": os.environ.get("BAIR_REF", "unknown"),
    }

    work = Path(tempfile.mkdtemp(prefix="bair-eval-"))
    trees: dict[str, Path] = {}
    diffs: dict[str, str] = {}
    fixes: dict[str, list[str]] = {}
    setup_errors: dict[str, str] = {}
    try:
        # Per case: one unreachable SHA (a deleted branch, a force-push) must cost
        # that case, not the whole run and its report.
        for c in cases:
            tree = work / c["id"]
            try:
                _git(repo_dir, "worktree", "add", "--detach", "-q", str(tree), c["head"])
                trees[c["id"]] = tree
                diffs[c["id"]] = _git(tree, "diff", f"{c['base']}...{c['head']}")[:200_000]
                fixes[c["id"]] = (
                    _git(repo_dir, "diff-tree", "--no-commit-id", "--name-only", "-r", c["fix"]).split()
                    if c.get("fix")
                    else []
                )
            except subprocess.CalledProcessError as exc:
                setup_errors[c["id"]] = f"setup: {' '.join(exc.cmd[1:3])}: {(exc.stderr or '').strip()[:200]}"
                print(f"[setup] {c['id']}: {setup_errors[c['id']]}", file=sys.stderr)

        jobs = [(c, ctx, m, r) for c in cases for ctx in contexts for m in models for r in range(args.runs)]
        rows: list[dict] = []
        with ThreadPoolExecutor(max_workers=args.workers) as pool, open(args.out, "w", encoding="utf-8") as out:
            for c, ctx, m, r in [j for j in jobs if j[0]["id"] in setup_errors]:
                row = {**meta, "case": c["id"], "label": c["label"], "split": c["split"], "context": ctx,
                       "model": m, "config": f"{ctx}@{m}", "run": r, "verdict": "UNAVAILABLE",
                       "error": setup_errors[c["id"]]}
                rows.append(row)
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
            jobs = [j for j in jobs if j[0]["id"] not in setup_errors]
            futures = {
                pool.submit(_run_one, c, ctx, m, r, trees[c["id"]], diffs[c["id"]], spec["repo"], fixes[c["id"]], meta): (
                    c["id"], ctx, m, r
                )
                for c, ctx, m, r in jobs
            }
            for n, fut in enumerate(as_completed(futures), 1):
                cid, ctx, m, r = futures[fut]
                try:
                    row = fut.result()
                except Exception as exc:  # one broken case must not sink the whole run
                    case = next(c for c in cases if c["id"] == cid)
                    row = {**meta, "case": cid, "label": case["label"], "split": case["split"], "context": ctx,
                           "model": m, "config": f"{ctx}@{m}", "run": r, "verdict": "UNAVAILABLE",
                           "error": f"{type(exc).__name__}: {exc}"[:300]}
                rows.append(row)
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
                out.flush()
                print(f"[{n}/{len(jobs)}] {cid} {ctx}@{m} #{r}: {row['verdict']}{' (would_block)' if row.get('would_block') else ''}", file=sys.stderr)
    finally:
        for tree in trees.values():
            subprocess.run(["git", "worktree", "remove", "--force", str(tree)], cwd=repo_dir, capture_output=True)
        shutil.rmtree(work, ignore_errors=True)

    report = _report(rows, meta)
    print(report)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
