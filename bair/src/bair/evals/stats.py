"""Scoring for the eval suite: majority verdicts, per-class rates with Wilson
intervals, flip rate, and paired discordance between two configurations.

Pure stdlib, no LLM, so the arithmetic is unit-tested on its own.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable

Z95 = 1.959963984540054


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    """95% Wilson score interval for k successes in n trials (valid at small n,
    unlike the normal approximation)."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def outcome(row: dict) -> str:
    """One run's effective verdict: a shadow-mode BLOCK counts as BLOCK."""
    if row.get("would_block"):
        return "BLOCK"
    return row.get("verdict", "UNAVAILABLE")


def majority(rows: Iterable[dict]) -> tuple[str, bool]:
    """(majority effective verdict, flipped) over a case's runs. Abstentions are
    dropped; ties break toward the stricter verdict."""
    votes = Counter(outcome(r) for r in rows if outcome(r) != "UNAVAILABLE")
    if not votes:
        return ("UNAVAILABLE", False)
    rank = {"APPROVE": 0, "WARN": 1, "BLOCK": 2}
    top = max(votes.items(), key=lambda kv: (kv[1], rank.get(kv[0], -1)))[0]
    return (top, len(votes) > 1)


def summarize(rows: list[dict]) -> dict[str, dict]:
    """Per context mode: majority per case, then recall/false-block/flip counts."""
    by_ctx: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    labels: dict[str, str] = {}
    for r in rows:
        by_ctx[r.get("config") or r["context"]][r["case"]].append(r)
        labels[r["case"]] = r["label"]
    out: dict[str, dict] = {}
    for ctx, cases in by_ctx.items():
        per_case = {cid: majority(rs) for cid, rs in cases.items()}
        defects = [c for c in per_case if labels[c] == "defect" and per_case[c][0] != "UNAVAILABLE"]
        cleans = [c for c in per_case if labels[c] == "clean" and per_case[c][0] != "UNAVAILABLE"]
        blocked = [c for c in defects if per_case[c][0] == "BLOCK"]
        flagged = [c for c in defects if per_case[c][0] in ("BLOCK", "WARN")]
        false_block = [c for c in cleans if per_case[c][0] == "BLOCK"]
        false_alarm = [c for c in cleans if per_case[c][0] in ("BLOCK", "WARN")]
        flips = [c for c, (_, flipped) in per_case.items() if flipped]
        located = [c for c in defects if any(r.get("localized") for r in cases[c])]
        out[ctx] = {
            "per_case": per_case,
            "defects": len(defects),
            "cleans": len(cleans),
            "blocked": blocked,
            "flagged": flagged,
            "false_block": false_block,
            "false_alarm": false_alarm,
            "flips": flips,
            "localized": located,
            "unavailable": [c for c, (v, _) in per_case.items() if v == "UNAVAILABLE"],
        }
    return out


def discordant(summary: dict[str, dict], a: str, b: str) -> list[tuple[str, str, str]]:
    """Cases whose majority verdict differs between configurations ``a`` and ``b``."""
    pa, pb = summary[a]["per_case"], summary[b]["per_case"]
    return [(c, pa[c][0], pb[c][0]) for c in sorted(set(pa) & set(pb)) if pa[c][0] != pb[c][0]]
