"""Конвейер одного прогона: отбор игр -> данные Metacritic -> отзывы -> резюме -> эмбеддинг."""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.llm.client import BudgetExceeded, LlmApiError, LlmClient, LlmError
from app.llm.summarize import update_game_summaries
from app.models import Game, Run
from app.scraper.api import get_game, get_reviews
from app.scraper.client import MetacriticClient
from app.scraper.selection import ListingSource, pick_batch, today_in
from app.scraper.store import upsert_game
from app.similar.embeddings import Embedder, refresh_embedding
from app.worker.state import RunState, utcnow
from app.worker.state import state as default_state

log = logging.getLogger(__name__)

# Один прогон на процесс. Между процессами защищает проверка Run.status == "running".
RUN_LOCK = threading.Lock()
MAX_ERROR_CHARS = 300

# Хук летсплея (шаг 8): получает сессию и игру, сам пишет результат в game.letsplay.
LetsplayHook = Callable[[Session, Game], None]


@dataclass
class Deps:
    """Всё, что нужно прогону. В тестах подменяются клиенты Metacritic, LLM и эмбеддер."""

    settings: Settings
    factory: sessionmaker[Session]
    state: RunState = field(default_factory=lambda: default_state)
    metacritic: Callable[[], MetacriticClient] | None = None
    llm: Callable[[], LlmClient] | None = None
    embedder: Embedder | None = None
    letsplay: LetsplayHook | None = None
    today: Callable[[], date] | None = None

    def make_metacritic(self) -> MetacriticClient:
        if self.metacritic:
            return self.metacritic()
        return MetacriticClient(self.settings.metacritic_api_key)

    def make_llm(self) -> LlmClient | None:
        if self.llm:
            return self.llm()
        if not self.settings.anthropic_api_key.get_secret_value():
            return None
        return LlmClient(self.settings, self.factory)

    def get_today(self) -> date:
        return self.today() if self.today else today_in(self.settings.tz)


def _short(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:MAX_ERROR_CHARS]}"


class _Run:
    """Итоги текущего прогона. Пишет их в строку Run после каждой игры."""

    def __init__(self, deps: Deps, run_id: int):
        self.deps = deps
        self.state = deps.state
        self.run_id = run_id
        self.errors: list[dict[str, Any]] = []
        self.processed = 0
        self.llm: LlmClient | None = None
        self.paused_budget = False

    def save(self, **fields: Any) -> None:
        with self.deps.factory() as db:
            db.execute(update(Run).where(Run.id == self.run_id).values(**fields))
            db.commit()

    def save_progress(self) -> None:
        self.save(games_processed=self.processed, errors=list(self.errors))

    def error(self, slug: str | None, stage: str, exc: BaseException) -> None:
        self.errors.append({"slug": slug, "stage": stage, "error": _short(exc)})
        self.state.add("errors")
        self.state.event(f"{slug or 'прогон'}: ошибка на этапе {stage}: {_short(exc)}", "error")

    def disable_llm(self, reason: str, *, budget: bool = False) -> None:
        self.llm = None
        self.paused_budget = self.paused_budget or budget
        self.state.update(llm_enabled=False)
        self.state.event(f"Резюме отключены до конца прогона: {reason}", "warning")


def run(trigger: str, deps: Deps, *, lock_held: bool = False) -> int | None:
    """Один прогон. Возвращает id строки Run или None, если прогон уже идёт.

    lock_held=True — RUN_LOCK уже взят вызывающим (trigger_manual) и будет отпущен здесь.
    """
    if not lock_held and not RUN_LOCK.acquire(blocking=False):
        deps.state.event(f"Прогон ({trigger}) пропущен: предыдущий ещё идёт", "warning")
        return None
    try:
        return _run_locked(trigger, deps)
    finally:
        RUN_LOCK.release()


