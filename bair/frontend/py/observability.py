"""Migration Monitor — Brython renderer with evidence drilldown + ApexCharts.

Loads two JSON artifacts:
  data/observability.json     — activity snapshot (Mongo)
  data/migration-signals.json — per-org V1/V2 signal with evidence (CloudWatch)

Merges on orgKey (HMAC-salted, never the raw UUID). Renders in BI order:
  1. KPI cards
  2. Migration split chart (ApexCharts horizontal bar)
     + confidence summary + validation note
  3. Activity trend chart (ApexCharts line)
  4. Outreach table with expandable evidence drilldown per row
  5. Data Notes (bottom, collapsed)

Graceful degradation:
  * No real snapshot / fallback _fallback flag -> show mock.
  * migration-signals.json missing -> version = unknown/pending, no drilldown.
  * ApexCharts not loaded -> skip charts, keep all other UI.

No PII: orgKeys are HMAC, no UUID/email/IP ever rendered.
"""

import json

from browser import ajax, document, html, window

_DATA_URL = "data/observability-mock.json"
_FALLBACK_URL = "data/observability-mock.json"
_SIGNALS_URL = "data/migration-signals.json"

# ── Semantic colors ───────────────────────────────────────────────────
C_V2 = "#56d364"
C_V1 = "#e3873d"
C_BOTH = "#4493f8"
C_UNKNOWN = "#8b949e"
C_DORMANT = "#5a6069"
C_HIGH = "#56d364"
C_MEDIUM = "#e3b341"
C_LOW = "#e3873d"

_VERSION_LABEL = {
    "v1_only": "Likely V1 only",
    "v2_only": "Likely V2 only",
    "both": "Uses both",
    "unknown": "Insufficient signal",
}
_VERSION_COLOR = {
    "v1_only": C_V1, "v2_only": C_V2, "both": C_BOTH, "unknown": C_UNKNOWN,
}
_CONF_COLOR = {"high": C_HIGH, "medium": C_MEDIUM, "low": C_LOW, "unknown": C_UNKNOWN}
_ACTION = {
    "v1_only": "Migration candidate",
    "both": "Monitor transition",
    "v2_only": "Healthy on V2",
    "unknown": "Needs signal",
}
_SORT_RANK = {"v1_only": 0, "both": 1, "unknown": 2, "v2_only": 3}


# ── Fetch helpers ────────────────────────────────────────────────────

def boot():
    _fetch(_DATA_URL, _on_obs)


def _fetch(url, cb):
    req = ajax.Ajax()
    req.bind("complete", cb)
    req.open("GET", url, True)
    req.send()


def _on_obs(req):
    if req.status in (200, 0) and req.text:
        try:
            data = json.loads(req.text)
            if not data.get("_fallback"):
                def _on_sig(r):
                    sig_map, sig_meta = _parse_signals(r)
                    _render(data, sig_map, sig_meta, is_fallback=False)
                _fetch(_SIGNALS_URL, _on_sig)
                return
        except Exception:  # noqa: BLE001
            pass
    _fetch(_FALLBACK_URL, _on_fallback)


def _on_fallback(req):
    root = document["root"]
    if req.status not in (200, 0):
        root.clear()
        root <= html.DIV("No migration snapshot available yet.", Class="loading")
        return
    try:
        data = json.loads(req.text)
    except Exception as exc:  # noqa: BLE001
        root.clear()
        root <= html.DIV(f"Bad JSON: {exc}", Class="loading")
        return
    _render(data, {}, None, is_fallback=True)


def _parse_signals(req):
    """Parse migration-signals.json; return (orgKey->signal_record map, meta)."""
    if req.status not in (200, 0) or not req.text:
        return {}, None
    try:
        d = json.loads(req.text)
    except Exception:  # noqa: BLE001
        return {}, None
    sig_map = {s["orgKey"]: s for s in d.get("signals", [])}
    meta = {
        "counts": d.get("counts", {}),
        "confidenceCounts": d.get("confidenceCounts", {}),
        "computedAt": d.get("computedAt"),
        "sources": d.get("sources", {}),
        "windowDays": d.get("windowDays", 30),
    }
    return sig_map, meta


# ── Top-level renderer ───────────────────────────────────────────────

