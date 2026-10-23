"""Chat completions that return a JSON object (planner / tool picker)."""

import json
import re
from typing import Any

from openai import AsyncOpenAI

from app.config import Settings


def parse_json_object(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```\s*$", "", text)
    if not text:
        return {}
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start : end + 1])
                return data if isinstance(data, dict) else {}
            except json.JSONDecodeError:
                pass
    raise json.JSONDecodeError("LLM did not return valid JSON", text, 0)


async def chat_json_object(
    client: AsyncOpenAI,
    settings: Settings,
    *,
    messages: list[dict[str, str]],
    temperature: float | None = 0.1,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "model": settings.llm_model,
        "messages": messages,
    }
    is_box = "boxapi" in (settings.llm_base_url or "").lower()
    is_liara_ai = "ai.liara.ir" in (settings.llm_base_url or "").lower()
    if not is_box and temperature is not None:
        base["temperature"] = temperature

    resp = None
    if not is_liara_ai:
        try:
            resp = await client.chat.completions.create(
                **base,
                response_format={"type": "json_object"},
            )
        except Exception:
            resp = None
    if resp is None:
        resp = await client.chat.completions.create(**base)

    raw = (resp.choices[0].message.content or "{}").strip()
    return parse_json_object(raw)
