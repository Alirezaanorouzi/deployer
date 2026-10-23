"""Normalize Cursor SDK RunResult into CursorFixResult."""

from __future__ import annotations

from typing import Any

from app.cursor_agent.client import CursorFixResult


def fix_result_from_run(result: Any) -> CursorFixResult:
    text = (getattr(result, "result", None) or "").strip()
    status = str(getattr(result, "status", "") or "unknown")
    run_id_out = getattr(result, "id", None)
    error_code = getattr(result, "error_code", None)
    error_message = (getattr(result, "error_message", None) or "").strip()

    if status == "error":
        detail = error_message or text or "Cursor run failed"
        if error_code:
            detail = f"{error_code}: {detail}"
        return CursorFixResult(
            False,
            status,
            detail,
            run_id=str(run_id_out) if run_id_out else None,
            error=error_code or "RUN_ERROR",
        )
    git = getattr(result, "git", None)
    branches = getattr(git, "branches", ()) if git else ()
    extras: list[str] = []
    for b in branches:
        pr = (getattr(b, "pr_url", None) or "").strip()
        if pr:
            extras.append(f"PR: {pr}")
    summary = text or "(no text)"
    if extras:
        summary = summary + "\n\n" + "\n".join(extras)
    return CursorFixResult(
        True,
        status,
        summary,
        run_id=str(run_id_out) if run_id_out else None,
    )
