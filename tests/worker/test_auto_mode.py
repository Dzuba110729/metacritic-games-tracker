import threading
from datetime import UTC, timedelta

import pytest

from app.models import AutoMode
from app.worker import pipeline
from app.worker.scheduler import AUTO_OFF_JOB_ID, JOB_ID, Worker
from app.worker.state import utcnow


@pytest.fixture
def fake_run(monkeypatch):
    """Подменяет прогон: только запоминает вызов и отпускает RUN_LOCK, как настоящий."""
    calls = []
    done = threading.Event()

    def run(trigger, deps, lock_held=False):
        calls.append(trigger)
        if lock_held:
            pipeline.RUN_LOCK.release()
        done.set()

    monkeypatch.setattr(pipeline, "run", run)
    return calls, done


@pytest.fixture
def worker(deps, fake_run):
    w = Worker(deps)
    w.start()
    yield w
    w.shutdown()


def _row(factory):
    with factory() as db:
        return db.get(AutoMode, 1)


def _messages(deps):
    return [e["message"] for e in deps.state.snapshot()["events"]]


def test_no_job_by_default(worker, fake_run, deps):
    calls, done = fake_run
    assert worker.scheduler.get_jobs() == []
    assert worker.next_run_at() is None
    assert worker.auto_until() is None
    assert not done.wait(0.3)
    assert calls == []
    assert any("прогоны только вручную" in m for m in _messages(deps))


def test_enable_creates_job_deadline_and_runs_now(worker, fake_run, deps, factory):
    calls, done = fake_run
    before = utcnow()
    result = worker.enable_auto()
    assert result.started

    assert done.wait(3)
    assert calls == ["schedule"]

    until = worker.auto_until()
    hours = timedelta(hours=deps.settings.auto_mode_hours)
    assert before + hours <= until <= utcnow() + hours
    row = _row(factory)
    assert row.enabled_until == until
    assert row.enabled_at is not None

    job = worker.scheduler.get_job(JOB_ID)
    assert job.trigger.interval == timedelta(minutes=deps.settings.schedule_minutes)
    assert job.max_instances == 1 and job.coalesce is True
    # Последний запуск — строго до срока: за 3 часа не больше 3 прогонов.
    assert job.trigger.end_date.replace(tzinfo=None) < until
    off = worker.scheduler.get_job(AUTO_OFF_JOB_ID)
    assert off.next_run_time.replace(tzinfo=None) == until
    assert any(m.startswith("Авто-режим включён") for m in _messages(deps))


def test_at_most_three_runs_in_window(worker, deps):
    worker.enable_auto()
    job = worker.scheduler.get_job(JOB_ID)
    first = job.next_run_time
    times, prev = [], None
    nxt = first
    while nxt is not None and len(times) < 10:
        times.append(nxt)
        prev = nxt
        nxt = job.trigger.get_next_fire_time(prev, prev)
    assert len(times) == 3


def test_repeat_enable_does_not_extend_or_duplicate(worker, fake_run, deps):
    calls, done = fake_run
    worker.enable_auto()
    assert done.wait(3)
    until = worker.auto_until()

    again = worker.enable_auto(now=utcnow() + timedelta(minutes=30))
    assert not again.started
    assert "уже включён" in again.reason
    assert worker.auto_until() == until
    assert [j.id for j in worker.scheduler.get_jobs()].count(JOB_ID) == 1
    assert calls == ["schedule"]


def test_enable_while_manual_run_skips_first(worker, deps, monkeypatch):
    # Настоящий pipeline.run: при занятом RUN_LOCK он пропускает прогон и пишет событие.
    monkeypatch.undo()
    skipped = threading.Event()
    real_event = deps.state.event

    def event(message, level="info"):
        real_event(message, level)
        if "пропущен" in message:
            skipped.set()

    deps.state.event = event
    assert pipeline.RUN_LOCK.acquire(blocking=False)
    try:
        assert worker.enable_auto().started
        assert skipped.wait(3)
    finally:
        pipeline.RUN_LOCK.release()
    assert worker.auto_until() is not None
    assert worker.scheduler.get_job(JOB_ID) is not None


def test_disable_removes_jobs(worker, fake_run, deps, factory):
    worker.enable_auto()
    result = worker.disable_auto()
    assert result.started
    assert worker.scheduler.get_job(JOB_ID) is None
    assert worker.scheduler.get_job(AUTO_OFF_JOB_ID) is None
    assert worker.auto_until() is None
    assert _row(factory).enabled_until is None
    assert "Авто-режим выключен вручную" in _messages(deps)
    assert not worker.disable_auto().started


def test_expires_by_deadline(worker, deps, factory):
    worker.enable_auto()
    until = worker.auto_until()

    assert not worker.expire(now=until - timedelta(minutes=5))
    assert worker.auto_until() is not None

    assert worker.expire(now=until)
    assert worker.auto_until() is None
    assert worker.scheduler.get_jobs() == []
    assert _row(factory).enabled_until is None
    assert "Авто-режим выключен по сроку" in _messages(deps)
    # После срока режим можно включить снова.
    assert worker.enable_auto().started


def test_auto_until_respects_time(worker):
    worker.enable_auto()
    until = worker.auto_until()
    assert worker.auto_until(now=until + timedelta(seconds=1)) is None


def test_restore_after_restart_keeps_deadline(deps, factory, fake_run):
    calls, done = fake_run
    now = utcnow()
    enabled_at = now - timedelta(minutes=70)
    until = enabled_at + timedelta(hours=3)
    with factory() as db:
        db.add(AutoMode(id=1, enabled_at=enabled_at, enabled_until=until))
        db.commit()

    w = Worker(deps)
    w.start(now=now)
    try:
        assert w.auto_until() == until
        # Без немедленного прогона: следующий по сетке через 120 мин от включения.
        expected = (enabled_at + timedelta(minutes=120)).replace(tzinfo=UTC)
        assert abs(w.next_run_at().timestamp() - expected.timestamp()) < 1
        off = w.scheduler.get_job(AUTO_OFF_JOB_ID)
        assert off.next_run_time.replace(tzinfo=None) == until
        assert not done.wait(0.3)
        assert calls == []
        assert any("восстановлен" in m for m in _messages(deps))
    finally:
        w.shutdown()


def test_restore_after_expired_deadline_is_off(deps, factory, fake_run):
    now = utcnow()
    with factory() as db:
        db.add(
            AutoMode(
                id=1, enabled_at=now - timedelta(hours=4), enabled_until=now - timedelta(hours=1)
            )
        )
        db.commit()
    w = Worker(deps)
    w.start(now=now)
    try:
        assert w.auto_until() is None
        assert w.scheduler.get_jobs() == []
        assert _row(factory).enabled_until is None
    finally:
        w.shutdown()
