"""Ручной запуск с кулдауном и авто-режим (APScheduler) с ограничением по времени.

По умолчанию прогоны идут только по кнопке. Авто-режим включается кнопкой на /monitor:
первый прогон сразу, дальше раз в SCHEDULE_MINUTES, через AUTO_MODE_HOURS режим выключается.
Срок хранится в базе (таблица auto_mode), поэтому переживает перезапуск.
"""

import contextlib
import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import func, select

from app.models import AutoMode, Run
from app.worker import pipeline
from app.worker.pipeline import Deps
from app.worker.state import utcnow

log = logging.getLogger(__name__)

JOB_ID = "pipeline"
AUTO_OFF_JOB_ID = "auto_off"
AUTO_ROW_ID = 1


@dataclass
class ManualResult:
    started: bool
    reason: str


def _aware(value: datetime) -> datetime:
    """Наивное UTC (как в базе) -> UTC с часовым поясом (для APScheduler)."""
    return value.replace(tzinfo=UTC)


class Worker:
    def __init__(self, deps: Deps):
        self.deps = deps
        self.scheduler: BackgroundScheduler | None = None
        # Проверка «можно ли запустить» и захват RUN_LOCK должны идти без разрыва.
        self._manual_lock = threading.Lock()
        # Включение, выключение и выключение по сроку не должны пересекаться.
        self._auto_lock = threading.Lock()
        self._auto_until: datetime | None = None

    def start(self, now: datetime | None = None) -> None:
        """Помечает зависшие прогоны, запускает планировщик и восстанавливает авто-режим,
        если его срок ещё не прошёл. Сам по себе прогон не запускает."""
        now = now or utcnow()
        pipeline.mark_stale_runs(self.deps.factory)
        self.scheduler = BackgroundScheduler(timezone=self.deps.settings.tz)
        self.scheduler.start()
        with self._auto_lock:
            with self.deps.factory() as db:
                row = db.get(AutoMode, AUTO_ROW_ID)
                until = row.enabled_until if row else None
                enabled_at = row.enabled_at if row else None
                if row and until is not None and until <= now:
                    row.enabled_until = None
                    db.commit()
            if until is not None and until > now:
                interval = timedelta(minutes=self.deps.settings.schedule_minutes)
                # Следующий запуск — по сетке от момента включения, строго в будущем.
                start = enabled_at or now
                first = start + interval * ((now - start) // interval + 1)
                self._schedule(first, until)
                self._auto_until = until
                self.deps.state.event(
                    f"Авто-режим восстановлен после перезапуска: {self._until_text()}"
                )
                return
        self.deps.state.event("Планировщик запущен: авто-режим выключен, прогоны только вручную")

    def shutdown(self) -> None:
        if self.scheduler is not None:
            self.scheduler.shutdown(wait=False)
            self.scheduler = None

    def next_run_at(self) -> datetime | None:
        job = self.scheduler.get_job(JOB_ID) if self.scheduler else None
        return job.next_run_time if job else None

    def auto_until(self, now: datetime | None = None) -> datetime | None:
        """Срок авто-режима (наивное UTC) или None, если режим выключен."""
        until = self._auto_until
        if until is None or until <= (now or utcnow()):
            return None
        return until

    def _until_text(self) -> str:
        tz = ZoneInfo(self.deps.settings.tz)
        local = _aware(self._auto_until).astimezone(tz) if self._auto_until else None
        return f"до {local:%H:%M}" if local else "выключен"

    def _schedule(self, first: datetime, until: datetime) -> None:
        """Интервальная задача (последний запуск строго раньше срока) и таймер выключения."""
        s = self.deps.settings
        end = _aware(until) - timedelta(seconds=1)
        if first <= until - timedelta(seconds=1):
            self.scheduler.add_job(
                self.run_scheduled,
                IntervalTrigger(minutes=s.schedule_minutes, end_date=end, timezone=UTC),
                next_run_time=_aware(first),
                id=JOB_ID,
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=300,
            )
        self.scheduler.add_job(
            self.expire,
            "date",
            run_date=_aware(until),
            id=AUTO_OFF_JOB_ID,
            replace_existing=True,
            misfire_grace_time=None,
        )

    def _save_auto(self, enabled_at: datetime | None, until: datetime | None) -> None:
        with self.deps.factory() as db:
            row = db.get(AutoMode, AUTO_ROW_ID) or AutoMode(id=AUTO_ROW_ID)
            if enabled_at is not None:
                row.enabled_at = enabled_at
            row.enabled_until = until
            db.add(row)
            db.commit()

    def _turn_off(self) -> None:
        # Сначала флаг в памяти: мониторинг сразу видит «выключен», даже если запись
        # в базу ждёт, пока идущий прогон отпустит SQLite.
        self._auto_until = None
        for job_id in (JOB_ID, AUTO_OFF_JOB_ID):
            with contextlib.suppress(JobLookupError):
                self.scheduler.remove_job(job_id)
        self._save_auto(None, None)

    def enable_auto(self, now: datetime | None = None) -> ManualResult:
        """Включает авто-режим: первый прогон сразу, дальше по расписанию до срока.
        Повторное включение не продлевает срок и не создаёт вторую задачу."""
        now = now or utcnow()
        with self._auto_lock:
            if self.scheduler is None:
                return ManualResult(False, "Планировщик не запущен.")
            if self.auto_until(now) is not None:
                return ManualResult(False, f"Авто-режим уже включён {self._until_text()}.")
            until = now + timedelta(hours=self.deps.settings.auto_mode_hours)
            self._save_auto(now, until)
            self._auto_until = until
            self._schedule(now, until)
            text = self._until_text()
        self.deps.state.event(f"Авто-режим включён {text}")
        return ManualResult(True, f"Авто-режим включён {text}. Первый прогон начинается.")

    def disable_auto(self) -> ManualResult:
        """Выключение по кнопке. Идущий прогон доработает, новые не начнутся."""
        with self._auto_lock:
            if self._auto_until is None or self.scheduler is None:
                return ManualResult(False, "Авто-режим уже выключен.")
            self._turn_off()
        self.deps.state.event("Авто-режим выключен вручную")
        return ManualResult(True, "Авто-режим выключен. Идущий прогон доработает до конца.")

    def expire(self, now: datetime | None = None) -> bool:
        """Таймер срока: выключает режим, если срок наступил. True — режим выключен сейчас."""
        now = now or utcnow()
        with self._auto_lock:
            until = self._auto_until
            # Секунда запаса: таймер может сработать на миллисекунды раньше срока.
            if until is None or self.scheduler is None or now + timedelta(seconds=1) < until:
                return False
            self._turn_off()
        self.deps.state.event("Авто-режим выключен по сроку")
        return True

    def run_scheduled(self) -> int | None:
        if self.auto_until() is None:
            return None
        # Если идёт ручной прогон, pipeline.run пропустит этот запуск и запишет событие.
        return pipeline.run("schedule", self.deps)

    def trigger_manual(self, now: datetime | None = None) -> ManualResult:
        """Запуск по кнопке в отдельном потоке. Отказ — с понятной причиной."""
        now = now or utcnow()
        cooldown = timedelta(minutes=self.deps.settings.force_run_cooldown_min)
        with self._manual_lock:
            if pipeline.RUN_LOCK.locked():
                return ManualResult(False, "Прогон уже идёт. Дождитесь его окончания.")
            with self.deps.factory() as db:
                if db.scalar(select(Run.id).where(Run.status == "running").limit(1)):
                    return ManualResult(False, "Прогон уже идёт. Дождитесь его окончания.")
                last = db.scalar(select(func.max(Run.started_at)).where(Run.trigger == "manual"))
            if last is not None and now - last < cooldown:
                wait = cooldown - (now - last)
                minutes = max(1, -(-int(wait.total_seconds()) // 60))
                return ManualResult(
                    False,
                    f"Ручной запуск можно делать не чаще раза в "
                    f"{self.deps.settings.force_run_cooldown_min} мин. "
                    f"Попробуйте через {minutes} мин.",
                )
            if not pipeline.RUN_LOCK.acquire(blocking=False):
                return ManualResult(False, "Прогон уже идёт. Дождитесь его окончания.")

        thread = threading.Thread(
            target=pipeline.run,
            args=("manual", self.deps),
            kwargs={"lock_held": True},
            name="manual-run",
            daemon=True,
        )
        thread.start()
        return ManualResult(True, "Прогон запущен.")
