from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


ID_TYPE = BigInteger().with_variant(Integer, "sqlite")


class RequestStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    processing = "processing"
    waiting_inventory = "waiting_inventory"
    generated = "generated"
    delivered = "delivered"
    rejected = "rejected"
    canceled = "canceled"
    failed = "failed"


class JobStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    complete = "complete"
    failed = "failed"


class Agency(Base):
    __tablename__ = "agencies"
    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Agent(Base):
    __tablename__ = "agents"
    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    agency_id: Mapped[int | None] = mapped_column(ForeignKey("agencies.id", ondelete="SET NULL"), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    agency: Mapped[Agency | None] = relationship()


class CustomerProfile(Base):
    __tablename__ = "customer_profiles"
    user_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    first_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    last_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    phone: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    licensed_states: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"), index=True)
    mapping_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    agent: Mapped[Agent | None] = relationship()


class LeadRequest(Base):
    __tablename__ = "lead_requests"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customer_profiles.user_id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id", ondelete="RESTRICT"), index=True)
    lead_count: Mapped[int] = mapped_column(Integer, nullable=False)
    state_mode: Mapped[str] = mapped_column(String(20), default="all_saved", nullable=False)
    states_snapshot: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    delivery_email: Mapped[str] = mapped_column(String(320), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default=RequestStatus.pending.value, index=True, nullable=False)
    available_count: Mapped[int | None] = mapped_column(Integer)
    status_message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True, nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    profile: Mapped[CustomerProfile] = relationship()
    agent: Mapped[Agent] = relationship()
    artifact: Mapped[BatchArtifact | None] = relationship(back_populates="request", uselist=False)


class Lead(Base):
    __tablename__ = "lead_inventory"
    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    phone: Mapped[str] = mapped_column(String(10), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(Text, default="", nullable=False)
    state: Mapped[str] = mapped_column(String(2), index=True, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    source_flow: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    last_distributed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    __table_args__ = (Index("lead_inventory_state_age_idx", "state", "last_distributed_at", "id"),)


class LeadSource(Base):
    __tablename__ = "lead_sources"
    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("lead_inventory.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    source_key: Mapped[str] = mapped_column(String(200), nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (UniqueConstraint("source", "source_key", name="uq_lead_source_key"),)


class DistributionEvent(Base):
    __tablename__ = "distribution_events"
    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("lead_inventory.id", ondelete="RESTRICT"), index=True)
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"), index=True)
    request_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("lead_requests.id", ondelete="SET NULL"), index=True)
    delivered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True, nullable=False)
    source: Mapped[str] = mapped_column(String(80), default="request", nullable=False)
    __table_args__ = (
        UniqueConstraint("request_id", "lead_id", name="uq_request_lead"),
        UniqueConstraint("lead_id", "agent_id", "delivered_at", "source", name="uq_legacy_distribution_event"),
    )


class BatchArtifact(Base):
    __tablename__ = "batch_artifacts"
    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    request_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("lead_requests.id", ondelete="CASCADE"), unique=True)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    byte_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    delivery_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    delivery_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    resend_message_id: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    request: Mapped[LeadRequest] = relationship(back_populates="artifact")


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(60), index=True, nullable=False)
    request_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("lead_requests.id", ondelete="CASCADE"), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default=JobStatus.queued.value, index=True, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True, nullable=False)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class SlackNotification(Base):
    __tablename__ = "slack_notifications"
    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    request_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("lead_requests.id", ondelete="CASCADE"), unique=True)
    channel_id: Mapped[str] = mapped_column(String(80), nullable=False)
    message_ts: Mapped[str] = mapped_column(String(80), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class WebhookReceipt(Base):
    __tablename__ = "webhook_receipts"
    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    event_key: Mapped[str] = mapped_column(String(160), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (UniqueConstraint("provider", "event_key", name="uq_webhook_receipt"),)


class ScraperRun(Base):
    __tablename__ = "scraper_runs"
    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    source_version: Mapped[str] = mapped_column(String(255), nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    rows_seen: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    rows_imported: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    details: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MigrationAudit(Base):
    __tablename__ = "migration_audits"
    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    source_rows: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    imported_rows: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    quarantined_rows: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (UniqueConstraint("source_path", "checksum", name="uq_migration_source_checksum"),)


class QuarantinedRow(Base):
    __tablename__ = "quarantined_rows"
    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    row_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    raw_data: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