def _render(data, sig_map, sig_meta, is_fallback):
    root = document["root"]
    root.clear()

    gen = data.get("generatedAt") or data.get("generated_at", "")
    if "updated" in document:
        document["updated"].text = f"snapshot {gen}{' · MOCK/placeholder' if is_fallback else ''}"
    if is_fallback:
        root <= html.DIV(
            "⚠ No real snapshot yet — showing placeholder.",
            Class="mockup-banner",
        )

    mig = data.get("migration", {})
    has_signal = mig.get("orgsV1Only") is not None

    _render_kpis(root, mig, has_signal)
    # Charts side by side (monitor layout). Attach the row first so ApexCharts
    # renders into elements already in the DOM (correct width).
    chart_row = html.DIV(Class="chart-row")
    root <= chart_row
    _render_split(chart_row, mig, sig_meta, has_signal)
    _render_trend(chart_row, data.get("trend", []))
    _render_table(root, data.get("orgs", []), sig_map, (sig_meta or {}).get("windowDays", 30))
    _render_notes(root, data.get("diagnostics", {}), data.get("sources", {}), sig_meta)


# ── 1. KPI cards ─────────────────────────────────────────────────────

def _kpi(tiles, value, label, color="var(--muted)"):
    t = html.DIV(Class="tile")
    t <= html.DIV(str(value if value is not None else "—"), Class="n", style={"color": color})
    t <= html.DIV(label, Class="l")
    tiles <= t


def _render_kpis(root, mig, has_signal):
    sec = html.SECTION()
    sec <= html.DIV("Overview", Class="sec-title")
    tiles = html.DIV(Class="tiles")
    _kpi(tiles, mig.get("orgsTotal", 0), "Total orgs")
    _kpi(tiles, mig.get("orgsActive30d", 0), "Active (30d)", C_V2)
    _kpi(tiles, mig.get("orgsDormant", 0), "Dormant", C_DORMANT)
    if has_signal:
        _kpi(tiles, mig.get("orgsV1Only"), "V1 only", C_V1)
        _kpi(tiles, mig.get("orgsV2Only"), "V2 only", C_V2)
        _kpi(tiles, mig.get("orgsBoth"), "Uses both", C_BOTH)
        _kpi(tiles, mig.get("orgsActiveUnknown"), "Insufficient signal", C_UNKNOWN)
    sec <= tiles
    root <= sec


# ── 2. Migration split + confidence summary ──────────────────────────

def _has_apex():
    try:
        return window.ApexCharts is not None
    except Exception:  # noqa: BLE001
        return False


def _render_split(root, mig, sig_meta, has_signal):
    sec = html.SECTION()
    sec <= html.DIV("Migration split — inferred from runtime logs (30d)", Class="sec-title")
    if not has_signal:
        sec <= html.DIV(
            "Classification pending — migration signal not available yet.",
            Class="card", style={"color": "var(--muted)", "font-size": "13px",
                                 "padding": "14px 16px"},
        )
        root <= sec
        return

    v1 = mig.get("orgsV1Only", 0) or 0
    both = mig.get("orgsBoth", 0) or 0
    v2 = mig.get("orgsV2Only", 0) or 0
    unk = mig.get("orgsActiveUnknown", 0) or 0
    total = v1 + both + v2 + unk

    card = html.DIV(Class="card")
    holder = html.DIV(id="split-chart", style={"min-height": "230px"})
    card <= holder

    # Validation note
    note = html.DIV(style={"padding": "8px 0 4px 0", "color": "var(--muted)", "font-size": "12px"})
    note <= html.SPAN("Migration signal is inferred from runtime log presence by org UUID. ")
    note <= html.SPAN("Expand rows below to inspect evidence.")
    card <= note

    # Confidence summary (from sig_meta if available)
    cc = (sig_meta or {}).get("confidenceCounts", {})
    if cc:
        csum = html.DIV(style={"padding": "2px 0 10px 0", "font-size": "12px"})
        for conf, color in (("high", C_HIGH), ("medium", C_MEDIUM), ("low", C_LOW)):
            n = cc.get(conf, 0)
            sp = html.SPAN(f"{conf.title()}: {n}",
                           style={"color": color, "margin-right": "16px", "font-weight": "600"})
            csum <= sp
        card <= csum

    sec <= card
    root <= sec

    if not _has_apex():
        holder <= html.DIV("Chart library unavailable.", style={"color": "var(--muted)"})
        return

    def fmt(val, *args):
        try:
            pct = round(100 * val / total) if total else 0
        except Exception:  # noqa: BLE001
            pct = 0
        return f"{val} orgs ({pct}%)"

    options = {
        "chart": {"type": "bar", "height": 230, "background": "transparent",
                  "fontFamily": "inherit", "toolbar": {"show": False}},
        "theme": {"mode": "dark"},
        "plotOptions": {"bar": {"horizontal": True, "distributed": True,
                                "barHeight": "65%", "borderRadius": 4}},
        "colors": [C_V1, C_BOTH, C_V2, C_UNKNOWN],
        "series": [{"name": "orgs", "data": [v1, both, v2, unk]}],
        "xaxis": {"categories": ["Likely V1 only", "Uses both", "Likely V2 only",
                                 "Insufficient signal"],
                  "labels": {"style": {"colors": "#8b949e"}}},
        "yaxis": {"labels": {"style": {"colors": "#e6edf3"}}},
        "dataLabels": {"enabled": True, "style": {"colors": ["#06060b"]}},
        "legend": {"show": False},
        "grid": {"borderColor": "#1e2430"},
        "tooltip": {"theme": "dark", "y": {"formatter": fmt}},
    }
    window.ApexCharts.new(window.document.querySelector("#split-chart"), options).render()


