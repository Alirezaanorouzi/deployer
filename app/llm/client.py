from functools import lru_cache

from openai import AsyncOpenAI

from app.config import get_settings


class LlmNotConfiguredError(RuntimeError):
    pass


@lru_cache
def get_llm_client() -> AsyncOpenAI:
    settings = get_settings()
    if not settings.llm_api_key:
        raise LlmNotConfiguredError(
            "LLM_API_KEY is not set. This service runs as an agentic AI — configure LLM_API_KEY and LLM_BASE_URL in .env."
        )
    return AsyncOpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url)


def llm_configured() -> bool:
    return bool(get_settings().llm_api_key)
