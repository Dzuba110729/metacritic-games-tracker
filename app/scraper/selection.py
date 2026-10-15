"""Отбор игр на один заход: до 20 slug, которые сегодня ещё не обрабатывались."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import DailyState, Game
from app.scraper.api import list_browse_page, list_new_releases
from app.scraper.client import MetacriticClient

BATCH_SIZE = 20
MAX_BROWSE_PAGES = 5


@dataclass
class Batch:
    slugs: list[str]
    source: str  # new_releases | browse
    pages: list[int] = field(default_factory=list)  # просмотренные страницы browse


@dataclass
class ListingSource:
    """Откуда брать списки slug. В проде — функции из app.scraper.api, в тестах — заглушки."""

    new_releases: Callable[[], list[str]]
    browse_page: Callable[[int], list[str]]

    @classmethod
    def from_client(cls, client: MetacriticClient) -> "ListingSource":
        return cls(
            new_releases=lambda: list_new_releases(client),
            browse_page=lambda page: list_browse_page(client, page),
        )


def today_in(tz: str) -> date:
    """«Сегодня» по часовому поясу сервиса (TZ, по умолчанию Europe/Moscow)."""
    return datetime.now(ZoneInfo(tz)).date()


def _daily_state(db: Session, today: date) -> DailyState:
    state = db.get(DailyState, today)
    if state is None:
        # Новый день: старые состояния больше не нужны, начинаем с New Releases.
        db.execute(delete(DailyState).where(DailyState.date != today))
        state = DailyState(date=today, new_releases_done=False, next_browse_page=1)
        db.add(state)
        db.flush()
    return state


def _processed_today(db: Session, today: date) -> set[str]:
    rows = db.scalars(select(Game.slug).where(Game.last_processed_date == today))
    return set(rows)


def pick_batch(today: date, db: Session, source: ListingSource, size: int = BATCH_SIZE) -> Batch:
    """До size slug, которых нет среди обработанных сегодня.

    Первый заход дня берёт блок New Releases, следующие — очередную страницу browse.
    Если на странице всё уже обработано, идём дальше, но не больше MAX_BROWSE_PAGES страниц.
    Страница, на которой остались необработанные игры, остаётся «текущей» для следующего захода.
    Состояние дня сохраняется (commit).
    """
    state = _daily_state(db, today)
    done = _processed_today(db, today)
    picked: list[str] = []

    def take(slugs: list[str]) -> bool:
        """Добавляет новые slug. True, если на странице остались неиспользованные."""
        left = False
        for slug in slugs:
            if slug in done or slug in picked:
                continue
            if len(picked) < size:
                picked.append(slug)
            else:
                left = True
        return left

    batch_source = "browse"
    if not state.new_releases_done:
        take(source.new_releases())
        state.new_releases_done = True
        batch_source = "new_releases"

    pages: list[int] = []
    page = max(state.next_browse_page, 1)
    while len(picked) < size and len(pages) < MAX_BROWSE_PAGES:
        slugs = source.browse_page(page)
        pages.append(page)
        left = take(slugs)
        if not slugs:
            break  # дошли до конца каталога
        if left:
            break  # страница не исчерпана — вернёмся к ней в следующий раз
        page += 1
    state.next_browse_page = page
    db.commit()
    return Batch(slugs=picked, source=batch_source, pages=pages)
