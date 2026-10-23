"""After a failed tool call: gather context, optional follow-up tools, or user-facing guide."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.agent.skill_pipelines import app_name_from, base_params

_LOG_TOOLS = frozenset(
    {
        "liara.analyze_service_logs",
        "liara.diagnose_deploy_health",
    }
)

_DEPLOY_FAIL_TOOLS = frozenset(
    {
        "repo.clone_github",
        "repo.prepare_deploy_workspace",
        "liara.create_app",
        "liara.deploy_service",
        "liara.verify_app_health",
    }
)

_SKIP_RECOVERY_CODES = frozenset(
    {
        "CONFIRMATION_REQUIRED",
    }
)


@dataclass
class FailureRecovery:
    """What the ReAct loop should do after a tool failure."""

    hint: str = ""
    thinking: str = ""
    terminal_line: str = ""
    follow_up: dict[str, Any] | None = None
    guide_message: str | None = None
    recovery_key: str | None = None


def _last_obs_for_tool(observations: list[dict[str, Any]], tool_id: str) -> dict[str, Any] | None:
    for obs in reversed(observations):
        if obs.get("tool") == tool_id:
            return obs
    return None


def _has_tool(observations: list[dict[str, Any]], tool_id: str) -> bool:
    return _last_obs_for_tool(observations, tool_id) is not None


def _output_snippet(output: str, limit: int = 400) -> str:
    text = (output or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def guide_for_failure(tool_id: str, code: str, output: str) -> str | None:
    """Deterministic user guide when auto-recovery cannot fix the issue."""
    out_l = (output or "").lower()
    code_u = (code or "").upper()

    if "not_enough_storage" in out_l or "storage" in out_l and "enough" in out_l:
        return (
            "**خطا:** فضای ذخیره‌سازی لیارا (دیسک/پلن) کافی نیست.\n\n"
            "**اقدام پیشنهادی:** در پنل لیارا پلن را ارتقا دهید یا دیسک/بکاپ اضافی را حذف کنید، "
            "سپس دوباره در چت بگویید «تأیید می‌کنم» تا عملیات را تکرار کنم."
        )
    if code_u in ("AUTH_MISSING", "UNAUTHORIZED") or "401" in out_l:
        return (
            "**خطا:** احراز هویت لیارا ناموفق بود.\n\n"
            "**اقدام:** `LIARA_API_TOKEN` را در `.env` سرور deployer بررسی کنید و uvicorn را restart کنید."
        )
    if code_u == "INVALID_INPUT":
        return (
            f"**خطا در ورودی** (`{tool_id}` / `{code}`):\n\n"
            f"{_output_snippet(output, 600)}\n\n"
            "پارامترها (نام اپ، نام دیسک، env و …) را اصلاح کنید و دوباره بفرستید."
        )
    if code_u in ("NETWORK_ERROR", "CLI_ERROR", "API_ERROR", "PARSE_ERROR"):
        return (
            f"**خطا در ارتباط با لیارا** (`{tool_id}` — `{code}`):\n\n"
            f"{_output_snippet(output, 600)}\n\n"
            "**اقدام:** اتصال شبکه، توکن API و وضعیت سرویس لیارا را بررسی کنید؛ "
            "می‌توانید همان درخواست را دوباره بفرستید."
        )
    if code_u == "GOAL_NOT_MET":
        return None
    return None


def build_max_iterations_guide(observations: list[dict[str, Any]]) -> str:
    for obs in reversed(observations):
        if not obs.get("ok"):
            tool = obs.get("tool") or "tool"
            code = obs.get("code") or "ERROR"
            out = obs.get("output") or ""
            guided = guide_for_failure(str(tool), str(code), str(out))
            if guided:
                return guided + "\n\n_(حداکثر تعداد گام‌های agent تمام شد؛ می‌توانید با تأیید یا پیام جدید ادامه دهید.)_"
            return (
                f"Agent به حداکثر گام‌ها رسید. آخرین خطا: **{tool}** (`{code}`).\n\n"
                f"{_output_snippet(str(out), 800)}"
            )
    return "Agent exceeded max tool iterations without completing the goal."


def plan_failure_recovery(
    *,
    skill_id: str,
    tool_id: str,
    code: str,
    output: str,
    observations: list[dict[str, Any]],
    planner_params: dict[str, Any],
    user_goal: str,
    recovery_tried: list[str],
) -> FailureRecovery:
    """Plan one recovery step after a failed tool (logs, diagnose, or guide)."""
    code_u = (code or "").upper()
    if code_u in _SKIP_RECOVERY_CODES:
        return FailureRecovery(
            hint="User must approve write (confirm_write) before this tool can run.",
            thinking="Waiting for user confirmation…",
        )

    app = app_name_from(planner_params)
    snippet = _output_snippet(output)

    # --- Deploy: pull diagnosis (logs + HTTP) once ---
    if skill_id == "liara.deploy" and tool_id in _DEPLOY_FAIL_TOOLS:
        key = "follow:liara.diagnose_deploy_health"
        if key not in recovery_tried and not _has_tool(observations, "liara.diagnose_deploy_health"):
            if app:
                params = base_params(planner_params)
                if planner_params.get("url"):
                    params["url"] = planner_params["url"]
                params["log_lines"] = 60
                return FailureRecovery(
                    hint="Deploy step failed; run diagnose_deploy_health for URL + logs.",
                    thinking="Deploy failed — fetching logs and health diagnosis…",
                    terminal_line=f"Recovery: diagnose deploy after {tool_id} ({code})",
                    recovery_key=key,
                    follow_up={
                        "action": "call_tool",
                        "tool_id": "liara.diagnose_deploy_health",
                        "tool_params": params,
                        "message": "",
                    },
                )
        guided = guide_for_failure(tool_id, code, output)
        if guided:
            return FailureRecovery(guide_message=guided, terminal_line=f"Guide: {code}")

    # --- After diagnosis or verify unhealthy: summarize for user ---
    if tool_id == "liara.diagnose_deploy_health" or (
        tool_id == "liara.verify_app_health" and code_u not in ("OK", "")
    ):
        guided = guide_for_failure(tool_id, code, output)
        body = guided or (
            f"**نتیجه بررسی** (`{tool_id}`):\n\n{snippet}\n\n"
            "لاگ‌ها و وضعیت HTTP در observation بالا هستند؛ اصلاح کد یا env و redeploy لازم است."
        )
        return FailureRecovery(guide_message=body, terminal_line="Recovery: diagnosis complete")

    # --- Generic: fetch service logs once when we have an app ---
    log_key = f"follow:liara.analyze_service_logs:{app or 'none'}"
    if (
        app
        and tool_id not in _LOG_TOOLS
        and log_key not in recovery_tried
        and not _has_tool(observations, "liara.analyze_service_logs")
        and code_u not in ("INVALID_INPUT", "AUTH_MISSING")
    ):
        params = base_params(planner_params)
        params["since_hours"] = planner_params.get("since_hours") or 2
        params["lines"] = planner_params.get("lines") or 80
        params["focus"] = planner_params.get("focus") or "errors and failures"
        params["user_goal"] = user_goal
        return FailureRecovery(
            hint=(
                f"Tool {tool_id} failed ({code}). Service logs may explain why; "
                "call analyze_service_logs then replan."
            ),
            thinking="Tool failed — pulling recent service logs…",
            terminal_line=f"Recovery: logs after {tool_id} ({code})",
            recovery_key=log_key,
            follow_up={
                "action": "call_tool",
                "tool_id": "liara.analyze_service_logs",
                "tool_params": params,
                "message": "",
            },
        )

    # --- Known quota / auth / input: guide only ---
    guided = guide_for_failure(tool_id, code, output)
    if guided:
        return FailureRecovery(
            guide_message=guided,
            terminal_line=f"Recovery: user guide for {code}",
            hint=snippet,
        )

    return FailureRecovery(
        hint=f"Last failure: {tool_id} ({code}). {snippet}",
        thinking="Analyzing error…",
        terminal_line=f"Tool failed: {tool_id} ({code})",
    )


def format_react_exception(exc: BaseException) -> str:
    msg = str(exc).strip() or type(exc).__name__
    if type(exc).__name__ == "NotImplementedError":
        return (
            "**خطا:** اجرای `liara deploy` در event loop فعلی پشتیبانی نمی‌شود (معمولاً Windows + uvicorn reload).\n\n"
            "**اقدام:** سرور را بدون `--reload` اجرا کنید، یا از PowerShell در مسیر پروژه همان دستور deploy را دستی بزنید؛ "
            "سپس در چت بگویید «تأیید می‌کنم» تا verify و URL را چک کنم."
        )
    low = msg.lower()
    if "timeout" in low or type(exc).__name__ in ("APITimeoutError", "ReadTimeout"):
        return (
            "**خطا:** درخواست به مدل LLM یا API لیارا timeout شد.\n\n"
            "**اقدام:** `LLM_BASE_URL` و شبکه را بررسی کنید و دوباره تلاش کنید."
        )
    if "json" in low or "JSONDecodeError" in type(exc).__name__:
        return (
            "**خطا:** پاسخ مدل JSON معتبر نبود.\n\n"
            "**اقدام:** مدل یا quota را بررسی کنید؛ درخواست را کوتاه‌تر یا دوباره ارسال کنید."
        )
    return f"**خطای داخلی agent:** {msg}\n\nاگر تکرار شد، لاگ سرور uvicorn را بررسی کنید."
