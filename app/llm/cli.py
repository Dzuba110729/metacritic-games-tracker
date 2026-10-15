"""Живой прогон резюме на одной игре.

uv run python -m app.llm.cli <slug>
"""

import argparse
import logging
import sys

from app.config import get_settings
from app.db import init_db, make_engine, make_sessionmaker
from app.llm.client import LlmClient, LlmError, spent_today
from app.llm.summarize import ReviewSummary, summarize_reviews
from app.scraper.api import get_game, get_reviews
from app.scraper.client import MetacriticClient
from app.scraper.selection import today_in

TITLES = {"critic": "Критики", "user": "Игроки"}


def print_summary(kind: str, s: ReviewSummary) -> None:
    print(f"\n== {TITLES[kind]} (отзывов: {s.based_on}) ==")
    if s.status == "few_reviews":
        print("Мало отзывов — резюме не строим.")
        return
    print("Понравилось:")
    for p in s.likes:
        print(f"  + {p}")
    print("Не понравилось:")
    for p in s.dislikes:
        print(f"  - {p}")
    print(f"Итог: {s.verdict}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.llm.cli")
    parser.add_argument("slug", help="slug игры на Metacritic, например borderlands-4")
    args = parser.parse_args(argv)

    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    engine = make_engine(settings.database_url)
    init_db(engine)
    factory = make_sessionmaker(engine)
    llm = LlmClient(settings, factory)

    with MetacriticClient(settings.metacritic_api_key) as mc:
        game = get_game(mc, args.slug)
        critic = get_reviews(mc, args.slug, "critic")
        user = get_reviews(mc, args.slug, "user")
    print(f"{game.title}: отзывов критиков {len(critic)}, игроков {len(user)}")

    try:
        print_summary("critic", summarize_reviews(llm, game.title, critic, "critic"))
        print_summary("user", summarize_reviews(llm, game.title, user, "user"))
    except LlmError as e:
        print(f"Ошибка LLM: {e}", file=sys.stderr)
        return 1
    finally:
        with factory() as db:
            day_total = spent_today(db, today_in(settings.tz))
        print(
            f"\nТокены: вход {llm.input_tokens}, выход {llm.output_tokens}. "
            f"Стоимость прогона ${llm.cost_usd:.5f}. "
            f"За сегодня ${day_total:.5f} из ${settings.llm_daily_budget_usd:.2f}."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
