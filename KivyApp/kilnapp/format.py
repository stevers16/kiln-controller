"""Shared display-formatting helpers used across multiple screens.

Anything here must be UI-flavour string formatting only -- no Kivy imports,
no I/O. Intended to deduplicate one-line helpers that drifted between
screens.
"""

from __future__ import annotations

from typing import Any, Dict


def format_size(b: int) -> str:
    """Render a byte count as B / KB / MB / GB."""
    if b < 1024:
        return f"{b} B"
    if b < 1024 * 1024:
        return f"{b / 1024:.1f} KB"
    if b < 1024 * 1024 * 1024:
        return f"{b / (1024 * 1024):.1f} MB"
    return f"{b / (1024 * 1024 * 1024):.2f} GB"


def format_run_label(run: Dict[str, Any]) -> str:
    """Friendly 1-line label for a run row.

    Prefers the formatted ended timestamp (from file mtime), falls back to
    the start date parsed from the rid, then to the raw rid. Every branch
    forces str() because Pi4 /runs returns integer primary keys and the
    Pico returns strings -- both must survive widget text properties.
    """
    ended = run.get("ended_at_str")
    if ended:
        return str(ended)
    started = run.get("started_at_str")
    if started and "-" in str(started):
        return str(started)
    rid = run.get("id")
    return str(rid) if rid is not None else "?"
