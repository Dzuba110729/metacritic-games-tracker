"""Запросы для страниц: список игр с поиском, фильтром, сортировкой и пагинацией."""

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session, selectinload

from app.models import Game, PlatformScore

PAGE_SIZE = 24
MAX_QUERY_LEN = 100

Sort = Literal["new", "metascore", "userscore"]
SORTS: dict[str, str] = {
    "new": "Сначала новые",
    "metascore": "Оценка критиков",
    "userscore": "Оценка игроков",
}


@dataclass
class GameRow:
    game: Game
    metascore: int | None
    userscore: float | None


@dataclass
class GamePage:
    rows: list[GameRow]
    total: int
    page: int
    pages: int


def platforms(db: Session) -> list[str]:
    """Платформы, которые есть в базе, по числу игр."""
    count = func.count(PlatformScore.id)
    return list(
        db.scalars(
            select(PlatformScore.platform)
            .group_by(PlatformScore.platform)
            .order_by(count.desc(), PlatformScore.platform)
        )
    )


def _best(column) -> Select:
    return (
        select(func.max(column))
        .where(PlatformScore.game_id == Game.id)
        .correlate(Game)
        .scalar_subquery()
    )


def list_games(db: Session, q: str = "", platform: str = "", sort: str = "new", page: int = 1):
    metascore = _best(PlatformScore.metascore).label("metascore")
    userscore = _best(PlatformScore.userscore).label("userscore")
    stmt = select(Game, metascore, userscore)

    q = q.strip()[:MAX_QUERY_LEN]
    if q:
        # autoescape: '%' и '_' в запросе ищутся как обычные символы.
        stmt = stmt.where(func.casefold(Game.title).contains(q.casefold(), autoescape=True))
    if platform:
        stmt = stmt.where(
            select(PlatformScore.id)
            .where(PlatformScore.game_id == Game.id, PlatformScore.platform == platform)
            .exists()
        )

    if sort == "metascore":
        order = [metascore.desc().nulls_last()]
    elif sort == "userscore":
        order = [userscore.desc().nulls_last()]
    else:
        order = []
    order += [Game.release_date.desc().nulls_last(), Game.first_seen_at.desc(), Game.id.desc()]

    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    pages = max(1, -(-total // PAGE_SIZE))
    page = min(max(1, page), pages)
    result = db.execute(
        stmt.options(selectinload(Game.scores))
        .order_by(*order)
        .limit(PAGE_SIZE)
        .offset((page - 1) * PAGE_SIZE)
    ).all()
    rows = [GameRow(game=r[0], metascore=r.metascore, userscore=r.userscore) for r in result]
    return GamePage(rows=rows, total=total, page=page, pages=pages)


def get_game(db: Session, slug: str) -> Game | None:
    return db.scalar(select(Game).options(selectinload(Game.scores)).where(Game.slug == slug))
