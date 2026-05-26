"""Bento grid orchestrator — renders all panels at once."""

from .stats import render_stats
from .runs import render_runs_panel, load_runs
from .dispatch import render_dispatch_resolve, render_dispatch_review
from .pulls import render_pulls_panel, load_pulls
from .chat import render_chat


def render_bento():
    """Render all bento panels and kick off data loading."""
    render_stats()
    render_runs_panel()
    render_dispatch_resolve()
    render_dispatch_review()
    render_pulls_panel()
    render_chat()
    load_runs()
    load_pulls()
