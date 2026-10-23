"""Analyze Liara service logs with LLM (secrets already redacted)."""

from __future__ import annotations

import json
import re
from typing import Any

from app.agent.language import prefers_persian
from app.config import get_settings
from app.diagnosis import _heuristic_diagnosis
from app.llm.client import LlmNotConfiguredError, get_llm_client, llm_configured


def _tail_lines(text: str, max_lines: int) -> str:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    return "\n".join(lines[-max_lines:])


def _quick_stats(logs: str) -> dict[str, Any]:
    lower = logs.lower()
    error_hits = len(re.findall(r"\b(error|exception|fatal|failed|panic)\b", lower))
    http_5xx = len(re.findall(r"\b5\d{2}\b", logs))
    http_4xx = len(re.findall(r"\b4\d{2}\b", logs))
    return {
        "line_count": len(logs.splitlines()),
        "error_keyword_hits": error_hits,
        "http_5xx_mentions": http_5xx,
        "http_4xx_mentions": http_4xx,
    }


async def analyze_service_logs(
    *,
    project_id: str,
    logs: str,
    user_focus: str | None = None,
    user_goal: str | None = None,
) -> str:
    stats = _quick_stats(logs)
    excerpt = logs[-14000:] if len(logs) > 14000 else logs

    if not llm_configured():
        return _heuristic_logs_analysis(project_id, excerpt, stats, user_focus)

    settings = get_settings()
    client = get_llm_client()
    lang_note = (
        "Respond entirely in Persian (فارسی). Headings: **خلاصه**، **مشکلات**، **اقدام‌های پیشنهادی**."
        if prefers_persian(user_goal or user_focus or "")
        else (
            "Respond in clear English. Structure: **Summary**, **Issues**, **Recommended next steps**."
        )
    )
    system = f"""You analyze Liara PaaS application logs for a DevOps agent.
Logs are already redacted (no secrets). {lang_note}
Keep under 12 lines. Do not repeat the full log dump."""

    user = json.dumps(
        {
            "project_id": project_id,
            "user_focus": user_focus or "general health and errors",
            "stats": stats,
            "log_excerpt": excerpt,
        },
        ensure_ascii=False,
    )

    try:
        kwargs: dict = {
            "model": settings.llm_model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if "boxapi" not in settings.llm_base_url.lower():
            kwargs["temperature"] = 0.2
        resp = await client.chat.completions.create(**kwargs)
        text = (resp.choices[0].message.content or "").strip()
        return text or _heuristic_logs_analysis(project_id, excerpt, stats, user_focus)
    except LlmNotConfiguredError:
        return _heuristic_logs_analysis(project_id, excerpt, stats, user_focus)
    except Exception:
        return _heuristic_logs_analysis(project_id, excerpt, stats, user_focus)


def _heuristic_logs_analysis(
    project_id: str,
    excerpt: str,
    stats: dict[str, Any],
    user_focus: str | None,
) -> str:
    evidence = {"logs": excerpt, "health": {}, "metrics": {"project_id": project_id}}
    base = _heuristic_diagnosis(evidence)
    lines = [
        f"**{project_id}** log scan ({stats.get('line_count', 0)} lines, source: heuristic)",
        f"Error-like keywords: **{stats.get('error_keyword_hits', 0)}** · 5xx mentions: **{stats.get('http_5xx_mentions', 0)}**",
    ]
    if user_focus:
        lines.append(f"Focus: {user_focus}")
    lines.append(base)
    return "\n".join(lines)
