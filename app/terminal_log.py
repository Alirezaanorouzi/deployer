"""Authoritative in-process terminal log buffers per deploy run."""

from __future__ import annotations

import threading
from collections import defaultdict

from sqlalchemy.orm import Session

MAX_LINES_PER_RUN = 8000
MAX_CHARS = 100_000

_lock = threading.Lock()
_lines: dict[str, list[str]] = defaultdict(list)


def append(run_id: str, line: str) -> None:
    text = (line or "").rstrip("\n")
    if not text:
        return
    key = (run_id or "").strip() or "__global__"
    with _lock:
        buf = _lines[key]
        buf.append(text)
        while len(buf) > MAX_LINES_PER_RUN:
            buf.pop(0)
        total = sum(len(x) + 1 for x in buf)
        while buf and total > MAX_CHARS:
            total -= len(buf[0]) + 1
            buf.pop(0)


def line_from_stream_event(event: str, data: dict | None) -> str | None:
    if not data:
        return None
    if event == "log_line":
        return data.get("line")
    if event == "step_start":
        name = data.get("name") or ""
        return f">>> step {name}" if name else None
    if event == "tool_call":
        tid = data.get("tool_id") or ""
        return f">>> tool {tid}" if tid else None
    if event == "run_error":
        msg = (data.get("message") or "").strip()
        return f"*** RUN ERROR: {msg}" if msg else "*** RUN ERROR"
    if event == "agent_message":
        if data.get("stream"):
            return None
        text = (data.get("text") or "").strip()
        return f"Agent: {text}" if text else None
    if event == "agent_thinking":
        text = (data.get("text") or "").strip()
        return f"… {text}" if text else None
    if event == "approval_required":
        msg = (data.get("message") or "").strip()
        return f"*** APPROVAL REQUIRED: {msg} ***" if msg else "*** APPROVAL REQUIRED ***"
    if event == "succeeded":
        summary = (data.get("summary") or "").strip()
        if summary:
            return f"--- succeeded --- {summary}"
        return "--- succeeded ---"
    return None


def buffer_text(run_id: str | None) -> str:
    key = (run_id or "").strip()
    if not key:
        return ""
    with _lock:
        lines = list(_lines.get(key, []))
    return "\n".join(lines)


def _db_supplement(run_id: str, db: Session) -> str:
    from app.models import Run, RunStep

    parts: list[str] = []
    run = db.get(Run, run_id)
    if run and run.error_message:
        parts.append(f"--- run error ---\n{run.error_message.strip()}")
    if run and run.result_summary:
        parts.append(f"--- result ---\n{run.result_summary.strip()}")
    steps = (
        db.query(RunStep)
        .filter(RunStep.run_id == run_id)
        .order_by(RunStep.seq)
        .all()
    )
    for step in steps:
        if step.output and step.output.strip():
            parts.append(
                f"--- step {step.name} ({step.status}) ---\n{step.output.strip()}"
            )
    return "\n\n".join(parts)


def get_terminal_log_text(
    run_id: str | None,
    db: Session | None = None,
    *,
    max_chars: int = 50_000,
) -> tuple[str, int, bool]:
    """Return (text, line_count, truncated)."""
    live = buffer_text(run_id)
    extra = _db_supplement(run_id, db) if run_id and db else ""
    if live and extra:
        combined = live + "\n\n" + extra
    else:
        combined = live or extra
    if not run_id:
        with _lock:
            global_lines = list(_lines.get("__global__", []))
        if global_lines:
            g = "\n".join(global_lines)
            combined = (combined + "\n" + g).strip() if combined else g
    combined = combined.strip()
    line_count = len(combined.splitlines()) if combined else 0
    truncated = len(combined) > max_chars
    if truncated:
        combined = combined[-max_chars:]
    return combined, line_count, truncated


def resolve_deploy_log_for_cursor(
    *,
    deploy_log: str | None,
    run_id: str | None,
    include_terminal_log: bool,
    db: Session | None,
) -> str:
    parts: list[str] = []
    if include_terminal_log:
        server, _, _ = get_terminal_log_text(run_id, db)
        if server:
            parts.append(server)
    client = (deploy_log or "").strip()
    if client:
        if not parts or client not in parts[0]:
            parts.append(client)
    merged = "\n\n".join(parts).strip()
    # Cursor agent: prefer log tail (errors are usually at the end).
    cursor_tail = 12_000
    if len(merged) > cursor_tail:
        merged = (
            "[Terminal log truncated — showing the last ~12k characters]\n"
            + merged[-cursor_tail:]
        )
    return merged
