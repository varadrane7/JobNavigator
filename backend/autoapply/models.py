"""AutoApply tables. Same declarative Base as backend/models/db.py so create_tables() builds
them, but every table is autoapply_* and nothing upstream references them."""
import uuid

from sqlalchemy import (
    Column, String, Integer, Float, Boolean, Text, DateTime, Date, ForeignKey, JSON, Index,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from backend.models.db import Base, utcnow

# Lifecycle of one queued job. Terminal states never go back into the queue.
STATUSES = (
    "queued",       # picked for today, waiting for review
    "approved",     # review passed (or auto mode) -> prepare next
    "preparing",    # tailoring / PDF / cover letter in progress
    "ready",        # documents built, waiting for the pacing worker
    "submitting",   # a submitter is driving the form
    "submitted",    # confirmed; Application row written
    "dry_run_ok",   # form filled, submit deliberately not clicked
    "needs_human",  # captcha / login wall / unanswered required question
    "failed",       # gave up after retries
    "rejected",     # user said no in review
    "skipped",      # dropped by caps or rules after queueing
)
TERMINAL = ("submitted", "failed", "rejected", "skipped")
IN_FLIGHT = ("approved", "preparing", "ready", "submitting")


class AutoApplyItem(Base):
    __tablename__ = "autoapply_items"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # One item per job, ever: a rejected or failed job is not offered again on later days.
    job_id = Column(UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"),
                    nullable=False, unique=True)
    run_date = Column(Date, nullable=False, index=True)
    rank = Column(Integer, nullable=True)
    score = Column(Float, nullable=True)
    base_resume_id = Column(UUID(as_uuid=True), ForeignKey("resumes.id", ondelete="SET NULL"), nullable=True)
    base_resume_name = Column(String, nullable=True)
    submitter = Column(String, nullable=False)   # greenhouse|lever|ashby|workday|generic|linkedin|ziprecruiter
    site_host = Column(String, nullable=True)
    # Where the form actually lives when it differs from Job.url (a careers page that hands off to an ATS).
    apply_url = Column(String, nullable=True)
    company_key = Column(String, nullable=True, index=True)
    status = Column(String, nullable=False, default="queued", index=True)

    tailored_resume_id = Column(UUID(as_uuid=True), ForeignKey("resumes.id", ondelete="SET NULL"), nullable=True)
    cover_letter_id = Column(UUID(as_uuid=True), ForeignKey("cover_letters.id", ondelete="SET NULL"), nullable=True)
    resume_pdf_path = Column(String, nullable=True)
    cl_pdf_path = Column(String, nullable=True)
    application_id = Column(UUID(as_uuid=True), ForeignKey("applications.id", ondelete="SET NULL"), nullable=True)
    # Why the tailored copy was not used, or anything else the reviewer should see.
    flags = Column(JSON, default=list)

    error = Column(Text, nullable=True)
    attempts = Column(Integer, default=0)
    next_attempt_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    submitted_at = Column(DateTime(timezone=True), nullable=True)

    job = relationship("Job")
    attempts_log = relationship("AutoApplyAttempt", back_populates="item",
                                cascade="all, delete-orphan", order_by="AutoApplyAttempt.started_at")


class AutoApplyAttempt(Base):
    """One submitter run against one item: what was filled (receipt) and the proof screenshots."""
    __tablename__ = "autoapply_attempts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    item_id = Column(UUID(as_uuid=True), ForeignKey("autoapply_items.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    started_at = Column(DateTime(timezone=True), default=utcnow)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    dry_run = Column(Boolean, default=True)
    outcome = Column(String, nullable=True)
    step_log = Column(JSON, default=list)
    receipt = Column(JSON, default=dict)        # {field label: value submitted}
    screenshots = Column(JSON, default=list)    # file paths under autoapply_data_dir
    confirmation_text = Column(Text, nullable=True)
    llm_calls = Column(Integer, default=0)

    item = relationship("AutoApplyItem", back_populates="attempts_log")


class AutoApplyQuestion(Base):
    """A question a submitter could not answer on its own; answered ones feed Persona.qa_bank."""
    __tablename__ = "autoapply_questions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    item_id = Column(UUID(as_uuid=True), ForeignKey("autoapply_items.id", ondelete="CASCADE"),
                     nullable=True, index=True)
    question = Column(Text, nullable=False)
    field_type = Column(String, nullable=True)
    options = Column(JSON, default=list)
    suggested_answer = Column(Text, nullable=True)
    answer = Column(Text, nullable=True)
    status = Column(String, default="open", index=True)   # open | answered | dismissed
    created_at = Column(DateTime(timezone=True), default=utcnow)
    answered_at = Column(DateTime(timezone=True), nullable=True)

    item = relationship("AutoApplyItem")


Index("ix_autoapply_items_date_status", AutoApplyItem.run_date, AutoApplyItem.status)
