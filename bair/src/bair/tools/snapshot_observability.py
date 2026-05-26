"""Snapshot Core 1.0 -> 2.0 migration/access state to a static JSON.

Boring, server-side, read-only. Runs in GitHub Actions (where DB creds and
the HMAC salt live as secrets), aggregates a handful of counts from MongoDB,
anonymizes org identifiers with HMAC, and writes a small static file the
Brython dashboard fetches same-origin from `data/observability.json`.

Mirrors the pattern of `bair.tools.snapshot_plane` (same writer convention,
same Pages deploy). Python on purpose: this repo's snapshot tooling is
Python; adding a Node toolchain just for this would be accidental overbuild.

VERIFIED REALITY (2026-05-20, against prod `bernard-org` DB) that shapes V1:
  * `organisations` has NO core-version flag. v1/v2 is inferred from activity,
    never read from a field.
  * `drafts.data.productVersion` is ABSENT (0 / 14907 docs). The Drafts
    2.0 vs 2.1 split is NOT derivable -> diagnostics.productVersionResolved
    stays False and the split is reported null, never faked.
  * No confirmed Core 1.0 runtime log group with parseable org id. Per-org
    *v1* usage is therefore unavailable -> diagnostics.perOrgVersion =
    "unavailable". CloudWatch is intentionally NOT queried in V1.

What V1 honestly answers: total orgs, who is active vs dormant in the last
N days, the active-org trend over time, and the dormant list for outreach.
Version-split fields are present in the schema but null until a real
discriminator exists (productVersion backfill or a parseable v1 access log).

Output: `.github/scripts/bair/frontend/data/observability.json`
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone

from pymongo import MongoClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from xair.log import logger

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
_OUTPUT = _REPO_ROOT / ".github/scripts/bair/frontend/data/observability.json"

_DB_NAME = "bernard-org"
_WINDOW_DAYS = 30
_TREND_WEEKS = 8
_SCHEMA_VERSION = 1
# k-anonymity: counts below this are rounded to the nearest 10 so small orgs
# can't be re-identified by an exact, distinctive number.
_KANON_FLOOR = 10


def _org_key(org_uuid: str, salt: str) -> str:
    """Stable, non-reversible 8-hex org identifier. Salt lives in a secret.

    8 hex = 32 bits. With a few thousand orgs the birthday-collision risk is
    ~256x lower than the old 6-hex (24-bit) key — a collision would attach one
    org's V1/V2 signal to another, which an 'auditable' signal cannot tolerate."""
    digest = hmac.new(salt.encode(), org_uuid.encode(), hashlib.sha256).hexdigest()
    return digest[:8]


_SIGNALS_FILE = _OUTPUT.parent / "migration-signals.json"


def _load_migration_signals():
    """Load the log-derived V1/V2 signal artifact.

    Returns (orgKey->signal map, top-level meta). Missing file is a legitimate
    'no signal yet' state -> ({}, None). But a file that EXISTS and fails to
    parse is corruption: we raise instead of silently degrading every org to
    'unknown' (that silent degrade is exactly how the prod join broke and shipped
    unnoticed). The caller / validator then fails the deploy loudly."""
    if not _SIGNALS_FILE.exists():
        logger.info("migration-signals.json absent -> no V1/V2 classification this run")
        return {}, None
    d = json.loads(_SIGNALS_FILE.read_text(encoding="utf-8"))  # raises on corrupt -> loud
    sig_map = {s["orgKey"]: s.get("migrationSignal", "unknown") for s in d.get("signals", [])}
    meta = {
        "counts": d.get("counts", {}),
        "computedAt": d.get("computedAt"),
        "sources": d.get("sources", {}),
    }
    return sig_map, meta


def _round_small(n: int) -> int:
    """Round counts < floor to the nearest 10 (k-anonymity)."""
    if n >= _KANON_FLOOR:
        return n
    return round(n / 10) * 10


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d") if dt else None


