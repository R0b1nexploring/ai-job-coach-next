from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid4().hex


class MaterialRevision(Base):
    """Immutable by application convention; no uploaded original is stored."""

    __tablename__ = "material_revisions"
    __table_args__ = (UniqueConstraint("material_id", "revision"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    material_id: Mapped[str] = mapped_column(String(32), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    scope_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    extracted_text: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255))
    # NULL is an unknown legacy ownership and must fail closed.
    retention: Mapped[str | None] = mapped_column(String(16))
    owner_batch_id: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class FactSnapshot(Base):
    """Frozen facts and their evidence references, not a confirmation shortcut."""

    __tablename__ = "fact_snapshots"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    scope_id: Mapped[str] = mapped_column(String(64), nullable=False)
    claims: Mapped[list[dict]] = mapped_column(JSON, nullable=False)
    # Frozen evidence copies preserve batch history if reusable source is removed.
    source_copies: Mapped[list[dict]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Batch(Base):
    __tablename__ = "batches"
    __table_args__ = (UniqueConstraint("idempotency_key"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    jd_text: Mapped[str] = mapped_column(Text, nullable=False)
    material_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    material_confirmed: Mapped[bool] = mapped_column(default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued")
    error_code: Mapped[str | None] = mapped_column(String(50))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    claim_token: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    plans: Mapped[dict | None] = mapped_column(JSON)
    # Optional initial header seed. Once resumes exist, each saved revision owns
    # its own header; exports never fall back to this batch-level value.
    profile: Mapped[dict | None] = mapped_column(JSON)
    # NULL means legacy v1; migration never derives facts from old records.
    schema_version: Mapped[int | None] = mapped_column(Integer)
    material_revision_ids: Mapped[list[str] | None] = mapped_column(JSON)
    fact_snapshot_id: Mapped[str | None] = mapped_column(String(32))
    target_snapshot: Mapped[dict | None] = mapped_column(JSON)
    generation_input_hash: Mapped[str | None] = mapped_column(String(100))
    resume_locale: Mapped[str | None] = mapped_column(String(16))
    guide_locale: Mapped[str | None] = mapped_column(String(16))
    generation_preferences: Mapped[dict | None] = mapped_column(JSON)
    resumes: Mapped[list[Resume]] = relationship(back_populates="batch", cascade="all, delete-orphan")
    guides: Mapped[list[Guide]] = relationship(back_populates="batch", cascade="all, delete-orphan")


class Resume(Base):
    __tablename__ = "resumes"
    __table_args__ = (UniqueConstraint("batch_id", "kind"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    batch_id: Mapped[str] = mapped_column(ForeignKey("batches.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    header: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict, server_default=text("'{}'"))
    items: Mapped[list[dict]] = mapped_column(JSON, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    semantic_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    plans: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    schema_version: Mapped[int | None] = mapped_column(Integer)
    document_v2: Mapped[dict | None] = mapped_column(JSON)
    batch: Mapped[Batch] = relationship(back_populates="resumes")


class ResumeRevision(Base):
    __tablename__ = "resume_revisions"
    __table_args__ = (UniqueConstraint("resume_id", "revision"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    resume_id: Mapped[str] = mapped_column(ForeignKey("resumes.id", ondelete="CASCADE"), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    header: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict, server_default=text("'{}'"))
    items: Mapped[list[dict]] = mapped_column(JSON, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    semantic_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    plans: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    schema_version: Mapped[int | None] = mapped_column(Integer)
    document_v2: Mapped[dict | None] = mapped_column(JSON)


class Guide(Base):
    __tablename__ = "guides"
    __table_args__ = (UniqueConstraint("batch_id", "kind"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    batch_id: Mapped[str] = mapped_column(ForeignKey("batches.id", ondelete="CASCADE"), nullable=False)
    resume_id: Mapped[str] = mapped_column(ForeignKey("resumes.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    semantic_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    resume_revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    sections: Mapped[dict | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(50))
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[str | None] = mapped_column(String(32))
    schema_version: Mapped[int | None] = mapped_column(Integer)
    guide_locale: Mapped[str | None] = mapped_column(String(16))
    guide_input_hash: Mapped[str | None] = mapped_column(String(100))
    batch: Mapped[Batch] = relationship(back_populates="guides")


class GuideRevision(Base):
    __tablename__ = "guide_revisions"
    __table_args__ = (UniqueConstraint("guide_id", "revision"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    guide_id: Mapped[str] = mapped_column(ForeignKey("guides.id", ondelete="CASCADE"), nullable=False)
    batch_id: Mapped[str] = mapped_column(ForeignKey("batches.id", ondelete="CASCADE"), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    resume_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    semantic_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    resume_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    plans: Mapped[list[dict]] = mapped_column(JSON, nullable=False)
    jd_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued")
    sections: Mapped[dict | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    schema_version: Mapped[int | None] = mapped_column(Integer)
    guide_locale: Mapped[str | None] = mapped_column(String(16))
    guide_input_hash: Mapped[str | None] = mapped_column(String(100))
    v2_snapshot: Mapped[dict | None] = mapped_column(JSON)
    coverage_unit_ids: Mapped[list[str] | None] = mapped_column(JSON)


class TaskExecution(Base):
    __tablename__ = "task_executions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    batch_id: Mapped[str] = mapped_column(ForeignKey("batches.id", ondelete="CASCADE"), nullable=False, index=True)
    guide_id: Mapped[str | None] = mapped_column(ForeignKey("guides.id", ondelete="CASCADE"))
    task_type: Mapped[str] = mapped_column(String(16), nullable=False)
    claim_token: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(50))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