def _run_locked(trigger: str, deps: Deps) -> int | None:
    with deps.factory() as db:
        if db.scalar(select(Run.id).where(Run.status == "running").limit(1)) is not None:
            deps.state.event(f"Прогон ({trigger}) пропущен: в базе есть незавершённый", "warning")
            return None
        row = Run(trigger=trigger, status="running", started_at=utcnow(), errors=[])
        db.add(row)
        db.commit()
        run_id = row.id

    deps.state.start(run_id, trigger)
    deps.state.event(f"Прогон #{run_id} ({trigger}) начат")
    ctx = _Run(deps, run_id)
    status = "failed"
    try:
        _execute(ctx)
        status = "paused_budget" if ctx.paused_budget else "partial" if ctx.errors else "ok"
    except Exception as exc:
        log.exception("прогон #%s упал", run_id)
        ctx.error(None, deps.state.stage, exc)
    finally:
        ctx.save(
            status=status,
            finished_at=utcnow(),
            games_processed=ctx.processed,
            errors=list(ctx.errors),
        )
        deps.state.event(
            f"Прогон #{run_id} завершён: {status}, обработано {ctx.processed}, "
            f"ошибок {len(ctx.errors)}"
        )
        deps.state.finish()
    return run_id


def _execute(ctx: _Run) -> None:
    deps = ctx.deps
    today = deps.get_today()
    with deps.make_metacritic() as mc:
        with deps.factory() as db:
            batch = pick_batch(today, db, ListingSource.from_client(mc))
        ctx.save(
            source=batch.source,
            page=batch.pages[-1] if batch.pages else None,
            games_found=len(batch.slugs),
        )
        deps.state.update(games_found=len(batch.slugs))
        deps.state.event(
            f"Отобрано игр: {len(batch.slugs)} (источник {batch.source}, "
            f"страницы browse {batch.pages})"
        )

        _start_llm(ctx)
        for slug in batch.slugs:
            _process_game(ctx, mc, slug, today)
            ctx.save_progress()


def _start_llm(ctx: _Run) -> None:
    ctx.llm = ctx.deps.make_llm()
    if ctx.llm is None:
        ctx.disable_llm("ключ ANTHROPIC_API_KEY не задан")
        return
    try:
        ctx.llm.check_budget()
    except BudgetExceeded as exc:
        ctx.disable_llm(f"дневной бюджет исчерпан ({exc})", budget=True)


def _process_game(ctx: _Run, mc: MetacriticClient, slug: str, today: date) -> None:
    deps = ctx.deps
    st = ctx.state
    with deps.factory() as db:
        stage = "metacritic"
        st.set_stage(stage, slug)
        try:
            game = upsert_game(db, get_game(mc, slug))
            stage = "reviews"
            st.set_stage(stage)
            critic = get_reviews(mc, slug, "critic")
            user = get_reviews(mc, slug, "user")
            # Данные игры сохраняем сразу: ошибка LLM дальше их не откатит.
            db.commit()
        except Exception as exc:
            db.rollback()
            ctx.error(slug, stage, exc)
            return

        # Повторимая ошибка (сеть, 429, 5xx) — не помечаем игру, следующий заход возьмёт её снова.
        mark_processed = True
        if ctx.llm is not None:
            st.set_stage("llm")
            try:
                if update_game_summaries(ctx.llm, game, critic, user):
                    st.event(f"{slug}: резюме обновлены")
            except BudgetExceeded as exc:
                ctx.disable_llm(f"дневной бюджет исчерпан ({exc})", budget=True)
            except LlmError as exc:
                ctx.error(slug, "llm", exc)
                mark_processed = not exc.retryable
                if isinstance(exc, LlmApiError) and exc.status_code in (401, 403):
                    ctx.disable_llm("Anthropic отклонил ключ")

        st.set_stage("embedding")
        try:
            refresh_embedding(game, deps.embedder)
        except Exception as exc:
            ctx.error(slug, "embedding", exc)

        if deps.letsplay is not None:
            st.set_stage("letsplay")
            try:
                deps.letsplay(db, game)
            except Exception as exc:
                ctx.error(slug, "letsplay", exc)

        if mark_processed:
            game.last_processed_date = today
        db.commit()
    ctx.processed += 1
    st.update(games_processed=ctx.processed)


def mark_stale_runs(factory: sessionmaker[Session]) -> int:
    """После рестарта: прогоны со статусом running уже никто не ведёт — помечаем failed."""
    with factory() as db:
        result = db.execute(
            update(Run).where(Run.status == "running").values(status="failed", finished_at=utcnow())
        )
        db.commit()
        count = result.rowcount or 0
    if count:
        log.warning("помечено зависших прогонов как failed: %s", count)
    return count