# ── 3. Activity trend chart ──────────────────────────────────────────

def _render_trend(root, trend):
    if not trend:
        return
    sec = html.SECTION()
    sec <= html.DIV("Activity trend — weekly active orgs", Class="sec-title")
    card = html.DIV(Class="card")
    holder = html.DIV(id="trend-chart", style={"min-height": "280px"})
    card <= holder
    sec <= card
    root <= sec

    cats = [f"W{t.get('isoWeek', '')}" for t in trend]
    vals = [t.get("activeOrgs", 0) for t in trend]

    if not _has_apex():
        holder <= html.DIV("Chart library unavailable.", style={"color": "var(--muted)"})
        return

    def xtip(val, *args):
        return f"{val} — ISO week"

    def ytip(val, *args):
        return f"{val} active orgs"

    options = {
        "chart": {"type": "line", "height": 280, "background": "transparent",
                  "fontFamily": "inherit", "toolbar": {"show": False},
                  "parentHeightOffset": 0,
                  "offsetY": 4},
        "theme": {"mode": "dark"},
        "colors": [C_V2],
        "series": [{"name": "Active orgs", "data": vals}],
        "xaxis": {"categories": cats,
                  "title": {"text": "ISO week", "offsetY": 4,
                            "style": {"color": "#8b949e", "fontSize": "11px",
                                      "fontWeight": 400}},
                  "labels": {"style": {"colors": "#8b949e"}}},
        "yaxis": {"title": {"text": "Active orgs", "offsetX": -2,
                            "style": {"color": "#8b949e", "fontSize": "11px",
                                      "fontWeight": 400}},
                  "labels": {"style": {"colors": "#8b949e"}}},
        "stroke": {"curve": "smooth", "width": 3},
        "markers": {"size": 4},
        "dataLabels": {"enabled": False},
        "grid": {"borderColor": "#1e2430",
                 "padding": {"top": 6, "right": 12, "bottom": 6, "left": 6}},
        "tooltip": {"theme": "dark",
                    "x": {"formatter": xtip},
                    "y": {"formatter": ytip}},
    }
    window.ApexCharts.new(window.document.querySelector("#trend-chart"), options).render()


# ── 4. Outreach table with evidence drilldown ────────────────────────

# V1 = legacy surfaces (gen + drafts); V2 = Core 2.0. An org's V1 count is the
# SUM across all V1 log groups, not just gen — otherwise drafts.bernard-org.ai usage
# is invisible in the drilldown.
_V1_LOGS = ("/ecs/visalawgen_production", "/ecs/bernard-org-drafts-prod",
            "/ecs/bernard-org-drafts-standalone-prod")
_V2_LOGS = ("/ecs/bernard-org-v2-prod", "/ecs/bernard-org-v2-standalone-prod")


