"""SSE streaming wrapper for Cursor chat turns."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.orm import Session

from app.cursor_agent.client import cursor_api_key_issue
from app.terminal_log import resolve_deploy_log_for_cursor
from app.cursor_agent.runtime import cursor_runtime_mode
from app.cursor_agent.sessions import (
    _chat_turn_sync,
    ensure_workspace,
    get_or_create_session,
)


async def stream_chat_events(
    message: str,
    *,
    session_id: str | None = None,
    run_id: str | None = None,
    repo: str | None = None,
    deploy_log: str | None = None,
    include_terminal_log: bool = False,
    db: Session | None = None,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    key_issue = cursor_api_key_issue()
    if key_issue:
        yield "error", {"text": key_issue, "session_id": session_id or ""}
        yield "done", {"ok": False, "error": key_issue, "session_id": session_id or "", "reply": ""}
        return

    session = get_or_create_session(session_id)
    mode = cursor_runtime_mode()

    if mode == "local":
        if repo:
            session.repo = repo
        try:
            await ensure_workspace(session, run_id=run_id, repo=repo or session.repo)
        except Exception as e:
            yield "error", {"text": str(e)}
            yield "done", {
                "ok": False,
                "session_id": session.session_id,
                "error": str(e),
                "reply": "",
            }
            return
        if not (session.cwd or "").strip():
            err = (
                "No workspace: pick a repository in the toolbar (top left), "
                "or run a deploy so a clone exists. If the repo list is empty, "
                "check GITHUB_TOKEN in .env and restart the server."
            )
            yield "error", {"text": err}
            yield "done", {
                "ok": False,
                "session_id": session.session_id,
                "error": "NO_WORKSPACE",
                "reply": err,
            }
            return
    else:
        if repo:
            session.repo = repo
        if not session.repo:
            err = "Select a GitHub repository for Cursor Cloud."
            yield "error", {"text": err}
            yield "done", {
                "ok": False,
                "session_id": session.session_id,
                "error": err,
                "reply": "",
            }
            return

    merged_log = resolve_deploy_log_for_cursor(
        deploy_log=deploy_log,
        run_id=run_id,
        include_terminal_log=include_terminal_log,
        db=db,
    )
    if merged_log:
        session.deploy_log = merged_log

    loop = asyncio.get_running_loop()
    event_queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()

    def emit(event: str, data: dict[str, Any]) -> None:
        loop.call_soon_threadsafe(event_queue.put_nowait, (event, data))

    from app.cursor_agent.bridge_client import run_on_sdk_thread

    async def worker():
        try:
            return await asyncio.wait_for(
                run_on_sdk_thread(
                    _chat_turn_sync,
                    session,
                    message.strip(),
                    deploy_log=merged_log or None,
                    run_id=run_id,
                    repo=repo,
                    emit=emit,
                ),
                timeout=900.0,
            )
        except asyncio.TimeoutError:
            emit("error", {"text": "Cursor run timed out after 15 minutes."})
            from app.cursor_agent.client import CursorFixResult

            return CursorFixResult(
                False,
                "timeout",
                "Cursor run timed out.",
                error="TIMEOUT",
            )
        except Exception as e:
            emit("error", {"text": str(e)})
            from app.cursor_agent.client import CursorFixResult

            return CursorFixResult(False, "error", str(e), error=str(e))
        finally:
            loop.call_soon_threadsafe(event_queue.put_nowait, None)

    task = asyncio.create_task(worker())

    yield "session", {"session_id": session.session_id, "runtime": mode}

    while True:
        item = await event_queue.get()
        if item is None:
            break
        yield item

    out = await task
    yield "done", {
        "ok": out.ok,
        "session_id": session.session_id,
        "status": out.status,
        "reply": out.summary,
        "workspace": session.cwd,
        "runtime": mode,
        "error": out.error,
    }
