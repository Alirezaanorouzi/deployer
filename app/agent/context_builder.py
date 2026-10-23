"""Structured LLM context for intent planner, ReAct replanner, and final reply.

Each turn exposes:
- user_goal: the current user message only (never a concatenation of prior turns)
- conversation_history: trimmed prior user/assistant messages for disambiguation
- observations: tool results from the active ReAct loop
- planner_params: resolved slots (app_name, confirm_write, …) from harness code
"""

from __future__ import annotations

import json
from typing import Any

# Limits (single place to tune)
HISTORY_TURNS_INTENT = 12
HISTORY_TURNS_REACT = 8
HISTORY_TURNS_REPLY = 6
MAX_MESSAGE_CHARS = 2000
MAX_OBSERVATIONS_REACT = 10
MAX_OBSERVATIONS_REPLY = 8
MAX_OBSERVATION_OUTPUT_CHARS = 4000

_ENV_GOAL_MARKERS = (
    "token",
    "توکن",
    "github",
    "گیت",
    "git ",
    "env",
    "متغیر",
    "environment",
    "secret",
)
_DISK_GOAL_MARKERS = ("disk", "دیسک", "volume", "حجم", "mount")


def _goal_mentions(goal: str, markers: tuple[str, ...]) -> bool:
    g = (goal or "").lower()
    raw = goal or ""
    return any(m in g or m in raw for m in markers)


def slim_observation_for_llm(obs: dict[str, Any], user_goal: str) -> dict[str, Any]:
    """Strip bulky tool data; keep only fields relevant to user_goal."""
    row: dict[str, Any] = {
        "tool": obs.get("tool"),
        "ok": obs.get("ok"),
        "code": obs.get("code"),
        "output": obs.get("output"),
    }
    data = obs.get("data")
    if not isinstance(data, dict):
        return row
    tool = obs.get("tool") or ""
    goal = current_user_goal(user_goal)
    if tool == "liara.get_app_settings":
        settings = data.get("settings") if isinstance(data.get("settings"), dict) else data
        if isinstance(settings, dict):
            if _goal_mentions(goal, _ENV_GOAL_MARKERS) and not _goal_mentions(goal, _DISK_GOAL_MARKERS):
                row["data"] = {
                    "env_var_count": settings.get("env_var_count"),
                    "env_var_keys": settings.get("env_var_keys"),
                }
            else:
                row["data"] = {
                    k: settings.get(k)
                    for k in (
                        "project_id",
                        "env_var_count",
                        "env_var_keys",
                        "scale",
                        "zero_downtime",
                        "reserved_disk_space",
                    )
                    if settings.get(k) is not None
                }
        return row
    if tool == "liara.list_app_disks":
        if _goal_mentions(goal, _DISK_GOAL_MARKERS):
            row["data"] = {"count": data.get("count"), "disks": data.get("disks")}
        return row
    if tool == "liara.update_env_variables":
        row["data"] = {k: data.get(k) for k in ("updated_keys", "app_name", "project_id") if data.get(k) is not None}
        return row
    return row


def current_user_goal(user_message: str) -> str:
    return (user_message or "").strip()