def _side_count(evidence, groups):
    """Sum hit counts across the given log groups; return (count, lastSeenAt)."""
    items = [e for e in evidence if any(g in e.get("logGroup", "") for g in groups)]
    total = sum(e.get("count", 0) for e in items)
    last = max((e.get("lastSeenAt", "") for e in items), default="")
    return total, last


def _explain(signal, evidence, window_days):
    """Human-readable classification rationale from evidence."""
    v1c, _ = _side_count(evidence, _V1_LOGS)
    v2c, _ = _side_count(evidence, _V2_LOGS)
    def ht(n): return f"{n} time{'s' if n != 1 else ''}"
    if signal == "v2_only":
        return (f"Classified as Likely V2 only because this org appeared in V2 runtime logs "
                f"{ht(v2c)} in the last {window_days} days and 0 times in V1 runtime logs.")
    if signal == "v1_only":
        return (f"Classified as Likely V1 only because this org appeared in V1 runtime logs "
                f"{ht(v1c)} in the last {window_days} days and 0 times in V2 runtime logs.")
    if signal == "both":
        return (f"Classified as Uses both because this org appeared in V1 runtime logs "
                f"{ht(v1c)} and V2 runtime logs {ht(v2c)} in the last {window_days} days.")
    return "No reliable runtime signal found for this org in the current window."


def _mk_toggle(detail_el):
    def _toggle(evt):
        cur = detail_el.style.display
        detail_el.style.display = "none" if cur != "none" else "table-row"
    return _toggle


def _render_table(root, orgs, sig_map, window_days):
    if not orgs:
        return
    sec = html.SECTION()
    sec <= html.DIV("Outreach — active orgs by priority", Class="sec-title")
    if sig_map:
        sec <= html.DIV("Click any row to expand evidence.",
                        style={"color": "var(--dim)", "font-size": "12px", "margin-bottom": "8px"})

    ordered = sorted(
        orgs,
        key=lambda o: (_SORT_RANK.get(o.get("version", "unknown"), 9),
                       -int(o.get("draftCount30d", 0) or 0)),
    )

    card = html.DIV(Class="card", style={"padding": "0"})
    tbl = html.TABLE()
    thead = html.THEAD()
    hr = html.TR()
    for h in ("Org", "Migration signal", "Confidence", "Last activity",
              "Draft activity (30d)", "Suggested action", "▶"):
        hr <= html.TH(h, style={"white-space": "nowrap"})
    thead <= hr
    tbl <= thead
    tbody = html.TBODY()

    for o in ordered:
        v = o.get("version", "unknown")
        ok = o.get("orgKey", "")
        sig_rec = sig_map.get(ok)
        conf = sig_rec["confidence"] if sig_rec else ("unknown" if v != "unknown" else "unknown")
        evidence = sig_rec.get("evidence", []) if sig_rec else []
        wd = sig_rec.get("windowDays", window_days) if sig_rec else window_days

        # ── Data row ──
        tr = html.TR(style={"cursor": "pointer" if sig_rec else "default"})
        tr <= html.TD(ok, style={"font-family": "'JetBrains Mono', monospace",
                                  "font-size": "12px"})
        sig_td = html.TD()
        sig_td <= html.SPAN(_VERSION_LABEL.get(v, "Insufficient signal"),
                             style={"color": _VERSION_COLOR.get(v, C_UNKNOWN),
                                    "font-weight": "600"})
        tr <= sig_td
        conf_td = html.TD()
        if sig_rec:
            conf_td <= html.SPAN(conf.title(),
                                  style={"color": _CONF_COLOR.get(conf, C_UNKNOWN),
                                         "font-size": "11px", "font-weight": "600"})
        else:
            conf_td <= html.SPAN("—", style={"color": "var(--dim)"})
        tr <= conf_td
        tr <= html.TD(o.get("lastActivityAt") or "—")
        tr <= html.TD(f"{o.get('draftCount30d', 0)} drafts")
        tr <= html.TD(_ACTION.get(v, "Needs signal"))
        tr <= html.TD("▶" if sig_rec else "", style={"color": "var(--dim)", "font-size": "11px"})

        # ── Evidence drilldown row (hidden by default) ──
        det_tr = html.TR(style={"display": "none", "background": "var(--surface)"})
        det_td = html.TD(colspan=7, style={"padding": "12px 16px"})

        if sig_rec:
            det_td <= html.DIV(
                _explain(v, evidence, wd),
                style={"color": "var(--text)", "margin-bottom": "10px",
                       "font-style": "italic", "font-size": "13px"},
            )
            grid = html.DIV(style={"display": "grid",
                                    "grid-template-columns": "auto auto",
                                    "gap": "4px 24px", "font-size": "12px",
                                    "color": "var(--muted)"})
            v1c, v1last = _side_count(evidence, _V1_LOGS)
            v2c, v2last = _side_count(evidence, _V2_LOGS)
            fields = [
                ("V1 log count (gen + drafts)", str(v1c)),
                ("V2 log count (Core 2.0)", str(v2c)),
                ("Last seen in V1", v1last or "not seen"),
                ("Last seen in V2", v2last or "not seen"),
                ("Window", f"{wd} days"),
                ("Confidence", conf.title()),
                ("Log groups", ", ".join(e.get("logGroup","?").split("/")[-1] for e in evidence) or "none"),
            ]
            for k, val in fields:
                grid <= html.SPAN(k, style={"color": "var(--dim)"})
                grid <= html.SPAN(val, style={"color": "var(--text)"})
            det_td <= grid
        else:
            det_td <= html.DIV("No runtime signal found for this org in the current window.",
                                style={"color": "var(--muted)", "font-size": "13px"})

        det_tr <= det_td
        tbody <= tr
        tbody <= det_tr

        if sig_rec:
            tr.bind("click", _mk_toggle(det_tr))

    tbl <= tbody
    card <= tbl
    sec <= card
    root <= sec


