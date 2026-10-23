"""Detect reply language from user chat."""

import re


def prefers_persian(text: str | None) -> bool:
    if not text:
        return False
    return bool(re.search(r"[\u0600-\u06ff]", text))
