"""Map CURSOR_SDK_MODEL to SDK ModelSelection with plan-required params."""

from __future__ import annotations

from cursor_sdk import ModelParameterValue, ModelSelection

# Plus / Start / X Subscription tiers require fixed model params; omitting them
# yields RunResult status=error and error_message "Model unavailable on …".
_FIXED_PARAMS: dict[str, tuple[ModelParameterValue, ...]] = {
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


def cursor_model_selection(model_id: str) -> ModelSelection | str:
    """Return a ModelSelection with required params, or the raw id if unknown."""
    mid = (model_id or "composer-2.5").strip()
    params = _FIXED_PARAMS.get(mid)
    if params:
        return ModelSelection(id=mid, params=params)
    return mid
