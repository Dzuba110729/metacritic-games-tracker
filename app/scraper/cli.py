"""Ручной запуск сборщика.

uv run python -m app.scraper.cli --once --dry-run   # 20 игр в консоль, базу не трогает
uv run python -m app.scraper.cli --once             # то же + запись игр в базу
"""

import argparse
import logging
import sys

from app.config import get_settings
from app.db import init_db, make_engine, make_sessionmaker
from app.scraper.api import GameNotFoundError, get_game
from app.scraper.client import MetacriticClient
from app.scraper.schemas import GameData
from app.scraper.selection import ListingSource, pick_batch, today_in
from app.scraper.store import upsert_game


def _fmt(value: float | int | None) -> str:
    return "tbd" if value is None else str(value)


def print_game(n: int, game: GameData) -> None:
    print(f"{n:2}. {game.title}  [{game.slug}]  {game.release_date or '-'}")
    print(f"    {game.developer or '-'} | {', '.join(game.genres) or '-'}")
    for p in game.platforms:
        print(
            f"    - {p.platform}: metascore {_fmt(p.metascore)} ({p.critic_count or 0} рец.), "
            f"userscore {_fmt(p.userscore)} ({p.user_count or 0} отз.)"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.scraper.cli")
    parser.add_argument("--once", action="store_true", help="один заход и выход")
    parser.add_argument("--dry-run", action="store_true", help="не писать в базу")
    args = parser.parse_args(argv)
    if not args.once:
        parser.error("пока поддерживается только --once (расписание — в воркере)")

    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # dry-run работает на базе в памяти: состояние дня и игры никуда не пишутся.
    db_url = "sqlite://" if args.dry_run else settings.database_url
    engine = make_engine(db_url)
    init_db(engine)
    today = today_in(settings.tz)

    with MetacriticClient(settings.metacritic_api_key) as client, make_sessionmaker(engine)() as db:
        batch = pick_batch(today, db, ListingSource.from_client(client))
        print(
            f"{today}: источник {batch.source}, страницы browse {batch.pages}, "
            f"игр {len(batch.slugs)}"
        )
        for n, slug in enumerate(batch.slugs, 1):
            try:
                game = get_game(client, slug)
            except GameNotFoundError:
                print(f"{n:2}. {slug}: не найдена")
                continue
            print_game(n, game)
            if not args.dry_run:
                upsert_game(db, game, processed_date=today)
                db.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
