"""Логика поиска и пересчёта на поддельных векторах (без модели)."""

import numpy as np

from app.models import Game
from app.scraper.schemas import GameData
from app.scraper.store import upsert_game
from app.similar.embeddings import from_bytes, game_text, refresh_embedding, to_bytes
from app.similar.search import similar_games


def add(db, slug, title, vec):
    game = Game(slug=slug, title=title, embedding=to_bytes(np.array(vec, dtype=np.float32)))
    db.add(game)
    db.flush()
    return game


def test_orders_by_cosine_and_respects_k(db):
    base = add(db, "a", "A", [1, 0, 0])
    near = add(db, "b", "B", [0.9, 0.1, 0])
    mid = add(db, "c", "C", [0.5, 0.5, 0])
    add(db, "d", "D", [0, 0, 1])

    assert [g.id for g in similar_games(db, base.id, k=2)] == [near.id, mid.id]
    assert len(similar_games(db, base.id)) == 3


def test_excludes_self_and_title_duplicates(db):
    base = add(db, "doom", "DOOM", [1, 0, 0])
    add(db, "doom-pc", "  doom ", [1, 0, 0])  # та же игра под другим slug
    twin_a = add(db, "quake", "Quake", [0.9, 0.1, 0])
    add(db, "quake-2", "QUAKE", [0.8, 0.2, 0])  # дубль среди результатов
    other = add(db, "farm", "Farm", [0, 1, 0])

    result = [g.id for g in similar_games(db, base.id)]
    assert result == [twin_a.id, other.id]


def test_games_without_embedding_are_skipped(db):
    base = add(db, "a", "A", [1, 0])
    db.add(Game(slug="b", title="B"))
    db.flush()
    assert similar_games(db, base.id) == []
    assert similar_games(db, 9999) == []


def test_new_game_takes_part_right_away(db):
    base = add(db, "a", "A", [1, 0])
    add(db, "b", "B", [0, 1])
    assert [g.title for g in similar_games(db, base.id)] == ["B"]
    add(db, "c", "C", [1, 0.1])
    assert [g.title for g in similar_games(db, base.id)] == ["C", "B"]


def test_game_text_format():
    game = Game(title="Doom", genres=["Shooter", "FPS"], description="Kill demons.")
    assert game_text(game) == "Doom. Shooter, FPS. Kill demons."
    assert game_text(Game(title="Doom", genres=[])) == "Doom"


def test_embedding_recomputed_only_when_text_changes(db):
    calls = []

    def fake(text):
        calls.append(text)
        return np.array([len(text), 1], dtype=np.float32)

    data = GameData(slug="g", title="G", genres=["RPG"], description="old")
    game = upsert_game(db, data)
    assert refresh_embedding(game, fake) is True
    assert from_bytes(game.embedding).dtype == np.float32

    # Ничего не поменялось — вектор остаётся.
    upsert_game(db, data)
    assert refresh_embedding(game, fake) is False

    # Поменялись оценки/обложка — тоже не пересчитываем.
    upsert_game(db, GameData(slug="g", title="G", genres=["RPG"], description="old", cover_url="x"))
    assert refresh_embedding(game, fake) is False

    # Сменилось описание — пересчёт.
    upsert_game(db, GameData(slug="g", title="G", genres=["RPG"], description="new"))
    assert refresh_embedding(game, fake) is True
    assert calls == ["G. RPG. old", "G. RPG. new"]
