"""Run history + idempotency keys. Postgres in production (Neon), SQLite locally."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Integer, LargeBinary, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, deferred, mapped_column, sessionmaker

from . import config


def _db_url(url: str) -> str:
    # Neon / Render give "postgres://" or "postgresql://"; SQLAlchemy needs the psycopg driver name.
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Run(Base):
    """One attempt to process one inbound document."""
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex[:12])
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    channel: Mapped[str] = mapped_column(String(32), default="webhook")
    filename: Mapped[str] = mapped_column(String(255), default="document.pdf")
    file_sha256: Mapped[str] = mapped_column(String(64), index=True)
    # received | processing | success | needs_review | duplicate | partial | failed
    status: Mapped[str] = mapped_column(String(20), default="received", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    fields: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    missing: Mapped[list | None] = mapped_column(JSON, nullable=True)
    record_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    record_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    duplicate_of: Mapped[str | None] = mapped_column(String(32), nullable=True)
    replay_of: Mapped[str | None] = mapped_column(String(32), nullable=True)
    pdf: Mapped[bytes | None] = deferred(mapped_column(LargeBinary, nullable=True))


class DocKey(Base):
    """Idempotency claims. The primary key makes the claim atomic, even under concurrent duplicates.
    kind=file    -> key is the SHA-256 of the exact bytes
    kind=content -> key is last name|DOB|date of service|equipment (catches re-scans of the same order)
    """
    __tablename__ = "doc_keys"

    key: Mapped[str] = mapped_column(String(300), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    run_id: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


_url = _db_url(config.DATABASE_URL)
engine = create_engine(
    _url,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False} if _url.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)
