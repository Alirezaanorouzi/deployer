"""Write-action helpers and confirmation gate for Liara platform tools."""

from __future__ import annotations

from typing import Any


def user_confirmed_write(params: dict[str, Any]) -> bool:
    return params.get("confirm_write") is True


CONFIRMATION_MESSAGE = (
    "This action changes your Liara app. Ask the user to confirm explicitly, then call again with "
    "`confirm_write: true` in tool_params (or planner params) after they agree."
)


def parse_env_updates(params: dict[str, Any]) -> list[dict[str, str]] | None:
    """Accept `variables` list or single `env_key` + `env_value`."""
    raw = params.get("variables")
    if isinstance(raw, list) and raw:
        out: list[dict[str, str]] = []
        for item in raw:
            if isinstance(item, dict) and item.get("key"):
                out.append({"key": str(item["key"]), "value": str(item.get("value", ""))})
        return out or None
    key = params.get("env_key")
    if key:
        return [{"key": str(key), "value": str(params.get("env_value", ""))}]
    return None