def trim_history(
    history: list[dict[str, str]] | None,
    *,
    max_turns: int,
    max_chars: int = MAX_MESSAGE_CHARS,
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in (history or [])[-max_turns:]:
        role = (item.get("role") or "").strip()
        content = (item.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        if len(content) > max_chars:
            content = content[: max_chars - 3] + "..."
        out.append({"role": role, "content": content})
    return out


def trim_observations(
    observations: list[dict[str, Any]] | None,
    *,
    limit: int,
    max_output_chars: int = MAX_OBSERVATION_OUTPUT_CHARS,
    user_goal: str = "",
) -> list[dict[str, Any]]:
    trimmed: list[dict[str, Any]] = []
    for obs in (observations or [])[-limit:]:
        row = slim_observation_for_llm(dict(obs), user_goal)
        out = row.get("output")
        if isinstance(out, str) and len(out) > max_output_chars:
            row["output"] = out[: max_output_chars - 3] + "..."
        trimmed.append(row)
    return trimmed


def build_intent_user_block(
    user_message: str,
    context: dict[str, Any],
) -> str:
    return f"User message:\n{current_user_goal(user_message)}\n\nContext:\n{json.dumps(context, ensure_ascii=False)}"


def build_react_planner_payload(
    *,
    skill_id: str | None,
    user_goal: str,
    conversation_history: list[dict[str, str]] | None,
    observations: list[dict[str, Any]],
    planner_params: dict[str, Any] | None,
) -> str:
    params = planner_params or {}
    body = {
        "skill_id": skill_id,
        "user_goal": current_user_goal(user_goal),
        "conversation_history": trim_history(conversation_history, max_turns=HISTORY_TURNS_REACT),
        "observations": trim_observations(
            observations, limit=MAX_OBSERVATIONS_REACT, user_goal=user_goal
        ),
        "planner_params": params,
        "failure_recovery_hints": params.get("failure_recovery_hints") or [],
    }
    return json.dumps(body, ensure_ascii=False)


def build_reply_payload(
    *,
    user_goal: str,
    latest_user_message: str | None,
    conversation_history: list[dict[str, str]] | None,
    observations: list[dict[str, Any]],
) -> str:
    goal = current_user_goal(latest_user_message or user_goal)
    body = {
        "user_goal": goal,
        "conversation_history": trim_history(conversation_history, max_turns=HISTORY_TURNS_REPLY),
        "observations": trim_observations(
            observations, limit=MAX_OBSERVATIONS_REPLY, user_goal=goal
        ),
    }
    return json.dumps(body, ensure_ascii=False)


def build_judge_payload(user_goal: str, observations: list[dict[str, Any]]) -> str:
    goal = current_user_goal(user_goal)
    return json.dumps(
        {
            "user_goal": goal,
            "observations": trim_observations(
                observations, limit=MAX_OBSERVATIONS_REPLY, user_goal=goal
            ),
        },
        ensure_ascii=False,
    )


def build_chat_payload(
    latest_user_message: str,
    conversation_history: list[dict[str, str]] | None,
) -> str:
    return json.dumps(
        {
            "user_goal": current_user_goal(latest_user_message),
            "conversation_history": trim_history(conversation_history, max_turns=HISTORY_TURNS_INTENT),
        },
        ensure_ascii=False,
    )


REACT_PLANNER_SYSTEM = """You are the step planner for a Liara DevOps agent (tools-in-a-loop).

You receive JSON with:
- user_goal: what the user asked in THIS turn only — satisfy this, not older questions from history.
- conversation_history: prior turns for disambiguation (app name, failed deploy, approvals) — do not re-answer old topics.
- observations: tool results already collected this run — treat as ground truth.
- planner_params: harness-filled slots (app_name, repo, confirm_write, since_hours, …).
- skill_id: active skill recipe.

Return ONLY JSON:
{
  "action": "call_tool" | "finish" | "clarify",
  "tool_id": "<id from allowed_tools or null>",
  "tool_params": {},
  "message": "user-facing text when action is finish or clarify"
}

Replanning rules:
- Read observations before deciding. Never invent API data missing from observations.
- finish when user_goal is fully answered by observations, or user needs advice derivable from observations/history.
- clarify when a required param is missing and no tool can proceed.
- call_tool: exactly one tool that advances user_goal.
- Merge app_name, repo, env_key, confirm_write, since_hours from planner_params into tool_params when relevant.
- Do not repeat the same successful tool with identical params.

Writes (restart, env, disk, domain, deploy_service, …): confirm_write must be true after explicit user approval in chat.

Skill recipes:
- liara.deploy: clone_github → prepare_deploy_workspace → create_app → deploy_service → get_app_url → verify_app_health; on failure → diagnose_deploy_health; never finish after create_app alone.
- liara.settings: get_app_settings first; update_env_variables only with confirm_write.
- liara.history: list_deploy_releases.
- liara.health: get_app_url (if needed) → verify_app_health; on UNHEALTHY/502/503 → diagnose_deploy_health → finish with clear status for the user.
- liara.logs / incident.diagnose: analyze_service_logs with app_name and time window; combine with verify/diagnose when user reports outage.
- After errors, use failure_recovery_hints and observations before finish.

For advisory follow-ups in history ("what now?", "چیکار کنم"), finish with concrete steps from the latest assistant/error text — do not restart a full deploy unless user_goal asks again."""


REPLY_SYSTEM = """You are the Liara DevOps agent on Liara cloud.

You receive JSON with user_goal (this turn), optional conversation_history, and observations from tools run this turn.

Answer ONLY user_goal using observations — never invent balances, counts, or API results.
Do not repeat facts from conversation_history unless user_goal asks for a recap or comparison.
Do not mention disks, reserved disk space, or unrelated settings when user_goal is only about env vars / GitHub token / secrets.
Do not include planning phrases ("I will check…", "now I am reviewing…") — only the outcome the user asked for.
Write in the user's language (Persian or English). Persian answers must be fully Persian — no English appendix.
Use clear prose; **bold** for key values is OK.
If the goal was diagnosis (slow site, errors), end with Persian sections when user is Persian: علت‌های محتمل، اقدام پیشنهادی، نیاز به تأیید.
If observations show errors, explain what failed and what to try next.
For health/status questions: state HTTP result, whether the app is up, and 1–3 concrete next steps (check Liara logs, fix crash/import, PORT/0.0.0.0). Do not paste raw tool codes like VERIFY_FAILED or English boilerplate.
Liara env vars use liara.get_app_settings and liara.update_env_variables — never claim those tools are missing."""


CHAT_ONLY_SYSTEM = """You are a Liara DevOps assistant. Reply in the user's language (Persian or English).

You receive user_goal (this message) and conversation_history for context.
Use history when the user refers to a prior deploy, error, or advice request — do not invent API results or claim you ran tools now.
If they ask what to do after a failed deploy, give concrete steps (upgrade @liara/cli, retry deploy, check logs, fix PORT/0.0.0.0).
If they only greet, reply briefly and offer help. Keep answers clear and actionable (short bullets OK)."""
