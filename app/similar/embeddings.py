"""Эмбеддинги игр: fastembed + BAAI/bge-small-en-v1.5 (ONNX, без torch)."""

import logging
import threading
from collections.abc import Callable

import numpy as np

from app.config import get_settings
from app.models import Game

log = logging.getLogger(__name__)

MODEL_NAME = "BAAI/bge-small-en-v1.5"
DIM = 384
# Модель сама обрезает вход до 512 токенов. Обрезка по символам — только чтобы
# не гонать через токенизатор огромные тексты (512 токенов ≈ 2–3 тыс. символов).
MAX_CHARS = 4000

Embedder = Callable[[str], np.ndarray]

_model = None
_model_lock = threading.Lock()


def get_model():
    """Ленивый синглтон: модель грузится один раз на процесс."""
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                from fastembed import TextEmbedding

                cache_dir = get_settings().models_dir
                log.info("Загружаю модель эмбеддингов %s (кэш %s)", MODEL_NAME, cache_dir)
                _model = TextEmbedding(model_name=MODEL_NAME, cache_dir=cache_dir)
    return _model


def embed(text: str) -> np.ndarray:
    """Нормированный вектор float32 длины DIM."""
    vec = next(iter(get_model().embed([text[:MAX_CHARS]])))
    return normalize(np.asarray(vec, dtype=np.float32))


def normalize(vec: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vec))
    return vec / norm if norm > 0 else vec


def game_text(game: Game) -> str:
    """Текст для эмбеддинга: "title. genres. description"."""
    parts = [game.title or "", ", ".join(game.genres or []), game.description or ""]
    return ". ".join(p.strip() for p in parts if p and p.strip())


def to_bytes(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def from_bytes(data: bytes) -> np.ndarray:
    return np.frombuffer(data, dtype=np.float32)


def refresh_embedding(game: Game, embedder: Embedder | None = None) -> bool:
    """Считает эмбеддинг, если его ещё нет.

    upsert_game сбрасывает Game.embedding, когда меняются название, жанры или описание,
    поэтому пересчёт идёт только для новых и изменившихся игр. Возвращает True, если считали.
    """
    if game.embedding is not None:
        return False
    text = game_text(game)
    if not text:
        return False
    game.embedding = to_bytes((embedder or embed)(text))
    return True
