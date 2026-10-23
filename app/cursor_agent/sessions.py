"""In-memory Cursor SDK chat sessions (resume by agent id between HTTP calls)."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass

from app.config import get_settings
from app.cursor_agent.client import CursorFixResult, cursor_api_key_issue
from app.cursor_agent.runtime import (
    bridge_workspace_key,
    bridge_workspace_path,
    build_agent_options,
    cursor_runtime_mode,
    workspace_label,
)
from app.repo.paths import repo_clone_dir


@dataclass
class CursorSession:
    session_id: str
    agent_id: str | None = None
    cwd: str = ""
    repo: str | None = None
    deploy_log: str = ""
    workspace_run_id: str | None = None


_lock = threading.Lock()
_sessions: dict[str, CursorSession] = {}


def _system_preamble(deploy_log: str, repo: str | None, *, cloud: bool) -> str:
    if cloud:
        parts = [
            "You are the coding agent for a Liara deployer user. "
            "Cursor Cloud has cloned the GitHub repository for this session. "
            "Inspect the repo, fix issues, run build/tests in the cloud environment, "
            "and commit, push, or open a PR when appropriate. "
            "Do not deploy to Liara unless explicitly asked.",
        ]
    else:
        parts = [
            "You are the coding agent for a Liara deployer workspace. "
            "Edit files in cwd, run builds/tests, use git when the user asks (commit, push, PR). "
            "Do not deploy to Liara unless explicitly asked.",
        ]
    if repo:
        parts.append(f"GitHub repo: {repo}")
    log = (deploy_log or "").strip()
    if log:
        parts.append(
            "Deployment / build log (focus on ERROR lines; minimal fixes; "
            "explain root cause and changes clearly):\n---\n"
            + log[:12000]
            + "\n---"
        )
    return "\n\n".join(parts)


def _resolve_cwd(run_id: str | None, repo: str | None, session: CursorSession) -> str:
    if run_id:
        path = repo_clone_dir(run_id)
        if path.is_dir():
            session.workspace_run_id = run_id
            return str(path.resolve())
    if session.cwd and session.workspace_run_id:
        p = repo_clone_dir(session.workspace_run_id)
        if p.is_dir():
            return session.cwd
    if repo:
        slug = repo.replace("/", "-")
        ws_run = f"cursor-ui-{slug}"
        path = repo_clone_dir(ws_run)
        if path.is_dir():
            session.workspace_run_id = ws_run
            return str(path.resolve())
    return ""


async def ensure_workspace(
    session: CursorSession,
    *,
    run_id: str | None,
    repo: str | None,
) -> str:
    cwd = _resolve_cwd(run_id, repo or session.repo, session)
    if cwd:
        session.cwd = cwd
        return cwd
    r = repo or session.repo
    if not r:
        return ""
    slug = r.replace("/", "-")
    ws_run = session.workspace_run_id or f"cursor-ui-{slug}"
    session.workspace_run_id = ws_run
    path = repo_clone_dir(ws_run)
    if not path.is_dir():
        from app.repo.workspace import clone_repository

        await clone_repository(ws_run, r, github_repos=[r])
    if not path.is_dir():
        raise RuntimeError(f"Clone failed for {r}")
    cwd = str(path.resolve())
    session.cwd = cwd
    return cwd


def _chat_turn_sync(
    session: CursorSession,
    user_message: str,
    *,
    deploy_log: str | None,
    run_id: str | None,
    repo: str | None,
    emit=None,
) -> CursorFixResult:
    from cursor_sdk import Agent, AgentOptions, CursorAgentError

    from app.cursor_agent.bridge_client import client_for_cwd
    from app.cursor_agent.run_result import fix_result_from_run
    from app.cursor_agent.stream_emit import consume_run

    settings = get_settings()
    api_key = (settings.cursor_api_key or "").strip()
    if not api_key:
        return CursorFixResult(False, "skipped", "CURSOR_API_KEY not set", error="CONFIG")

    if deploy_log is not None:
        session.deploy_log = deploy_log
    if repo:
        session.repo = repo

    mode = cursor_runtime_mode()
    local_cwd = session.cwd

    if mode == "local":
        if not local_cwd:
            return CursorFixResult(
                False,
                "skipped",
                "No workspace: run a deploy first or select a GitHub repo.",
                error="NO_WORKSPACE",
            )
    elif not session.repo:
        return CursorFixResult(
            False,
            "skipped",
            "Select a GitHub repository. Cloud agents clone via Cursor GitHub integration.",
            error="NO_REPO",
        )

    try:
        opts = build_agent_options(
            api_key=api_key,
            model_id=settings.cursor_sdk_model,
            local_cwd=local_cwd,
            repo=session.repo,
        )
    except ValueError as e:
        return CursorFixResult(False, "skipped", str(e), error="CONFIG")

    cache_key = bridge_workspace_key(mode, local_cwd or "")
    launch = bridge_workspace_path(mode, local_cwd or "")

    def _status(text: str) -> None:
        if emit:
            emit("status", {"text": text})

    try:
        _status("Connecting to Cursor…")
        sdk_client = client_for_cwd(cache_key, launch_workspace=launch)
        if not session.agent_id:
            _status("Starting agent…")
            with Agent.create(opts, client=sdk_client) as agent:
                session.agent_id = agent.agent_id
                preamble = _system_preamble(
                    session.deploy_log, session.repo, cloud=(mode == "cloud")
                )
                first = f"{preamble}\n\nUser:\n{user_message}"
                run = agent.send(first)
                _status("Working…")
                result = consume_run(run, emit)
        else:
            _status("Resuming session…")
            # Local agents require model (+ local/cloud) on resume/send, not only api_key.
            with Agent.resume(session.agent_id, opts, client=sdk_client) as agent:
                run = agent.send(user_message)
                _status("Working…")
                result = consume_run(run, emit)
    except CursorAgentError as e:
        session.agent_id = None
        return CursorFixResult(False, "startup_error", e.message, error=e.message)
    except OSError as e:
        session.agent_id = None
        win = getattr(e, "winerror", None)
        if win == 10038:
            msg = (
                "Cursor SDK bridge failed on this host (Windows socket error). "
                "Ensure Cursor CLI is installed and try restarting the deployer server."
            )
        else:
            msg = f"Cursor SDK OS error: {e}"
        return CursorFixResult(False, "startup_error", msg, error=msg)
    except Exception as e:
        session.agent_id = None
        msg = str(e) or type(e).__name__
        return CursorFixResult(False, "startup_error", msg, error=msg)

    session.cwd = workspace_label(mode, session.repo, local_cwd)
    return fix_result_from_run(result)


def get_or_create_session(session_id: str | None) -> CursorSession:
    with _lock:
        if session_id and session_id in _sessions:
            return _sessions[session_id]
        sid = session_id or str(uuid.uuid4())
        s = CursorSession(session_id=sid)
        _sessions[sid] = s
        return s


def clear_session(session_id: str) -> None:
    with _lock:
        _sessions.pop(session_id, None)


async def chat_turn(
    message: str,
    *,
    session_id: str | None = None,
    run_id: str | None = None,
    repo: str | None = None,
    deploy_log: str | None = None,
    include_terminal_log: bool = False,
    db=None,
) -> dict:
    key_issue = cursor_api_key_issue()
    if key_issue:
        return {
            "ok": False,
            "error": key_issue,
            "session_id": session_id or "",
            "reply": "",
        }
    session = get_or_create_session(session_id)
    mode = cursor_runtime_mode()
    if mode == "local":
        try:
            await ensure_workspace(session, run_id=run_id, repo=repo)
        except Exception as e:
            return {
                "ok": False,
                "session_id": session.session_id,
                "error": str(e),
                "reply": "",
            }
    else:
        if repo:
            session.repo = repo
        if not session.repo:
            return {
                "ok": False,
                "session_id": session.session_id,
                "error": "Select a GitHub repository for Cursor Cloud.",
                "reply": "",
            }

    from app.cursor_agent.bridge_client import run_on_sdk_thread
    from app.terminal_log import resolve_deploy_log_for_cursor

    merged_log = resolve_deploy_log_for_cursor(
        deploy_log=deploy_log,
        run_id=run_id,
        include_terminal_log=include_terminal_log,
        db=db,
    )
    if merged_log:
        session.deploy_log = merged_log

    out = await run_on_sdk_thread(
        _chat_turn_sync,
        session,
        message.strip(),
        deploy_log=merged_log or None,
        run_id=run_id,
        repo=repo,
    )
    return {
        "ok": out.ok,
        "session_id": session.session_id,
        "status": out.status,
        "reply": out.summary,
        "workspace": session.cwd,
        "runtime": mode,
        "error": out.error,
    }
