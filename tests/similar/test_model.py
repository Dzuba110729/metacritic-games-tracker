"""Проверки на настоящей модели bge-small-en-v1.5 (кэш в data/models)."""

import subprocess
import sys

import numpy as np

from app.models import Game
from app.similar.embeddings import DIM, embed, refresh_embedding
from app.similar.search import similar_games

MEMORY_LIMIT_MB = 600


def test_two_shooters_and_a_farm(db):
    games = [
        Game(
            slug="doom-eternal",
            title="Doom Eternal",
            genres=["First-Person Shooter", "Action"],
            description="Fight hordes of demons with shotguns and rocket launchers "
            "in fast-paced first-person combat across hell-invaded Earth.",
        ),
        Game(
            slug="sunny-acres",
            title="Sunny Acres",
            genres=["Simulation", "Farming"],
            description="Grow crops, raise chickens and cows, and sell vegetables "
            "at the village market in this relaxing countryside farming sim.",
        ),
        Game(
            slug="battlefront-zero",
            title="Battlefront Zero",
            genres=["Shooter", "Military"],
            description="Squad-based military shooter: aim, fire assault rifles and "
            "snipe enemy soldiers in large multiplayer battles.",
        ),
    ]
    db.add_all(games)
    db.flush()
    for g in games:
        assert refresh_embedding(g)
    db.flush()

    doom, farm, battlefront = games
    assert similar_games(db, doom.id)[0].id == battlefront.id
    assert similar_games(db, battlefront.id)[0].id == doom.id


def test_vector_is_normalized_float32():
    vec = embed("Doom. Shooter. Demons.")
    assert vec.shape == (DIM,)
    assert vec.dtype == np.float32
    assert abs(float(np.linalg.norm(vec)) - 1) < 1e-4


def test_long_text_is_truncated_not_failing():
    assert embed("word " * 5000).shape == (DIM,)


def test_process_memory_after_model_load_under_limit():
    # Отдельный процесс: в процессе pytest память занята и другими тестами.
    out = subprocess.run(
        [sys.executable, "-m", "app.similar.cli", "memory"],
        capture_output=True,
        text=True,
        check=True,
        timeout=300,
    ).stdout
    peak = float(out.strip().splitlines()[-1].split("=")[1])
    assert peak < MEMORY_LIMIT_MB, f"пиковая память {peak} МБ"
