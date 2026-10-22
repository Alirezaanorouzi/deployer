import json
from typing import Any

from app.config import get_settings
from app.llm.client import LlmNotConfiguredError, get_llm_client, llm_configured


async def diagnose_with_llm(evidence: dict[str, Any]) -> str:
    if not llm_configured():
        return _heuristic_diagnosis(evidence)

    settings = get_settings()
    client = get_llm_client()
    prompt = (
        "You are a Liara DevOps agent. Given JSON evidence (health, metrics, logs), "
        "respond in English (short Persian summary optional) with: "
        "1) ranked likely causes 2) recommended next action (which skill: redeploy, scale, fix env) "
        "3) whether user confirmation is required. Evidence:\n"
        + json.dumps(evidence, ensure_ascii=False)[:12000]
    )
    try:
        kwargs: dict = {
            "model": settings.llm_model,
            "messages": [{"role": "user", "content": prompt}],
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
    health = evidence.get("health") or {}
    code = health.get("status_code")
    metrics = evidence.get("metrics") or {}
    logs = (evidence.get("logs") or "")[:2000].lower()

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
