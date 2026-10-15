"""Живой прогон летсплея на играх из базы. Пишет результат в Game.letsplay.

uv run python -m app.letsplay.cli <slug> [<slug> ...] [--force]
"""

import argparse
import json
import logging

from app.config import get_settings
from app.db import init_db, make_engine, make_sessionmaker
from app.letsplay.service import LetsplayHook
from app.llm.client import LlmClient
from app.models import Game


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.letsplay.cli")
    parser.add_argument("slugs", nargs="+", help="slug игр из базы")
    parser.add_argument("--force", action="store_true", help="проверить заново, даже если ok")
    args = parser.parse_args(argv)

    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(levelname)s %(name)s: %(message)s")
    engine = make_engine(settings.database_url)
    init_db(engine)
    factory = make_sessionmaker(engine)
    llm = LlmClient(settings, factory) if settings.anthropic_api_key.get_secret_value() else None
    hook = LetsplayHook()

    for slug in args.slugs:
        with factory() as db:
            game = db.query(Game).filter_by(slug=slug).one_or_none()
            if game is None:
                print(f"{slug}: нет в базе")
                continue
            if args.force:
                game.letsplay = None
            status = hook(db, game, llm)
            db.commit()
            print(f"\n== {game.title}: {status or 'проверка не нужна'} ==")
            print(json.dumps(game.letsplay, ensure_ascii=False, indent=2))
    if llm is not None:
        print(f"\nТокены: вход {llm.input_tokens}, выход {llm.output_tokens}, ${llm.cost_usd:.5f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
