import re
from enum import Enum

from app.models import RiskTier


class OperationType(str, Enum):
    read = "read"
    deploy = "deploy"
    env_set = "env_set"
    restart = "restart"
    scale = "scale"
    create_app = "create_app"
    guide = "guide"


SECRET_KEY_PATTERN = re.compile(r"(PASSWORD|SECRET|TOKEN|API_KEY)", re.I)


def classify_step(
    operation: OperationType,
    *,
    target_env: str = "staging",
    env_key: str | None = None,
) -> RiskTier:
    if operation == OperationType.guide:
        return RiskTier.guide_only
    if operation in (OperationType.read,):
        return RiskTier.low
    if operation == OperationType.deploy and target_env != "prod":
        return RiskTier.medium
    if operation in (OperationType.deploy, OperationType.restart, OperationType.scale, OperationType.create_app):
        return RiskTier.high
    if operation == OperationType.env_set:
        if env_key and SECRET_KEY_PATTERN.search(env_key):
            return RiskTier.high
        return RiskTier.medium
    return RiskTier.medium


def max_risk(tiers: list[RiskTier]) -> RiskTier:
    order = [RiskTier.low, RiskTier.medium, RiskTier.high, RiskTier.guide_only]
    return max(tiers, key=lambda t: order.index(t))


def requires_approval(tier: RiskTier) -> bool:
    return tier in (RiskTier.high, RiskTier.medium)
