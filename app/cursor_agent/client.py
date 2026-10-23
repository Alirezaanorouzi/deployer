"""Cursor SDK — coding agent on the deploy workspace after build/deploy failures."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings

_BUILD_PATTERNS = (
    re.compile(r"cannot find module", re.I),
    re.compile(r"module not found", re.I),
    re.compile(r"npm run build", re.I),
    re.compile(r"failed to compile", re.I),
    re.compile(r"typescript error", re.I),
    re.compile(r"error TS\d+", re.I),
    re.compile(r"build failed", re.I),
    re.compile(r"exit code [1-9]", re.I),
)


def cursor_sdk_configured() -> bool:
    return cursor_api_key_issue() is None


def cursor_api_key_issue() -> str | None:
    """Return a user-facing config error, or None if the key looks usable."""
    key = (get_settings().cursor_api_key or "").strip()
    if not key:
        return "CURSOR_API_KEY not configured"
    return None


def looks_like_build_failure(log_text: str) -> bool:
    text = log_text or ""
    return any(p.search(text) for p in _BUILD_PATTERNS)


@dataclass
class CursorFixResult:
    ok: bool
    status: str
    summary: str
    run_id: str | None = None
    error: str | None = None


def _build_fix_prompt(error_log: str, repo: str | None) -> str:
    repo_line = f"Repository: {repo}\n" if repo else ""
    return f"""You are fixing a deployment build failure in this project directory (cwd).

{repo_line}
Deployment / build log:
---
{error_log[:12000]}
---

Tasks:
1. Find the root cause (imports, path aliases, missing files, tsconfig, package.json, etc.).
2. Apply minimal code/config fixes in this repo.
3. Run the project build command (e.g. npm run build, pnpm build, python -m build) and confirm it succeeds.
4. Summarize: what was wrong, what you changed, build result, and whether git diff shows only safe changes.

Do not deploy to any cloud platform; only fix and verify build locally in cwd."""


def run_deploy_code_fix(
    deploy_path: str | Path,
    error_log: str,
    *,
    repo: str | None = None,
) -> CursorFixResult:
    """
    Run a one-shot Cursor local agent on the cloned deploy tree.
    Blocking — call via asyncio.to_thread from async code.
    """
    settings = get_settings()
    api_key = (settings.cursor_api_key or "").strip()
    if not api_key:
        return CursorFixResult(
            ok=False,
            status="skipped",
            summary="CURSOR_API_KEY is not set in .env",
            error="CONFIG",
        )

    root = Path(deploy_path).resolve()
    if not root.is_dir():
        return CursorFixResult(
            ok=False,
            status="skipped",
            summary=f"Deploy path does not exist: {root}",
            error="NO_WORKSPACE",
        )

    try:
        from cursor_sdk import Agent, AgentOptions, CursorAgentError, LocalAgentOptions
    except ImportError as e:
        return CursorFixResult(
            ok=False,
            status="error",
            summary="cursor-sdk is not installed (pip install cursor-sdk)",
            error=str(e),
        )

    prompt = _build_fix_prompt(error_log, repo)
    from app.cursor_agent.bridge_client import client_for_cwd
    from app.cursor_agent.model_selection import cursor_model_selection
    from app.cursor_agent.run_result import fix_result_from_run

    model = cursor_model_selection(settings.cursor_sdk_model or "composer-2.5")

    cwd = str(root)

    try:
        sdk_client = client_for_cwd(cwd)
        result = Agent.prompt(
            prompt,
            AgentOptions(
                api_key=api_key,
                model=model,
                local=LocalAgentOptions(cwd=cwd),
            ),
            client=sdk_client,
        )
    except CursorAgentError as e:
        return CursorFixResult(
            ok=False,
            status="startup_error",
            summary=f"Cursor agent did not start: {e.message}",
            error=e.message,
        )
    except OSError as e:
        msg = str(e)
        if getattr(e, "winerror", None) == 10038:
            msg = "Cursor SDK bridge failed (Windows socket error)."
        return CursorFixResult(False, "startup_error", summary=msg, error=msg)
    except Exception as e:
        return CursorFixResult(
            ok=False,
            status="startup_error",
            summary=str(e) or type(e).__name__,
            error=str(e),
        )

    return fix_result_from_run(result)