# ── 5. Data Notes ────────────────────────────────────────────────────

def _render_notes(root, diag, sources, sig_meta):
    mongo_ok = sources.get("mongo", {}).get("ok")
    resolved = diag.get("productVersionResolved")
    cov = diag.get("signalSourceCoverage", {}) or {}
    cc = (sig_meta or {}).get("confidenceCounts", {})
    notes = [
        "Terminology — V1 = legacy (gen.bernard-org.ai + drafts.bernard-org.ai). "
        "V2 = Core 2.0 (app.bernard-org.ai); Josh refers to V2 as \"Drafts 2.1\".",
        "Mongo snapshot: " + ("healthy." if mongo_ok else "unavailable."),
    ]
    if resolved:
        notes.append("Classifier source: CloudWatch runtime logs — org UUID presence. "
                     "V1 = /ecs/visalawgen_production + /ecs/bernard-org-drafts-prod; "
                     "V2 = /ecs/bernard-org-v2-prod.")
        notes.append(
            "Secondary signal: not available. "
            "Current classification is based on runtime log presence only. "
            "Secondary confirmation signal pending (e.g. productVersion field on drafts)."
        )
        if cc:
            notes.append(
                f"Confidence rules — high: ≥2 hits, zero cross-hits, recent (<14d); "
                f"medium: ≥2 hits stale, or 1 hit recent; low: 1 hit stale. "
                f"Distribution: high {cc.get('high',0)}, "
                f"medium {cc.get('medium',0)}, low {cc.get('low',0)}."
            )
        v1c, v2c = cov.get("v1", {}), cov.get("v2", {})
        notes.append(f"Coverage — V1 ({len(v1c.get('logGroups', []))} log groups): "
                     f"{v1c.get('orgsSeen', 0)} orgs; "
                     f"V2: {v2c.get('orgsSeen', 0)} orgs.")
    else:
        notes.append("Runtime signal not available yet — showing activity only.")
        notes.append("Secondary signal: pending.")
    notes.append("PII guard: HMAC org keys only. No UUID, email, or IP exposed.")
    notes.append(f"Active orgs listed: {diag.get('rowCount', 0)} (last 30 days).")

    sec = html.SECTION(style={"margin-top": "8px"})
    det = html.DETAILS(style={"color": "var(--muted)", "font-size": "12px"})
    det <= html.SUMMARY("Data Notes", style={"cursor": "pointer", "color": "var(--muted)"})
    inner = html.DIV(Class="card", style={"margin-top": "8px"})
    for n in notes:
        inner <= html.DIV(n, style={"line-height": "1.7"})
    det <= inner
    sec <= det
    root <= sec
