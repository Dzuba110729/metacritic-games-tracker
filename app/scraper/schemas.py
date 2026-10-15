from dataclasses import dataclass, field
from datetime import date
from typing import Literal

ReviewKind = Literal["critic", "user"]
Sentiment = Literal["positive", "neutral", "negative"]


@dataclass
class PlatformScoreData:
    platform: str  # отображаемое имя: "PlayStation 5"
    slug: str  # "playstation-5"
    metascore: int | None = None
    userscore: float | None = None
    critic_count: int | None = None
    user_count: int | None = None


@dataclass
class GameData:
    slug: str
    title: str
    cover_url: str | None = None
    developer: str | None = None
    publisher: str | None = None
    description: str | None = None
    genres: list[str] = field(default_factory=list)
    release_date: date | None = None
    video_url: str | None = None
    metacritic_url: str | None = None
    platforms: list[PlatformScoreData] = field(default_factory=list)


@dataclass
class Review:
    kind: ReviewKind
    sentiment: Sentiment
    score: float | None
    quote: str
    author: str | None = None
    publication: str | None = None
    date: str | None = None
    platform: str | None = None
