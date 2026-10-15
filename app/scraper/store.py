from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Game, PlatformScore
from app.scraper.schemas import GameData

GAME_FIELDS = (
    "title",
    "cover_url",
    "developer",
    "publisher",
    "description",
    "genres",
    "release_date",
    "video_url",
    "metacritic_url",
)
# Поля, из которых строится текст эмбеддинга (app/similar/embeddings.game_text).
EMBEDDING_FIELDS = {"title", "genres", "description"}


def upsert_game(db: Session, data: GameData, processed_date: date | None = None) -> Game:
    """Создаёт или обновляет игру (по slug) и её оценки по платформам. Без commit."""
    game = db.scalar(select(Game).where(Game.slug == data.slug))
    if game is None:
        game = Game(slug=data.slug)
        db.add(game)

    for name in GAME_FIELDS:
        value = getattr(data, name)
        # Пустое значение не затирает уже известное (например, сайт временно не отдал описание).
        if value or getattr(game, name) is None:
            if name in EMBEDDING_FIELDS and value != getattr(game, name):
                # Текст для эмбеддинга изменился: refresh_embedding посчитает вектор заново.
                game.embedding = None
            setattr(game, name, value)

    by_platform = {s.platform: s for s in game.scores}
    seen = set()
    for p in data.platforms:
        if p.platform in seen:
            continue
        seen.add(p.platform)
        score = by_platform.get(p.platform)
        if score is None:
            score = PlatformScore(platform=p.platform)
            game.scores.append(score)
        score.metascore = p.metascore
        score.userscore = p.userscore
        score.critic_count = p.critic_count
        score.user_count = p.user_count
    if seen:
        # Платформу убрали с сайта — убираем и у нас (delete-orphan).
        game.scores = [s for s in game.scores if s.platform in seen]

    if processed_date is not None:
        game.last_processed_date = processed_date
    db.flush()
    return game
