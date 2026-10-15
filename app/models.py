from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Game(Base):
    __tablename__ = "games"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(500))
    cover_url: Mapped[str | None] = mapped_column(String(1000))
    developer: Mapped[str | None] = mapped_column(String(500))
    publisher: Mapped[str | None] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    genres: Mapped[list[str]] = mapped_column(JSON, default=list)
    release_date: Mapped[date | None] = mapped_column(Date, index=True)
    video_url: Mapped[str | None] = mapped_column(String(1000))
    metacritic_url: Mapped[str | None] = mapped_column(String(1000))
    critic_summary: Mapped[dict | None] = mapped_column(JSON)
    user_summary: Mapped[dict | None] = mapped_column(JSON)
    reviews_hash: Mapped[str | None] = mapped_column(String(64))
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary)
    letsplay: Mapped[dict | None] = mapped_column(JSON)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    last_processed_date: Mapped[date | None] = mapped_column(Date, index=True)

    scores: Mapped[list["PlatformScore"]] = relationship(
        back_populates="game", cascade="all, delete-orphan"
    )


class PlatformScore(Base):
    __tablename__ = "platform_scores"
    __table_args__ = (UniqueConstraint("game_id", "platform"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), index=True)
    platform: Mapped[str] = mapped_column(String(100))
    metascore: Mapped[int | None] = mapped_column(Integer)
    userscore: Mapped[float | None] = mapped_column(Float)
    critic_count: Mapped[int | None] = mapped_column(Integer)
    user_count: Mapped[int | None] = mapped_column(Integer)

    game: Mapped[Game] = relationship(back_populates="scores")


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    trigger: Mapped[str] = mapped_column(String(20))  # schedule | manual
    status: Mapped[str] = mapped_column(String(20), default="running")
    source: Mapped[str | None] = mapped_column(String(20))  # new_releases | browse
    page: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    games_found: Mapped[int] = mapped_column(Integer, default=0)
    games_processed: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list] = mapped_column(JSON, default=list)


class DailyState(Base):
    __tablename__ = "daily_state"

    date: Mapped[date] = mapped_column(Date, primary_key=True)
    new_releases_done: Mapped[bool] = mapped_column(Boolean, default=False)
    next_browse_page: Mapped[int] = mapped_column(Integer, default=0)


class LlmUsage(Base):
    __tablename__ = "llm_usage"

    date: Mapped[date] = mapped_column(Date, primary_key=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
