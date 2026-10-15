"""Этап конвейера «Летсплей»: найти ролик, взять субтитры, сделать конспект, записать в Game."""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.letsplay.summarize import TranscriptTooShort, summarize_letsplay
from app.letsplay.youtube import LetsplayError, SearchResult, Video, find_letsplay, get_transcript
from app.llm.client import BudgetExceeded, LlmClient, LlmError
from app.models import Game
from app.worker.state import utcnow

log = logging.getLogger(__name__)

TIMEOUT_SEC = 60.0
RETRY_AFTER = timedelta(days=1)
STATUSES = ("ok", "not_found", "no_subtitles", "blocked", "error")


def needs_check(letsplay: dict | None, now: datetime) -> bool:
    """ok не пересчитываем; остальные статусы проверяем снова не чаще раза в сутки."""
    if not letsplay:
        return True
    if letsplay.get("status") == "ok":
        return False
    try:
        checked = datetime.fromisoformat(letsplay["checked_at"])
    except (KeyError, TypeError, ValueError):
        return True
    return now - checked >= RETRY_AFTER


def _record(status: str, now: datetime, video: Video | None = None, **extra: Any) -> dict:
    rec: dict[str, Any] = {
        "status": status,
        "video_id": video.video_id if video else None,
        "url": video.url if video else None,
        "title": video.title if video else None,
        "channel": video.channel if video else None,
        "views": video.views if video else None,
        "summary": None,
        "checked_at": now.isoformat(timespec="seconds"),
    }
    if video:
        rec["subtitles"] = f"{video.subtitle_lang}{' (авто)' if video.auto_subtitles else ''}"
    rec.update(extra)
    return rec


@dataclass
class LetsplayHook:
    """Хук для Deps.letsplay. Возвращает записанный статус или None, если проверка не нужна.

    Сеть и LLM идут в отдельном потоке с таймаутом: зависший YouTube не держит прогон.
    BudgetExceeded пробрасывается после записи статуса — конвейер выключит LLM.
    """

    timeout: float = TIMEOUT_SEC
    finder: Callable[[str], SearchResult] = find_letsplay
    transcriber: Callable[[Video], str] = get_transcript
    summarizer: Callable[[LlmClient, str, str], dict] = summarize_letsplay
    now: Callable[[], datetime] = field(default=utcnow)

    def __call__(self, db: Session, game: Game, llm: LlmClient | None) -> str | None:
        now = self.now()
        if not needs_check(game.letsplay, now):
            return None

        box: dict[str, Any] = {}

        def work() -> None:
            try:
                box["result"] = self.collect(game.title, llm, now)
            except BaseException as exc:  # передаём в основной поток
                box["exc"] = exc

        thread = threading.Thread(target=work, name=f"letsplay-{game.slug}", daemon=True)
        thread.start()
        thread.join(self.timeout)
        if thread.is_alive():
            game.letsplay = _record("error", now, error=f"таймаут {self.timeout:g} с")
            return "error"
        if "exc" in box:
            game.letsplay = _record("error", now, error=f"{type(box['exc']).__name__}")
            raise box["exc"]
        rec, reraise = box["result"]
        game.letsplay = rec
        if reraise is not None:
            raise reraise
        return rec["status"]

    def collect(
        self, title: str, llm: LlmClient | None, now: datetime
    ) -> tuple[dict, BaseException | None]:
        """(запись для Game.letsplay, исключение для конвейера или None)."""
        try:
            found = self.finder(title)
        except LetsplayError as exc:
            return _record(exc.status, now, error=str(exc)[:300]), None
        if found.video is None:
            return _record(found.reason or "not_found", now), None
        video = found.video

        try:
            transcript = self.transcriber(video)
        except LetsplayError as exc:
            return _record(exc.status, now, video, error=str(exc)[:300]), None

        if llm is None:
            return _record("error", now, video, error="резюме отключены в этом прогоне"), None
        try:
            summary = self.summarizer(llm, title, transcript)
        except TranscriptTooShort as exc:
            return _record("no_subtitles", now, video, error=str(exc)), None
        except BudgetExceeded as exc:
            return _record("error", now, video, error="дневной бюджет LLM исчерпан"), exc
        except LlmError as exc:
            return _record("error", now, video, error=f"LLM: {exc}"[:300]), None
        return _record("ok", now, video, summary=summary), None
