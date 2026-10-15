"""Состояние воркера в памяти для страницы мониторинга. Все методы потокобезопасны."""

import logging
import threading
from collections import deque
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger("app.worker")

MAX_EVENTS = 50


def utcnow() -> datetime:
    """Наивное UTC-время: так же хранит даты SQLite (CURRENT_TIMESTAMP)."""
    return datetime.now(UTC).replace(tzinfo=None)


class RunState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)
        self._reset()

    def _reset(self) -> None:
        self.running = False
        self.run_id: int | None = None
        self.trigger: str | None = None
        self.started_at: datetime | None = None
        self.stage = "idle"
        self.current_game: str | None = None
        self.games_found = 0
        self.games_processed = 0
        self.errors = 0
        self.llm_enabled = True

    def start(self, run_id: int, trigger: str) -> None:
        with self._lock:
            self._reset()
            self.running = True
            self.run_id = run_id
            self.trigger = trigger
            self.started_at = utcnow()
            self.stage = "pick_batch"

    def finish(self) -> None:
        with self._lock:
            self.running = False
            self.stage = "idle"
            self.current_game = None

    def set_stage(self, stage: str, game: str | None = None) -> None:
        with self._lock:
            self.stage = stage
            if game is not None:
                self.current_game = game

    def update(self, **counters: Any) -> None:
        with self._lock:
            for name, value in counters.items():
                setattr(self, name, value)

    def add(self, name: str, n: int = 1) -> None:
        with self._lock:
            setattr(self, name, getattr(self, name) + n)

    def event(self, message: str, level: str = "info") -> None:
        """Событие для ленты мониторинга. Дублируется в обычный лог."""
        log.log(logging.getLevelName(level.upper()), message)
        with self._lock:
            self._events.append({"ts": utcnow(), "level": level, "message": message})

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self.running,
                "run_id": self.run_id,
                "trigger": self.trigger,
                "started_at": self.started_at,
                "stage": self.stage,
                "current_game": self.current_game,
                "games_found": self.games_found,
                "games_processed": self.games_processed,
                "errors": self.errors,
                "llm_enabled": self.llm_enabled,
                "events": list(self._events),
            }


# Один на процесс: воркер пишет, веб-страница мониторинга читает.
state = RunState()