def main() -> int:
    mongo_url = os.environ.get("OBS_MONGO_URL_READONLY")
    salt = os.environ.get("OBS_ORG_SALT")
    if not mongo_url:
        logger.error("OBS_MONGO_URL_READONLY not set; cannot snapshot")
        return 1
    if not salt:
        logger.error("OBS_ORG_SALT not set; refusing to emit un-salted org keys")
        return 1

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=_WINDOW_DAYS)

    client = MongoClient(mongo_url, serverSelectionTimeoutMS=10000)
    db = client[_DB_NAME]

    mongo_ok = True
    try:
        orgs_total = db.organisations.count_documents({})

        # Last draft activity per org within the window. Aggregated only —
        # we never pull raw draft rows.
        per_org = list(db.drafts.aggregate([
            {"$match": {"createdAt": {"$gte": window_start}}},
            {"$group": {
                "_id": "$organisationId",
                "draftCount": {"$sum": 1},
                "lastActivity": {"$max": "$createdAt"},
            }},
        ]))

        drafts_in_window = sum(r["draftCount"] for r in per_org)

        # Active-org trend by ISO week (distinct orgs that created >=1 draft).
        trend_start = now - timedelta(weeks=_TREND_WEEKS)
        trend_raw = list(db.drafts.aggregate([
            {"$match": {"createdAt": {"$gte": trend_start}}},
            {"$group": {
                "_id": {
                    "y": {"$isoWeekYear": "$createdAt"},
                    "w": {"$isoWeek": "$createdAt"},
                    "org": "$organisationId",
                },
            }},
            {"$group": {"_id": {"y": "$_id.y", "w": "$_id.w"}, "orgs": {"$sum": 1}}},
            {"$sort": {"_id.y": 1, "_id.w": 1}},
        ]))
    except Exception as exc:  # noqa: BLE001 — degrade, never crash the deploy
        logger.error(f"Mongo aggregation failed: {exc}")
        client.close()
        return 1
    finally:
        client.close()

    active_orgs = len(per_org)
    dormant_orgs = max(0, orgs_total - active_orgs)

    sig_map, sig_meta = _load_migration_signals()

    org_rows = []
    active_unknown = 0
    for r in sorted(per_org, key=lambda x: x["lastActivity"], reverse=True):
        uuid = r["_id"]
        if not uuid:
            continue
        key = _org_key(str(uuid), salt)
        # Join the log-derived V1/V2 signal; default 'unknown' if no signal.
        version = sig_map.get(key, "unknown")
        if version == "unknown":
            active_unknown += 1
        org_rows.append({
            "orgKey": key,
            "lastActivityAt": _iso(r["lastActivity"]),
            "draftCount30d": _round_small(r["draftCount"]),
            "migrationState": "active",
            "version": version,
        })

    # DEAD-JOIN GUARD: a signal artifact is present but it matched ZERO active
    # orgs -> the orgKey salts diverged. This is the exact failure that shipped
    # "147 insufficient" to prod. Refuse to emit it; fail loudly so the deploy
    # stops and an operator notices, instead of silently degrading to all-unknown.
    if sig_map and active_orgs > 0 and active_unknown == active_orgs:
        logger.error(
            f"DEAD JOIN: migration-signals has {len(sig_map)} signals but matched "
            f"0/{active_orgs} active orgs (orgKey salt mismatch?). Refusing to write."
        )
        return 1

    trend = [
        {"isoYear": t["_id"]["y"], "isoWeek": t["_id"]["w"], "activeOrgs": t["orgs"]}
        for t in trend_raw
    ]

    payload = {
        "schemaVersion": _SCHEMA_VERSION,
        "generatedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "windowDays": _WINDOW_DAYS,
        "sources": {
            "mongo": {"ok": mongo_ok, "queriedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ")},
            "cloudwatch": {"ok": bool(sig_meta), "via": "backfill_migration_signals -> migration-signals.json"},
        },
        "migration": {
            "orgsTotal": orgs_total,
            "orgsActive30d": active_orgs,
            "orgsDormant": dormant_orgs,
            # V1/V2 split from log presence (migration-signals.json). Global
            # log-derived counts; null only if the artifact is missing.
            "orgsV1Only": (sig_meta or {}).get("counts", {}).get("v1_only") if sig_meta else None,
            "orgsV2Only": (sig_meta or {}).get("counts", {}).get("v2_only") if sig_meta else None,
            "orgsBoth": (sig_meta or {}).get("counts", {}).get("both") if sig_meta else None,
            # Active orgs (draft activity) with no runtime-log signal yet.
            "orgsActiveUnknown": active_unknown if sig_meta else None,
        },
        "drafts": {
            "totalInWindow": drafts_in_window,
            "note": "draft-level 2.0/2.1 split not inferred (productVersion absent on drafts)",
        },
        "trend": trend,
        "orgs": org_rows,
        "diagnostics": {
            "productVersionResolved": bool(sig_meta),
            "perOrgVersion": "from_runtime_logs" if sig_meta else "pending_backfill",
            "migrationSignalSyncedAt": (sig_meta or {}).get("computedAt") if sig_meta else None,
            "signalSourceCoverage": (sig_meta or {}).get("sources", {}) if sig_meta else {},
            "rowCount": len(org_rows),
        },
    }

    _OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    _OUTPUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(
        f"Wrote {_OUTPUT} — orgsTotal={orgs_total} active={active_orgs} "
        f"dormant={dormant_orgs} drafts30d={drafts_in_window} trendWeeks={len(trend)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
