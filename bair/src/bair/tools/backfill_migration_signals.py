"""Backfill a per-org V1/V2 migration signal from CloudWatch log presence.

Auditable, honest, no PII. Classifies each org by where its UUID appears in
runtime logs over the window:

  * only in /ecs/visalawgen_production (Core 1.0)  -> v1_only
  * only in /ecs/bernard-org-v2-prod      (Core 2.0)  -> v2_only
  * in both                                        -> both
  * in neither (but active in Mongo)               -> unknown (decided downstream)

The org UUID NEVER leaves this script in the clear: it is HMAC'd to the same
6-hex orgKey the observability snapshot uses (shared OBS_ORG_SALT), so the two
artifacts join without ever exposing a UUID. Evidence is counts + lastSeenAt +
log group only.

Output: bair/frontend/data/migration-signals.json  (committed artifact)
This does NOT touch production Mongo. It is a read-only logs->JSON backfill.

Run locally:
    AWS_PROFILE=bernard OBS_ORG_SALT=... python -m bair.tools.backfill_migration_signals
In CI: needs logs:StartQuery/GetQueryResults on the two log groups + OBS_ORG_SALT.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import pathlib
import sys
import time
from datetime import datetime, timedelta, timezone

import boto3

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from xair.log import logger

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
_OUTPUT = _REPO_ROOT / ".github/scripts/bair/frontend/data/migration-signals.json"

_REGION = "us-east-1"
_WINDOW_DAYS = 30
# V1 = the legacy surfaces, per Jason: gen.bernard-org.ai + drafts.bernard-org.ai.
#   gen.bernard-org.ai      -> /ecs/visalawgen_production
#   drafts.bernard-org.ai   -> /ecs/bernard-org-drafts-prod  (legacy Drafts standalone)
# V2 = Core 2.0 (app.bernard-org.ai). Josh refers to V2 as "Drafts 2.1".
# An org counts as V1 if it appears in ANY V1 log group.
_V1_GROUPS = [
    "/ecs/visalawgen_production",          # gen.bernard-org.ai backend
    "/ecs/bernard-org-drafts-prod",            # drafts.bernard-org.ai backend
    "/ecs/bernard-org-drafts-standalone-prod",  # drafts legacy processing worker
]
_V2_GROUPS = [
    "/ecs/bernard-org-v2-prod",                # app.bernard-org.ai backend (Core 2.0)
    "/ecs/bernard-org-v2-standalone-prod",     # Core 2.0 processing worker
]

# Same org-UUID extraction used during signal discovery. Presence-only.
_QUERY = (
    r"filter @message like /(?i)org/ "
    r"| parse @message /(?i)(?:organisation|organization|org)(?:id)?[\"':\s]+"
    r"(?<org>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/ "
    r"| filter ispresent(org) "
    r"| stats count() as c, latest(@timestamp) as last by org "
    r"| sort c desc | limit 5000"
)


def _org_key(org_uuid: str, salt: str) -> str:
    # 8 hex (32 bits) — must match snapshot_observability._org_key so the join holds.
    return hmac.new(salt.encode(), org_uuid.encode(), hashlib.sha256).hexdigest()[:8]


def _run_query(client, group: str, start: int, end: int) -> dict[str, dict]:
    """Return {org_uuid: {count, lastMs}} for one log group."""
    qid = client.start_query(
        logGroupName=group, startTime=start, endTime=end, queryString=_QUERY, limit=5000,
    )["queryId"]
    while True:
        resp = client.get_query_results(queryId=qid)
        if resp["status"] in ("Complete", "Failed", "Cancelled", "Timeout"):
            break
        time.sleep(1)
    if resp["status"] != "Complete":
        raise RuntimeError(f"query on {group} ended {resp['status']}")
    out: dict[str, dict] = {}
    for row in resp["results"]:
        rec = {f["field"]: f["value"] for f in row}
        org = rec.get("org")
        if not org:
            continue
        out[org] = {"count": int(rec.get("c", 0)), "lastMs": int(rec.get("last", 0))}
    return out


def _iso_from_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_RECENT_MS = 14 * 24 * 3600 * 1000  # 14 days


def _confidence(signal: str, v1_data: dict | None, v2_data: dict | None, now_ms: int) -> str:
    """Multi-factor confidence.

    high   — multiple hits in classifying version, zero in the other, recent (<14d)
    medium — multiple hits but stale, OR single hit that is recent
    low    — single hit and stale, OR both-sides signal with weak counts
    """
    if signal == "both":
        c1 = v1_data["count"] if v1_data else 0
        c2 = v2_data["count"] if v2_data else 0
        # Recency matters here too: "uses both" is only high-confidence if both
        # sides are well-attested AND at least one side was seen recently.
        last = max(v1_data.get("lastMs", 0) if v1_data else 0,
                   v2_data.get("lastMs", 0) if v2_data else 0)
        recent = (now_ms - last) < _RECENT_MS
        return "high" if (c1 >= 3 and c2 >= 3 and recent) else "medium"

    primary = v2_data if signal == "v2_only" else v1_data
    cross = v1_data if signal == "v2_only" else v2_data

    count = primary["count"] if primary else 0
    last_ms = primary.get("lastMs", 0) if primary else 0
    is_recent = (now_ms - last_ms) < _RECENT_MS
    cross_count = cross["count"] if cross else 0  # 0 for pure v1/v2_only

    if count >= 2 and cross_count == 0 and is_recent:
        return "high"
    if count >= 2:          # multiple hits but stale
        return "medium"
    if count == 1 and is_recent:
        return "medium"
    return "low"            # single hit, stale — weak signal


def main() -> int:
    salt = os.environ.get("OBS_ORG_SALT")
    if not salt:
        logger.error("OBS_ORG_SALT not set; refusing to emit un-salted org keys")
        return 1

    now = datetime.now(timezone.utc)
    start_ms = int((now - timedelta(days=_WINDOW_DAYS)).timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)

    client = boto3.client("logs", region_name=_REGION)
    now_ms = int(now.timestamp() * 1000)

    # Query every group; keep per-group results for evidence, and a merged
    # per-side view (org -> {count: sum, lastMs: max}) for classification.
    per_group: dict[str, dict] = {}
    for g in _V1_GROUPS + _V2_GROUPS:
        logger.info(f"querying {g} ...")
        per_group[g] = _run_query(client, g, start_ms, end_ms)

    def _merge(groups: list[str]) -> dict[str, dict]:
        m: dict[str, dict] = {}
        for g in groups:
            for org, d in per_group[g].items():
                cur = m.setdefault(org, {"count": 0, "lastMs": 0})
                cur["count"] += d["count"]
                cur["lastMs"] = max(cur["lastMs"], d["lastMs"])
        return m

    v1 = _merge(_V1_GROUPS)  # appears in ANY V1 surface (gen OR drafts)
    v2 = _merge(_V2_GROUPS)

    counts = {"v1_only": 0, "v2_only": 0, "both": 0}
    signals = []
    for org in sorted(set(v1) | set(v2)):
        in1, in2 = org in v1, org in v2
        signal = "both" if (in1 and in2) else ("v1_only" if in1 else "v2_only")
        counts[signal] += 1
        # Evidence: one entry per log group the org actually appeared in.
        evidence = []
        for g in _V1_GROUPS + _V2_GROUPS:
            d = per_group[g].get(org)
            if d:
                evidence.append({"source": "cloudwatch", "logGroup": g,
                                 "count": d["count"], "lastSeenAt": _iso_from_ms(d["lastMs"])})
        conf = _confidence(signal, v1.get(org) if in1 else None,
                           v2.get(org) if in2 else None, now_ms)
        signals.append({
            "orgKey": _org_key(org, salt),
            "migrationSignal": signal,
            "confidence": conf,
            "windowDays": _WINDOW_DAYS,
            "evidence": evidence,
        })

    payload = {
        "schemaVersion": 1,
        "computedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "windowDays": _WINDOW_DAYS,
        "sources": {
            "v1": {"logGroups": _V1_GROUPS, "orgsSeen": len(v1)},
            "v2": {"logGroups": _V2_GROUPS, "orgsSeen": len(v2)},
        },
        "counts": counts,
        "confidenceCounts": {
            "high": sum(1 for s in signals if s["confidence"] == "high"),
            "medium": sum(1 for s in signals if s["confidence"] == "medium"),
            "low": sum(1 for s in signals if s["confidence"] == "low"),
        },
        "signals": signals,
    }

    # PII guard — never let a UUID/email/IP reach the committed artifact.
    blob = json.dumps(payload)
    import re
    if re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", blob):
        logger.error("PII guard: UUID found in payload; refusing to write")
        return 1
    if re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", blob):
        logger.error("PII guard: email found in payload; refusing to write")
        return 1

    _OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    _OUTPUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(
        f"Wrote {_OUTPUT} — v1_only={counts['v1_only']} v2_only={counts['v2_only']} "
        f"both={counts['both']} (v1 orgs seen={len(v1)}, v2 orgs seen={len(v2)})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
