"""Validate an observability snapshot before it is committed/deployed.

Two jobs, both fail-closed (exit != 0 => the workflow does NOT commit, so the
last good JSON survives):

  1. Structural: parses, has the explicit versioned schema, is non-empty
     (orgsTotal > 0). Catches a silently-broken query that returns nothing
     (e.g. a Mongo schema change renaming `organisationId`).

  2. PII guard: scans the RAW serialized text for emails, UUIDs, and IPv4
     addresses. The snapshot must only ever contain HMAC org keys and
     aggregated counts. If a UUID or email leaks in, this blocks the deploy
     before it reaches public GitHub Pages (where git history is permanent).

Usage: python -m bair.tools.validate_observability <path-to-json>
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_IPV4 = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\b")

_REQUIRED_TOP = (
    "schemaVersion", "generatedAt", "windowDays", "sources",
    "migration", "drafts", "trend", "orgs", "diagnostics",
)


def validate(path: pathlib.Path) -> list[str]:
    errors: list[str] = []
    raw = path.read_text(encoding="utf-8")

    # --- PII guards on raw text (catch leaks regardless of structure) ---
    if _EMAIL.search(raw):
        errors.append("PII guard: email address found in snapshot")
    if _UUID.search(raw):
        errors.append("PII guard: UUID (org/supabase id) found — only HMAC keys allowed")
    if _IPV4.search(raw):
        errors.append("PII guard: IPv4 address found in snapshot")

    # --- Structural ---
    try:
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        return errors + [f"invalid JSON: {exc}"]

    for key in _REQUIRED_TOP:
        if key not in data:
            errors.append(f"missing required top-level key: {key}")

    if data.get("schemaVersion") != 1:
        errors.append(f"unexpected schemaVersion: {data.get('schemaVersion')!r}")

    mig = data.get("migration", {})
    if not isinstance(mig.get("orgsTotal"), int) or mig.get("orgsTotal", 0) <= 0:
        errors.append("migration.orgsTotal missing or <= 0 (empty/broken query?)")

    if not isinstance(data.get("orgs"), list):
        errors.append("orgs must be a list")

    # --- Dead-join guard ---
    # The per-org V1/V2 version comes from joining migration-signals.json by
    # orgKey (HMAC with a shared salt). If the salts diverge the join yields
    # zero matches and every active org silently becomes "unknown" — this is
    # what shipped "147 insufficient" to prod. Fail the deploy on a dead join.
    sig_path = path.parent / "migration-signals.json"
    if sig_path.exists():
        try:
            sig = json.loads(sig_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"migration-signals.json present but unparseable: {exc}")
            sig = None
        if sig and sig.get("signals"):
            sig_keys = {s.get("orgKey") for s in sig["signals"]}
            obs_keys = {o.get("orgKey") for o in data.get("orgs", [])}
            active = mig.get("orgsActive30d", 0) or 0
            overlap = len(sig_keys & obs_keys)
            if active > 0 and overlap == 0:
                errors.append(
                    f"DEAD JOIN: 0 orgKey overlap between observability ({len(obs_keys)} "
                    f"active) and migration-signals ({len(sig_keys)} signals) — salt mismatch"
                )

    return errors


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: validate_observability.py <path>", file=sys.stderr)
        return 2
    path = pathlib.Path(sys.argv[1])
    if not path.exists():
        print(f"file not found: {path}", file=sys.stderr)
        return 2

    errors = validate(path)
    if errors:
        for e in errors:
            print(f"INVALID: {e}", file=sys.stderr)
        return 1
    print(f"OK: {path} passed structural + PII validation")
    return 0


if __name__ == "__main__":
    sys.exit(main())
