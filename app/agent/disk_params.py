"""Disk-specific param extraction (uses shared conversation helpers)."""

import re
from typing import Any

from app.agent.conversation_params import (
    compose_user_goal,
    conversation_text,
    infer_app_name,
    persian_digits_to_ascii,
    user_approved_write,
)

__all__ = ["compose_user_goal", "enrich_disk_params"]


def infer_disk_size_gb(text: str) -> int | None:
    t = persian_digits_to_ascii(text.lower())
    for pat in (
        r"(\d+)\s*(?:gb|g|گیگ|گیگابایت)",
        r"(?:ظرفیت|حجم|size)\s*[:=]?\s*(\d+)",
        r"^(\d+)\s*گیگ",
        r"^(\d+)$",
    ):
        m = re.search(pat, t, re.I)
        if m:
            try:
                n = int(m.group(1))
                if 1 <= n <= 1024:
                    return n
            except ValueError:
                continue
    return None


def infer_disk_name(text: str) -> str | None:
    for m in re.finditer(r"`([a-z0-9][a-z0-9-]{1,30})`", text, re.I):
        return m.group(1).lower()
    m = re.search(
        r"(?:disk|دیسک)\s+[`'\"]?([a-z0-9][a-z0-9-]{1,30})[`'\"]?",
        text,
        re.I,
    )
    if m:
        return m.group(1).lower()
    m = re.search(r"\b([a-z][a-z0-9]*(?:-[a-z0-9]+)+)\b", text, re.I)
    if m:
        return m.group(1).lower()
    if "terminal" in text.lower() and "log" in text.lower():
        return "terminal-logs"
    if "لاگ" in text and "ترمینال" in text:
        return "terminal-logs"
    return None


def enrich_disk_params(
    user_message: str,
    params: dict[str, Any],
    *,
    conversation_history: list[dict[str, str]] | None = None,
    liara_projects: list[str] | None = None,
) -> None:
    combined = conversation_text(user_message, conversation_history)
    if not params.get("disk_name"):
        name = infer_disk_name(combined)
        if name:
            params["disk_name"] = name
    if params.get("size_gb") is None and params.get("size") is None:
        size = infer_disk_size_gb(combined) or infer_disk_size_gb(user_message)
        if size is not None:
            params["size_gb"] = size
    if not params.get("app_name"):
        app = infer_app_name(combined, liara_projects) or params.get("liara_app_name")
        if app:
            params["app_name"] = app
    if user_approved_write(user_message, conversation_history):
        params["confirm_write"] = True
