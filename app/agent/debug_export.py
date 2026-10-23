"""Structured agent-run export for Cursor debugging (redacted)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.liara.redact import redact
from app.models import Run

_SCHEMA = "deployer.agent_debug.v1"
_MAX_STEP_OUTPUT = 8000
_MAX_OBS_OUTPUT = 6000


def _redact_value(v: Any) -> Any:
    if isinstance(v, str):
        return redact(v)
    if isinstance(v, dict):
        return {k: _redact_value(val) for k, val in v.items()}
    if isinstance(v, list):
        return [_redact_value(x) for x in v[:50]]
    return v


def _redact_params(params: dict[str, Any] | None) -> dict[str, Any]:
    if not params:
        return {}
    out = dict(params)
    for key in ("env_value", "github_token", "token", "api_token"):
        if key in out and out[key]:
            out[key] = "***"
    return _redact_value(out)


def _trim_observation(obs: dict[str, Any]) -> dict[str, Any]:
    row = {
        "tool": obs.get("tool"),
        "ok": obs.get("ok"),
        "code": obs.get("code"),
    }
    out = obs.get("output")
    if isinstance(out, str):
        out = redact(out)
        if len(out) > _MAX_OBS_OUTPUT:
            out = out[: _MAX_OBS_OUTPUT - 3] + "..."
        row["output"] = out
    data = obs.get("data")
    if isinstance(data, dict):
        row["data"] = _redact_value(data)
    return row


def build_agent_debug_export(run: Run) -> dict[str, Any]:
    ctx: dict[str, Any] = {}
    if run.context_json:
        try:
            ctx = json.loads(run.context_json)
        except json.JSONDecodeError:
            ctx = {}

    steps_out: list[dict[str, Any]] = []
    for step in sorted(run.steps or [], key=lambda s: s.seq):
        out = (step.output or "") if step.output else ""
        out = redact(out)
        if len(out) > _MAX_STEP_OUTPUT:
            out = out[: _MAX_STEP_OUTPUT - 3] + "..."
        steps_out.append(
            {
                "seq": step.seq,
                "name": step.name,
                "status": step.status,
                "output_preview": out or None,
            }
        )

    history = ctx.get("conversation_history") or []
    if isinstance(history, list):
        history = history[-12:]

    return {
        "schema": _SCHEMA,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "run": {
            "id": run.id,
            "state": str(run.state),
            "skill_id": run.skill_id,
            "user_message": run.user_message,
            "result_summary": redact(run.result_summary or "") or None,
            "error_code": run.error_code,
            "error_message": redact(run.error_message or "") or None,
            "site_url": run.site_url,
            "repo": ctx.get("repo"),
            "liara_app_name": ctx.get("liara_app_name") or ctx.get("active_app"),
        },
        "react": {
            "user_goal": ctx.get("user_goal"),
            "planner_params": _redact_params(ctx.get("planner_params")),
            "conversation_history": _redact_value(history) if history else [],
            "observations": [_trim_observation(o) for o in (ctx.get("observations") or []) if isinstance(o, dict)],
            "agent_transcript": _redact_value(ctx.get("agent_transcript") or []),
            "failure_hints": ctx.get("failure_hints"),
        },
        "plan": json.loads(run.plan_json) if run.plan_json else None,
        "steps": steps_out,
    }
