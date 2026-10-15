"""Страница мониторинга: живое состояние воркера (SSE), последние прогоны, ручной запуск."""

import asyncio
import json
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, PlainTextResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Game, LlmUsage, Run
from app.scraper.selection import BATCH_SIZE, today_in
from app.web.routes import get_db, templates
from app.web.security import same_origin

STREAM_INTERVAL_SEC = 1.0
EVENTS_SHOWN = 20
RUNS_SHOWN = 20

STATUS_LABELS = {
    "idle": "Ждёт",
    "running": "Работает",
    "budget": "Пауза: бюджет",
    "error": "Ошибка",
}
STAGE_LABELS = {
    "idle": "—",
    "pick_batch": "Выбор игр",
    "metacritic": "Данные Metacritic",
    "reviews": "Отзывы",
    "llm": "Резюме отзывов",
    "embedding": "Похожие игры",
    "letsplay": "Летсплей",
}
RUN_STATUS_LABELS = {
    "running": "идёт",
    "ok": "успешно",
    "partial": "с ошибками",
    "failed": "упал",
    "paused_budget": "пауза: бюджет",
}
TRIGGER_LABELS = {"schedule": "по расписанию", "manual": "вручную"}

router = APIRouter()


def _local(value: datetime | None, tz: ZoneInfo) -> datetime | None:
    """Наивное UTC из базы и state -> время в часовом поясе сервиса."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return value.astimezone(tz)


def _day_start_utc(tz: ZoneInfo) -> datetime:
    """Начало «сегодня» (по TZ сервиса) в наивном UTC — для сравнения с Run.started_at."""
    start = datetime.combine(today_in(tz.key), time.min, tzinfo=tz)
    return start.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} с"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} мин {sec:02d} с"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч {minutes:02d} мин"


def collect(app: FastAPI) -> dict[str, Any]:
    """Снимок для страницы и SSE: только простые типы, готовые к JSON."""
    settings = app.state.settings
    worker = app.state.worker
    tz = ZoneInfo(settings.tz)
    snap = worker.deps.state.snapshot()
    today = today_in(settings.tz)

    with app.state.sessionmaker() as db:
        total_games = db.scalar(select(func.count(Game.id))) or 0
        processed_today = (
            db.scalar(select(func.count(Game.id)).where(Game.last_processed_date == today)) or 0
        )
        today_runs = db.scalars(select(Run.errors).where(Run.started_at >= _day_start_utc(tz)))
        errors_today = sum(len(e or []) for e in today_runs)
        usage = db.get(LlmUsage, today)
        last = db.execute(
            select(Run.id, Run.status, Run.games_processed).order_by(Run.id.desc()).limit(1)
        ).first()

    spent = usage.cost_usd if usage else 0.0
    budget = settings.llm_daily_budget_usd
    if snap["running"]:
        status = "running"
    elif spent >= budget or (last and last.status == "paused_budget"):
        status = "budget"
    elif last and last.status == "failed":
        status = "error"
    else:
        status = "idle"

    next_run = worker.next_run_at()
    now = datetime.now(tz)
    seconds_left = int((next_run - now).total_seconds()) if next_run else None
    started = _local(snap["started_at"], tz)

    return {
        "status": status,
        "status_label": STATUS_LABELS[status],
        "run_id": snap["run_id"] if snap["running"] else None,
        "trigger": TRIGGER_LABELS.get(snap["trigger"] or "", "") if snap["running"] else "",
        "stage": STAGE_LABELS.get(snap["stage"], snap["stage"]),
        "current_game": (snap["current_game"] or "—") if snap["running"] else "—",
        "processed": snap["games_processed"] if snap["running"] else 0,
        "found": snap["games_found"] if snap["running"] else 0,
        "batch_size": BATCH_SIZE,
        "run_errors": snap["errors"] if snap["running"] else 0,
        "elapsed": _duration((now - started).total_seconds()) if snap["running"] else "—",
        "next_run_at": next_run.astimezone(tz).strftime("%H:%M") if next_run else None,
        "next_run_in": _duration(seconds_left) if seconds_left is not None else "не запланирован",
        "total_games": total_games,
        "processed_today": processed_today,
        "errors_today": errors_today,
        "llm_spent": round(spent, 4),
        "llm_budget": budget,
        "llm_percent": min(100, round(spent * 100 / budget)) if budget else 0,
        "llm_input_tokens": usage.input_tokens if usage else 0,
        "llm_output_tokens": usage.output_tokens if usage else 0,
        "llm_enabled": snap["llm_enabled"],
        "events": [
            {
                "time": _local(e["ts"], tz).strftime("%H:%M:%S"),
                "level": e["level"],
                "message": e["message"],
            }
            for e in reversed(snap["events"][-EVENTS_SHOWN:])
        ],
        # Меняется, когда появился новый прогон, сменился статус последнего или он обработал
        # ещё одну игру: страница по нему перезагружает таблицу прогонов.
        "runs_key": f"{last.id}:{last.status}:{last.games_processed}" if last else "",
    }


def recent_runs(db: Session, tz: ZoneInfo) -> list[dict[str, Any]]:
    rows = db.scalars(select(Run).order_by(Run.id.desc()).limit(RUNS_SHOWN))
    runs = []
    for r in rows:
        started = _local(r.started_at, tz)
        finished = _local(r.finished_at, tz)
        runs.append(
            {
                "id": r.id,
                "trigger": TRIGGER_LABELS.get(r.trigger, r.trigger),
                "status": r.status,
                "status_label": RUN_STATUS_LABELS.get(r.status, r.status),
                "started": started.strftime("%d.%m %H:%M:%S") if started else "—",
                "duration": _duration((finished - started).total_seconds())
                if started and finished
                else "идёт",
                "games_found": r.games_found,
                "games_processed": r.games_processed,
                "errors": r.errors or [],
                "source": r.source,
                "page": r.page,
            }
        )
    return runs


def sse_message(data: dict[str, Any]) -> str:
    return f"event: state\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("/monitor", response_class=HTMLResponse)
def monitor_page(request: Request, db: Session = Depends(get_db)):  # noqa: B008
    tz = ZoneInfo(request.app.state.settings.tz)
    context = {
        "s": collect(request.app),
        "runs": recent_runs(db, tz),
        "cooldown_min": request.app.state.settings.force_run_cooldown_min,
    }
    return templates.TemplateResponse(request, "monitor.html", context)


@router.get("/monitor/runs", response_class=HTMLResponse)
def monitor_runs(request: Request, db: Session = Depends(get_db)):  # noqa: B008
    tz = ZoneInfo(request.app.state.settings.tz)
    return templates.TemplateResponse(request, "_runs.html", {"runs": recent_runs(db, tz)})


@router.get("/monitor/stream")
async def monitor_stream(request: Request) -> StreamingResponse:
    app = request.app
    # Только для тестов: TestClient читает ответ целиком, бесконечный поток его повесит.
    limit: int | None = getattr(app.state, "sse_limit", None)

    async def events():
        yield "retry: 3000\n\n"
        sent = 0
        while not await request.is_disconnected():
            data = await run_in_threadpool(collect, app)
            yield sse_message(data)
            sent += 1
            if limit is not None and sent >= limit:
                return
            await asyncio.sleep(STREAM_INTERVAL_SEC)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/monitor/run", response_class=HTMLResponse)
def monitor_run(request: Request):
    # Защита от CSRF: Basic Auth браузер подставит и в запрос с чужого сайта,
    # поэтому POST принимаем только со своей страницы.
    if not same_origin(request.headers.get("origin"), request.headers.get("host")):
        return PlainTextResponse("Запрос с чужого сайта отклонён.", status_code=403)
    result = request.app.state.worker.trigger_manual()
    return templates.TemplateResponse(
        request, "_run_result.html", {"started": result.started, "reason": result.reason}
    )
