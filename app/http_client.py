"""Outbound HTTP — ignore system HTTP_PROXY (often breaks GitHub/Liara on Windows)."""

from __future__ import annotations

import httpx


def async_client(**kwargs: object) -> httpx.AsyncClient:
    kwargs.setdefault("trust_env", False)
    return httpx.AsyncClient(**kwargs)
