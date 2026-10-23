"""Map CURSOR_SDK_MODEL to SDK ModelSelection with plan-required params."""

from __future__ import annotations

from typing import Any

try:
    from cursor_sdk import ModelParameterValue, ModelSelection

    _CURSOR_SDK = True
except ImportError:  # Liara prod image omits cursor-sdk
    ModelParameterValue = Any  # type: ignore[misc, assignment]
    ModelSelection = Any  # type: ignore[misc, assignment]
    _CURSOR_SDK = False

# Plus / Start / X Subscription tiers require fixed model params; omitting them
# yields RunResult status=error and error_message "Model unavailable on …".
_FIXED_PARAMS: dict[str, tuple[Any, ...]] = {}
if _CURSOR_SDK:
    _FIXED_PARAMS = {
        "composer-2.5": (ModelParameterValue(id="fast", value="false"),),
        "grok-4.7": (
            ModelParameterValue(id="reasoning_effort", value="medium"),
            ModelParameterValue(id="fast", value="false"),
        ),
        "grok-4.6": (
            ModelParameterValue(id="effort", value="medium"),
            ModelParameterValue(id="fast", value="false"),
        ),
        "grok-4.5": (
            ModelParameterValue(id="effort", value="medium"),
            ModelParameterValue(id="fast", value="false"),
        ),
    }


def cursor_sdk_available() -> bool:
    return _CURSOR_SDK


def cursor_model_selection(model_id: str) -> Any:
    """Return a ModelSelection with required params, or the raw id if unknown / SDK missing."""
    mid = (model_id or "composer-2.5").strip()
    if not _CURSOR_SDK:
        return mid
    params = _FIXED_PARAMS.get(mid)
    if params:
        return ModelSelection(id=mid, params=params)
    return mid
