"""Resolve log time-window parameters from tool/planner/chat input."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any


def _parse_iso_to_unix(value: str) -> int | None:
    text = (value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return None


def resolve_log_time_range(params: dict[str, Any] | None) -> tuple[int, int | None, str]:
    """
    Returns (since_unix, until_unix|None, human-readable window label).
    Default window: last 1 hour.
    """
    p = params or {}
    now = int(time.time())
    max_span_seconds = 7 * 24 * 3600

    since: int | None = None
    if p.get("since_ts") is not None:
        try:
            since = int(float(p["since_ts"]))
        except (TypeError, ValueError):
            since = None
    elif p.get("since_iso"):
        since = _parse_iso_to_unix(str(p["since_iso"]))
    elif p.get("since_hours") is not None:
        try:
            hours = float(p["since_hours"])
            hours = max(0.25, min(hours, 168.0))
            since = int(now - hours * 3600)
        except (TypeError, ValueError):
            since = None

    if since is None:
        since = int(now - 3600)

    until: int | None = None
    if p.get("until_ts") is not None:
        try:
            until = int(float(p["until_ts"]))
        except (TypeError, ValueError):
            until = None
    elif p.get("until_iso"):
        until = _parse_iso_to_unix(str(p["until_iso"]))

    if until is not None and until < since:
        since, until = until, since

    if until is not None and (until - since) > max_span_seconds:
        since = until - max_span_seconds

    if until is None and (now - since) > max_span_seconds:
        since = now - max_span_seconds

    label = _format_window_label(since, until, now)
    return since, until, label


def _format_window_label(since: int, until: int | None, now: int) -> str:
    end = until if until is not None else now
    span_h = (end - since) / 3600
    if span_h < 1:
        return f"last {int(span_h * 60)} minutes"
    if span_h < 48:
        return f"last {span_h:.1f} hours"
    return f"last {span_h / 24:.1f} days"


def entry_unix(entry: dict[str, Any]) -> int | None:
    raw = entry.get("datetime") or entry.get("timestamp")
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    return _parse_iso_to_unix(str(raw))


def filter_log_entries(
    entries: list[dict[str, Any]],
    since: int,
    until: int | None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        ts = entry_unix(e)
        if ts is None:
            out.append(e)
            continue
        if ts < since:
            continue
        if until is not None and ts > until:
            continue
        out.append(e)
    return out


def entries_to_text(entries: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"{e.get('datetime', '')} [{e.get('type', '')}] {e.get('message', '')}"
        for e in entries
        if isinstance(e, dict)
    )
