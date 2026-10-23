"""Stream LLM chat completions to run SSE (agent chat tokens)."""

from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.llm.client import get_llm_client
from app.streaming import stream_bus


async def stream_chat_to_run(
    run_id: str,
    messages: list[dict[str, str]],
    *,
    temperature: float | None = None,
    new_block: bool = False,
    replace: bool = False,
) -> str:
    """Stream assistant tokens; publish agent_message SSE events with stream=true."""
    client = get_llm_client()
    settings = get_settings()
    kwargs: dict[str, Any] = {
        "model": settings.llm_model,
        "messages": messages,
        "stream": True,
    }
    if temperature is not None and "boxapi" not in settings.llm_base_url.lower():
        kwargs["temperature"] = temperature

    stream = await client.chat.completions.create(**kwargs)
    parts: list[str] = []
    first = True
    async for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta.content or ""
        if not delta:
            continue
        parts.append(delta)
        await stream_bus.publish(
            run_id,
            "agent_message",
            {
                "text": delta,
                "append": True,
                "stream": True,
                "stream_start": first,
                "new_block": bool(new_block and first),
                "replace": bool(replace and first),
            },
        )
        first = False
    return "".join(parts)
