from datetime import date

from sqlalchemy import func, select

from app.models import Game, PlatformScore
from app.scraper.schemas import GameData, PlatformScoreData
from app.scraper.store import upsert_game


def make(**kw) -> GameData:
    base = dict(
        slug="g",
        title="Game",
        description="desc",
        genres=["RPG"],
        release_date=date(2026, 10, 1),
        platforms=[
            PlatformScoreData("PC", "pc", metascore=80, userscore=7.5, critic_count=10),
            PlatformScoreData("PlayStation 5", "playstation-5", metascore=None),
        ],
    )
    base.update(kw)
    return GameData(**base)


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def test_upsert_creates_game_and_platforms(db):
    game = upsert_game(db, make(), processed_date=date(2026, 10, 15))
    db.commit()
    assert game.id
    assert game.last_processed_date == date(2026, 10, 15)
    assert {s.platform: s.metascore for s in game.scores} == {"PC": 80, "PlayStation 5": None}


def test_upsert_twice_no_duplicates_and_updates_scores(db):
    upsert_game(db, make())
    db.commit()
    upsert_game(
        db,
        make(
            title="Game (new)",
            platforms=[
                PlatformScoreData("PC", "pc", metascore=82, userscore=8.0),
                PlatformScoreData("PlayStation 5", "playstation-5", metascore=75),
            ],
        ),
    )
    db.commit()
    assert count(db, Game) == 1
    assert count(db, PlatformScore) == 2
    game = db.scalar(select(Game))
    assert game.title == "Game (new)"
    assert {s.platform: s.metascore for s in game.scores} == {"PC": 82, "PlayStation 5": 75}


def test_upsert_keeps_known_fields_when_new_value_empty(db):
    upsert_game(db, make())
    db.commit()
    upsert_game(db, make(description=None, genres=[]))
    db.commit()
    game = db.scalar(select(Game))
    assert game.description == "desc"
    assert game.genres == ["RPG"]


def test_upsert_removes_dropped_platform(db):
    upsert_game(db, make())
    db.commit()
    upsert_game(db, make(platforms=[PlatformScoreData("PC", "pc", metascore=80)]))
    db.commit()
    assert count(db, PlatformScore) == 1


def test_upsert_without_processed_date_keeps_old(db):
    upsert_game(db, make(), processed_date=date(2026, 10, 14))
    db.commit()
    upsert_game(db, make())
    db.commit()
    assert db.scalar(select(Game)).last_processed_date == date(2026, 10, 14)


def test_upsert_keeps_only_metacritic_covers(db):
    good = "https://www.metacritic.com/a/img/catalog/provider/7/2/x.jpg"
    game = upsert_game(db, make(slug="c1", cover_url=good))
    assert game.cover_url == good
    for bad in ("https://evil.example/x.jpg", "http://www.metacritic.com/x.jpg", "javascript:1"):
        game = upsert_game(db, make(slug="c2", cover_url=bad))
        assert game.cover_url is None
