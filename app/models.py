import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class RunState(str, enum.Enum):
    queued = "queued"
    planning = "planning"
    awaiting_approval = "awaiting_approval"
    executing = "executing"
    verifying = "verifying"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


class RiskTier(str, enum.Enum):
    low = "low"
    medium = "medium"
    high = "high"
    guide_only = "guide_only"


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    user_message: Mapped[str] = mapped_column(Text, default="")
    state: Mapped[RunState] = mapped_column(Enum(RunState), default=RunState.queued)
    skill_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    plan_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    max_risk: Mapped[RiskTier] = mapped_column(Enum(RiskTier), default=RiskTier.low)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    site_url: Mapped[str | None] = mapped_column(String(512), nullable=True)

    steps: Mapped[list["RunStep"]] = relationship(back_populates="run", order_by="RunStep.seq")


class RunStep(Base):
    __tablename__ = "run_steps"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("runs.id"))
    seq: Mapped[int] = mapped_column()
    name: Mapped[str] = mapped_column(String(128))
    risk: Mapped[RiskTier] = mapped_column(Enum(RiskTier), default=RiskTier.low)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    output: Mapped[str | None] = mapped_column(Text, nullable=True)

    run: Mapped["Run"] = relationship(back_populates="steps")
