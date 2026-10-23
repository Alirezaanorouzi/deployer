"""Agent loop: after each tool/step, observe results and narrate to the user (Cursor-style)."""

import json
from dataclasses import dataclass
from typing import Any

from app.config import get_settings
from app.llm.client import get_llm_client

STEP_LABELS: dict[str, str] = {
    "github_clone": "Cloning your GitHub repository",
    "repo_analyze": "Analyzing the project stack and deploy needs",
    "write_liara_config": "Writing Liara configuration",
    "liara_auth_check": "Verifying Liara API access",
    "liara_create_app": "Creating the Liara app",
    "liara_deploy": "Deploying to Liara",
    "resolve_site_url": "Resolving your live URL",
    "verify_health": "Checking that the site is healthy",
    "health_check": "Running a health check",
    "auth_check": "Checking Liara authentication",
    "deploy": "Deploying",
    "bootstrap": "Bootstrapping the app",
    "fetch_logs": "Fetching logs",
}


def step_label(name: str) -> str:
    return STEP_LABELS.get(name, f"Running {name.replace('_', ' ')}")


@dataclass
class StepObservation:
    message: str
    continue_execution: bool = True


def _truncate(s: str | None, limit: int = 1800) -> str:
    if not s:
        return ""
    s = s.strip()
    if len(s) <= limit:
        return s
    return s[: limit - 3] + "..."


async def observe_after_step(
    *,
    user_goal: str,
    skill_id: str,
    step_name: str,
    step_output: str | None,
    transcript: list[dict[str, Any]],
    pending_step_names: list[str],
    run_state: str,
    error: str | None = None,
) -> StepObservation:
    """LLM reflects on the last tool result and tells the user what happened / what's next."""
    if error:
        return StepObservation(
            message=(
                f"Step **{step_label(step_name)}** failed.\n\n{error}\n\n"
                "You can fix the issue and send another message in chat to retry."
            ),
            continue_execution=False,
        )

    client = get_llm_client()
    settings = get_settings()

    compact_transcript = [
        {"step": t.get("step"), "result_preview": _truncate(str(t.get("output", "")), 400)}
        for t in transcript[-6:]
    ]

    persian = any("\u0600" <= c <= "\u06ff" for c in user_goal)
    lang = (
        "Write in Persian (فارسی) only."
        if persian
        else "Write in the same language as the user."
    )
    system = f"""You are the live Liara DevOps agent speaking in chat (like Cursor).
The user sees your messages while tools run in the background.
{lang}
After each tool step, write a SHORT update (2-4 sentences):
- What just completed and key facts (repo, stack, app name, URL) from the result
- What you will do next (name the next step in plain English)
Do NOT invent URLs or success if the output shows failure.
Do NOT include secrets or tokens.
Plain text only (no JSON). You may use **bold** for emphasis."""

    user = json.dumps(
        {
            "user_goal": user_goal,
            "skill_id": skill_id,
            "just_finished_step": step_name,
            "step_output_preview": _truncate(step_output),
            "prior_steps": compact_transcript,
            "pending_steps": pending_step_names[:8],
            "run_state": run_state,
        },
        ensure_ascii=False,
    )

    kwargs: dict = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if "boxapi" not in settings.llm_base_url.lower():
        kwargs["temperature"] = 0.3

    try:
        resp = await client.chat.completions.create(**kwargs)
        text = (resp.choices[0].message.content or "").strip()
        if not text:
            text = f"Finished {step_label(step_name)}. Continuing with the plan."
        return StepObservation(message=text, continue_execution=True)
    except Exception as e:
        fallback = f"Done: {step_label(step_name)}."
        if pending_step_names:
            fallback += f" Next: {step_label(pending_step_names[0])}."
        fallback += f" (Detail: {e})"
        return StepObservation(message=fallback, continue_execution=True)


async def observe_awaiting_approval(
    *,
    user_goal: str,
    repo: str | None,
    app_name: str | None,
) -> str:
    client = get_llm_client()
    settings = get_settings()
    system = """You are the Liara DevOps agent. Phase 1 (clone, analyze, config) is done.
Tell the user in 2-3 friendly sentences that you need their approval before creating the Liara app and deploying.
Mention repo and app name if provided. Plain text."""
    user = json.dumps(
        {"user_goal": user_goal, "repo": repo, "liara_app": app_name},
        ensure_ascii=False,
    )
    kwargs: dict = {
        "model": settings.llm_model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    if "boxapi" not in settings.llm_base_url.lower():
        kwargs["temperature"] = 0.3
    try:
        resp = await client.chat.completions.create(**kwargs)
        return (resp.choices[0].message.content or "").strip() or (
            f"Ready to create Liara app «{app_name}» for {repo}. "
            "Please click **Approve & continue** on the left."
        )
    except Exception:
        return (
            f"Repository is prepared for **{repo}**. "
            f"Approve on the left to create app **{app_name}** and deploy."
        )
