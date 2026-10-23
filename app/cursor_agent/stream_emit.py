"""Map Cursor SDK run stream events to short UI status / text deltas."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

EmitFn = Callable[[str, dict[str, Any]], None]

# Throttle duplicate activity lines (grep/glob flood the UI otherwise).
_last_activity: str = ""


def _short(s: str, n: int = 72) -> str:
    t = (s or "").strip().replace("\n", " ")
    return t if len(t) <= n else t[: n - 1] + "…"


def _tool_activity(name: str) -> str:
    n = (name or "tool").lower()
    if "grep" in n or "search" in n or "ripgrep" in n:
        return "Searching the codebase…"
    if "glob" in n or "list_dir" in n or "ls" == n:
        return "Scanning project files…"
    if "read" in n:
        return "Reading project files…"
    if "write" in n or "edit" in n or "patch" in n or "str_replace" in n:
        return "Editing files…"
    if "shell" in n or "terminal" in n or "bash" in n:
        return "Running a shell command…"
    if "test" in n or "build" in n or "npm" in n:
        return "Running build/tests…"
    return f"Working ({name})…"


def _emit_activity(emit: EmitFn, text: str) -> None:
    global _last_activity
    t = _short(text, 80)
    if not t or t == _last_activity:
        return
    _last_activity = t
    emit("activity", {"text": t})


def reset_stream_emit_state() -> None:
    global _last_activity
    _last_activity = ""


def emit_stream_event(ev: Any, emit: EmitFn) -> None:
    kind = getattr(ev, "kind", "")
    if kind == "sdk_message":
        msg = getattr(ev, "sdk_message", None)
        if msg is None:
            return
        mtype = getattr(msg, "type", "")
        if mtype == "assistant":
            content = getattr(getattr(msg, "message", None), "content", ())
            for block in content:
                text = getattr(block, "text", "")
                if text:
                    emit("delta", {"text": text})
        elif mtype == "tool_call":
            _emit_activity(emit, _tool_activity(getattr(msg, "name", "") or "tool"))
        elif mtype == "status":
            line = getattr(msg, "message", "") or getattr(msg, "status", "")
            if line:
                _emit_activity(emit, line)
        elif mtype == "thinking":
            _emit_activity(emit, "Thinking…")
        elif mtype == "task":
            line = getattr(msg, "text", "") or getattr(msg, "status", "")
            if line:
                _emit_activity(emit, line)
    elif kind == "step":
        step = getattr(ev, "step", None)
        if step is not None:
            label = str(getattr(step, "type", "") or getattr(step, "kind", "") or "")
            if label and label.lower() not in ("message", "assistant", "user"):
                _emit_activity(emit, _tool_activity(label))


def consume_run(run: Any, emit: EmitFn | None) -> Any:
    """Drain run events (optional), then return RunResult from wait()."""
    if emit is not None:
        reset_stream_emit_state()
        for ev in run.events():
            emit_stream_event(ev, emit)
    result = run.wait()
    if emit is not None:
        text = (getattr(result, "result", None) or "").strip()
        if text:
            emit("final", {"text": text})
    return result
