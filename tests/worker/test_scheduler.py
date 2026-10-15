from datetime import timedelta

from sqlalchemy import select

from app.models import Run
from app.worker import pipeline
from app.worker.scheduler import Worker
from app.worker.state import MAX_EVENTS, RunState, utcnow


def _wait_idle():
    # Ручной прогон идёт в своём потоке; RUN_LOCK отпускается в его конце.
    assert pipeline.RUN_LOCK.acquire(timeout=30)
    pipeline.RUN_LOCK.release()


def test_trigger_manual_starts_run_then_cooldown(deps, factory):
    worker = Worker(deps)

    result = worker.trigger_manual()
    assert result.started
    _wait_idle()

    with factory() as db:
        run = db.scalar(select(Run))
    assert run.trigger == "manual"
    assert run.status == "ok"

    again = worker.trigger_manual(now=run.started_at + timedelta(minutes=3))
    assert not again.started
    assert "через 7 мин" in again.reason

    later = worker.trigger_manual(now=run.started_at + timedelta(minutes=11))
    assert later.started
    _wait_idle()


def test_trigger_manual_refused_while_running(deps, factory):
    worker = Worker(deps)
    assert pipeline.RUN_LOCK.acquire(blocking=False)
    try:
        result = worker.trigger_manual()
    finally:
        pipeline.RUN_LOCK.release()
    assert not result.started
    assert "уже идёт" in result.reason

    with factory() as db:
        db.add(Run(trigger="schedule", status="running", started_at=utcnow()))
        db.commit()
    result = worker.trigger_manual()
    assert not result.started
    assert "уже идёт" in result.reason


def test_scheduler_job_settings(deps):
    deps.settings.first_run_delay_sec = 30
    worker = Worker(deps)
    worker.start()
    try:
        job = worker.scheduler.get_job("pipeline")
        assert job.max_instances == 1
        assert job.coalesce is True
        assert job.trigger.interval == timedelta(minutes=deps.settings.schedule_minutes)
        assert worker.next_run_at() is not None
    finally:
        worker.shutdown()


def test_state_keeps_last_events_only():
    st = RunState()
    for i in range(MAX_EVENTS + 20):
        st.event(f"событие {i}")
    events = st.snapshot()["events"]
    assert len(events) == MAX_EVENTS
    assert events[-1]["message"] == f"событие {MAX_EVENTS + 19}"
