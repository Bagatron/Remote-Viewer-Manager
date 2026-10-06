from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .config import DATABASE_URL, DATA_DIR, SESSIONS_DIR, TRIALS_DIR

_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=_connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class RVSession(Base):
    __tablename__ = "rv_sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    name: Mapped[str] = mapped_column(String(255))
    original_filename: Mapped[str] = mapped_column(String(255))
    stored_filename: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|done|failed
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    notes: Mapped[str] = mapped_column(Text, default="")

    @property
    def params(self) -> dict:
        return json.loads(self.params_json or "{}")

    @property
    def metrics(self) -> dict:
        return json.loads(self.metrics_json or "{}")

    @property
    def dir(self):
        return SESSIONS_DIR / self.id


class RVTrial(Base):
    """One remote-viewing trial: coordinate -> sketch + confidence -> reveal -> self-score.

    A new table, so `create_all` adds it to an existing database without a migration.
    """

    __tablename__ = "rv_trials"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    coordinate: Mapped[str] = mapped_column(String(16), index=True)
    # assigned -> (judging) -> revealed -> complete ; or abandoned
    status: Mapped[str] = mapped_column(String(16), default="assigned")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    judging_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)   # 0-100, entered before reveal
    accuracy: Mapped[int | None] = mapped_column(Integer, nullable=True)     # 0-100, self-scored after reveal
    duration_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    n_pages: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str] = mapped_column(Text, default="")           # impressions written before the reveal
    feedback_notes: Mapped[str] = mapped_column(Text, default="")  # reflection written after the reveal
    target_json: Mapped[str] = mapped_column(Text, default="{}")   # hidden from the UI until revealed
    options_json: Mapped[str] = mapped_column(Text, default="[]")  # judging: shuffled target + decoys
    judged_choice: Mapped[str | None] = mapped_column(String(8), nullable=True)
    judged_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    @property
    def target(self) -> dict:
        return json.loads(self.target_json or "{}")

    @property
    def options(self) -> list:
        return json.loads(self.options_json or "[]")

    @property
    def dir(self):
        return TRIALS_DIR / self.id


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    TRIALS_DIR.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
