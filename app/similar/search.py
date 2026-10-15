"""Похожие игры по косинусной близости эмбеддингов."""

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Game
from app.similar.embeddings import from_bytes


def _title_key(title: str | None) -> str:
    return " ".join((title or "").casefold().split())


def similar_games(db: Session, game_id: int, k: int = 5) -> list[Game]:
    """До k самых похожих игр. Без кэша: новые игры участвуют сразу.

    Исключаем саму игру и игры с тем же названием (одна игра под разными slug),
    среди результатов тоже оставляем по одной игре на название.
    """
    game = db.get(Game, game_id)
    if game is None or game.embedding is None:
        return []

    rows = db.execute(
        select(Game.id, Game.title, Game.embedding).where(
            Game.embedding.is_not(None), Game.id != game_id
        )
    ).all()
    if not rows:
        return []

    matrix = np.stack([from_bytes(r.embedding) for r in rows])
    matrix /= np.clip(np.linalg.norm(matrix, axis=1, keepdims=True), 1e-12, None)
    query = from_bytes(game.embedding)
    query = query / max(float(np.linalg.norm(query)), 1e-12)
    scores = matrix @ query

    seen = {_title_key(game.title)}
    picked: list[int] = []
    for idx in np.argsort(-scores, kind="stable"):
        key = _title_key(rows[idx].title)
        if key in seen:
            continue
        seen.add(key)
        picked.append(rows[idx].id)
        if len(picked) == k:
            break

    by_id = {g.id: g for g in db.scalars(select(Game).where(Game.id.in_(picked)))}
    return [by_id[i] for i in picked]
