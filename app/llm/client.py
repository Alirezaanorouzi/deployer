from functools import lru_cache

import httpx
from openai import AsyncOpenAI

from app.config import get_settings


class LlmNotConfiguredError(RuntimeError):
    pass


@lru_cache
def _openai_client_cached(api_key: str, base_url: str) -> AsyncOpenAI:
    timeout = httpx.Timeout(connect=15.0, read=90.0, write=30.0, pool=15.0)
    return AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=1)


def get_llm_client() -> AsyncOpenAI:
    settings = get_settings()
    if not settings.llm_api_key:
        raise LlmNotConfiguredError(
            "LLM_API_KEY is not set. This service runs as an agentic AI — configure LLM_API_KEY and LLM_BASE_URL in .env."
        )
    return _openai_client_cached(settings.llm_api_key, settings.llm_base_url)


def clear_llm_client_cache() -> None:
    """Call after changing LLM_API_KEY / LLM_BASE_URL without full process restart."""
    _openai_client_cached.cache_clear()
    get_settings.cache_clear()


def llm_configured() -> bool:
    return bool(get_settings().llm_api_key)
