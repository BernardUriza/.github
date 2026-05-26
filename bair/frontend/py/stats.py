"""Stat cards — render and update from runs data."""

from browser import document, html
from datetime import datetime


def calc_duration_secs(run):
    """Return duration in seconds for a run dict, or None if unavailable."""
    started = run.get("run_started_at")
    updated = run.get("updated_at")
    if not started or not updated:
        return None
    try:
        s = datetime.fromisoformat(started.replace("Z", "+00:00"))
        e = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        return int((e - s).total_seconds())
    except Exception:
        return None


def fmt_duration(secs):
    """Format seconds as 'Xm Ys' or 'Xs'."""
    if secs is None:
        return "\u2014"
    return f"{secs // 60}m {secs % 60}s" if secs >= 60 else f"{secs}s"


def render_stats():
    """Create skeleton stat cards with loading state."""
    for card_id, label in [("stat-total", "Total Runs"), ("stat-success", "Success Rate"),
                           ("stat-time", "Avg Duration"), ("stat-active", "Active Now")]:
        card = document[card_id]
        card.clear()
        card <= html.DIV(label, Class="stat-label")
        card <= html.DIV("\u2014", Class="stat-value stat-value-text", id=f"{card_id}-val")
        card <= html.DIV("loading...", Class="stat-sub", id=f"{card_id}-sub")


def update_stats(runs):
    """Compute and display stats from a list of workflow runs."""
    total = len(runs)
    successes = sum(1 for r in runs if r.get("conclusion") == "success")
    failures = sum(1 for r in runs if r.get("conclusion") == "failure")
    active = sum(1 for r in runs if r.get("status") == "in_progress")

    rate = f"{int(successes / total * 100)}%" if total else "0%"

    durations = [d for r in runs if (d := calc_duration_secs(r)) is not None]
    avg = int(sum(durations) / len(durations)) if durations else 0
    avg_str = fmt_duration(avg)

    document["stat-total-val"].text = str(total)
    document["stat-total-val"].classList.add("stat-value-purple")
    document["stat-total-sub"].text = f"{failures} failed"

    document["stat-success-val"].text = rate
    document["stat-success-val"].classList.add("stat-value-green")
    document["stat-success-sub"].text = f"{successes}/{total}"

    document["stat-time-val"].text = avg_str
    document["stat-time-val"].classList.add("stat-value-text")
    document["stat-time-sub"].text = f"across {len(durations)} runs"

    document["stat-active-val"].text = str(active)
    if active > 0:
        document["stat-active-val"].classList.add("stat-value-yellow")
        document["stat-active-sub"].text = "running now"
    else:
        document["stat-active-val"].classList.add("stat-value-text")
        document["stat-active-sub"].text = "all idle"
