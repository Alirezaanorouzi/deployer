import re
from typing import Any

_TOKEN_PATTERNS = [
    re.compile(r"(api[_-]?token|authorization)\s*[:=]\s*['\"]?\S+", re.I),
    re.compile(r"Bearer\s+\S+", re.I),
    re.compile(r"LIARA_API_TOKEN=\S+"),
]


def redact(text: str) -> str:
    out = text
    for pat in _TOKEN_PATTERNS:
        out = pat.sub(lambda m: m.group(0).split("=")[0] + "=***" if "=" in m.group(0) else "***", out)
    return out


def sanitize_liara_project(project: dict[str, Any]) -> dict[str, Any]:
    """Strip secret env values before UI/LLM."""
    out = dict(project)
    envs = out.get("envs")
    if isinstance(envs, list):
        out["envs"] = [
            {
                "key": e.get("key"),
                "encrypted": e.get("encrypted"),
                "value": "***" if e.get("value") else None,
            }
            for e in envs
            if isinstance(e, dict)
        ]
    return out
