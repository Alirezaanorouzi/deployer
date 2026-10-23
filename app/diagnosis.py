import json
from typing import Any

from app.agent.language import prefers_persian
from app.config import get_settings
from app.llm.client import LlmNotConfiguredError, get_llm_client, llm_configured


def _diagnosis_system(user_goal: str) -> str:
    if prefers_persian(user_goal):
        return (
            "You are a Liara DevOps agent. Given JSON evidence (health, metrics, logs), "
            "write the full answer in Persian (فارسی) only — no English section at the end.\n"
            "Structure:\n"
            "1) **علت‌های محتمل** — مرتب‌شده از محتمل‌ترین\n"
            "2) **اقدام پیشنهادی بعدی** — مشخص (redeploy، scale، env، بررسی دیتابیس و …)\n"
            "3) **نیاز به تأیید کاربر** — بله/خیر و چرا\n"
            "فقط از شواهد JSON استفاده کن؛ حداکثر ۱۵ خط."
        )
    return (
        "You are a Liara DevOps agent. Given JSON evidence (health, metrics, logs), respond in English with: "
        "1) ranked likely causes 2) recommended next action (redeploy, scale, fix env) "
        "3) whether user confirmation is required. Use only the evidence provided."
    )


async def diagnose_with_llm(evidence: dict[str, Any]) -> str:
    if not llm_configured():
        return _heuristic_diagnosis(evidence)

    user_goal = str(evidence.get("user_goal") or "")
    settings = get_settings()
    client = get_llm_client()
    system = _diagnosis_system(user_goal)
    user = "Evidence:\n" + json.dumps(evidence, ensure_ascii=False)[:12000]
    try:
        kwargs: dict = {
            "model": settings.llm_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if "boxapi" not in settings.llm_base_url.lower():
            kwargs["temperature"] = 0
        resp = await client.chat.completions.create(**kwargs)
        return (resp.choices[0].message.content or "").strip()
    except LlmNotConfiguredError:
        return _heuristic_diagnosis(evidence)
    except Exception:
        return _heuristic_diagnosis(evidence)


def _heuristic_diagnosis(evidence: dict[str, Any]) -> str:
    user_goal = str(evidence.get("user_goal") or "")
    fa = prefers_persian(user_goal)
    health = evidence.get("health") or {}
    code = health.get("status_code")
    metrics = evidence.get("metrics") or {}
    logs = (evidence.get("logs") or "")[:2000].lower()

    if fa:
        lines = ["**تحلیل اولیه** (بدون LLM):"]
        if code and code >= 500:
            lines.append("1) احتمال خطای اپلیکیشن (HTTP 5xx) — لاگ را برای stack trace ببینید.")
        elif code and code >= 400:
            lines.append("1) احتمال مشکل مسیر یا تنظیمات (HTTP 4xx).")
        if "build_timeout" in logs or "timeout" in logs:
            lines.append("2) احتمال BUILD_TIMEOUT — پلن یا زمان build را بررسی کنید.")
        if "memory" in logs or "oom" in logs:
            lines.append("3) احتمال کمبود RAM — ارتقای پلن (نیاز به تأیید).")
        if metrics.get("limitation"):
            lines.append("متریک API محدود بود؛ فقط health و لاگ استفاده شد.")
        if len(lines) == 1:
            lines.append("شواهد کم است — health/لاگ را دوباره بگیرید یا deploy قبلی را rollback کنید.")
        lines.append("**پیشنهاد:** پس از تأیید شما — restart، redeploy، یا scale.")
        return "\n".join(lines)

    lines = ["Initial analysis (LLM unavailable):"]
    if code and code >= 500:
        lines.append("1) Likely application error (HTTP 5xx) — check logs for stack trace.")
    elif code and code >= 400:
        lines.append("1) Likely routing/config issue (HTTP 4xx).")
    if "build_timeout" in logs or "timeout" in logs:
        lines.append("2) Possible BUILD_TIMEOUT — reduce SSG pages or increase plan timeout.")
    if "memory" in logs or "oom" in logs:
        lines.append("3) Possible RAM limit — plan upgrade (requires approval).")
    if metrics.get("limitation"):
        lines.append("Metrics API limited; used logs and health only.")
    if len(lines) == 1:
        lines.append("Limited evidence — recheck logs/health or rollback last deploy.")
    lines.append("Suggested: after your approval — restart, redeploy, or scale plan.")
    return "\n".join(lines)
