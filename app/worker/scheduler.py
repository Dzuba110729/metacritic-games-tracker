"""Расписание прогонов (APScheduler) и ручной запуск с кулдауном."""

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import func, select

from app.models import Run
from app.worker import pipeline
from app.worker.pipeline import Deps
from app.worker.state import utcnow

log = logging.getLogger(__name__)

JOB_ID = "pipeline"


@dataclass
class ManualResult:
    started: bool
    reason: str


class Worker:
    def __init__(self, deps: Deps):
        self.deps = deps
        self.scheduler: BackgroundScheduler | None = None
        # Проверка «можно ли запустить» и захват RUN_LOCK должны идти без разрыва.
        self._manual_lock = threading.Lock()

    def start(self) -> None:
        """Помечает зависшие прогоны и запускает расписание: первый прогон через
        FIRST_RUN_DELAY_SEC, дальше раз в SCHEDULE_MINUTES."""
        pipeline.mark_stale_runs(self.deps.factory)
        s = self.deps.settings
        self.scheduler = BackgroundScheduler(timezone=s.tz)
        self.scheduler.add_job(
            self.run_scheduled,
            "interval",
            minutes=s.schedule_minutes,
            next_run_time=datetime.now(self.scheduler.timezone)
            + timedelta(seconds=s.first_run_delay_sec),
            id=JOB_ID,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
        )
        self.scheduler.start()
        self.deps.state.event(
            f"Планировщик запущен: первый прогон через {s.first_run_delay_sec} с, "
            f"дальше раз в {s.schedule_minutes} мин"
        )

    def shutdown(self) -> None:
        if self.scheduler is not None:
            self.scheduler.shutdown(wait=False)
            self.scheduler = None

    def next_run_at(self) -> datetime | None:
        job = self.scheduler.get_job(JOB_ID) if self.scheduler else None
        return job.next_run_time if job else None

    def run_scheduled(self) -> int | None:
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
