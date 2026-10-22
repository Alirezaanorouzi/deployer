from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models import RiskTier, RunState


class PlanStep(BaseModel):
    name: str
    operation: str
    risk: RiskTier
    params: dict[str, Any] = Field(default_factory=dict)


class Plan(BaseModel):
    skill_id: str
    summary_fa: str = ""
    summary_en: str = ""
    steps: list[PlanStep]
    recommendation: str | None = None
    domain_playbook: list[str] | None = None


class ChatHistoryItem(BaseModel):
    role: str = Field(description="user or assistant")
    content: str = Field(max_length=4000)


class ChatRequest(BaseModel):
    message: str
    app: str | None = None
    target_env: str = "staging"
    health_url: str | None = None
    history: list[ChatHistoryItem] = Field(default_factory=list, max_length=24)


class PipelineStartRequest(BaseModel):
    repo: str
    subpath: str = ""
    liara_app_name: str | None = None


class RunStepOut(BaseModel):
    seq: int
    name: str
    risk: RiskTier
    status: str
    output: str | None

    class Config:
        from_attributes = True


class RunOut(BaseModel):
    id: str
    state: RunState
    user_message: str
    skill_id: str | None
    max_risk: RiskTier
    plan: Plan | None = None
    steps: list[RunStepOut] = Field(default_factory=list)
    error_code: str | None
    error_message: str | None
    result_summary: str | None
    created_at: datetime
    approved_at: datetime | None
    site_url: str | None = None
    repo: str | None = None
    liara_app_name: str | None = None
    agent_message: str | None = None

    class Config:
        from_attributes = True
