"""Проверки безопасности конвейера: ключ не утекает, кнопка не запускает два прогона."""

import json
import logging
import threading

import anthropic
import httpx2
from pydantic import SecretStr
from sqlalchemy import select

from app.config import Settings
from app.llm.client import LlmClient
from app.models import Run
from app.worker import pipeline
from app.worker.scheduler import Worker

from .conftest import TODAY

FAKE_KEY = "sk-ant-api03-test-DO-NOT-LEAK-0123456789"


def test_api_key_not_in_logs_events_or_errors(deps, factory, settings, caplog):
    """Настоящий SDK Anthropic, сервер отвечает 401: ключ не должен попасть
    ни в лог (даже DEBUG), ни в ленту событий, ни в Run.errors."""

    def anthropic_api(request: httpx2.Request) -> httpx2.Response:
        assert request.headers["x-api-key"] == FAKE_KEY  # ключ уходит только в заголовок
        return httpx2.Response(
            401,
            json={
                "type": "error",
                "error": {"type": "authentication_error", "message": "invalid x-api-key"},
            },
        )

    sdk = anthropic.Anthropic(
        api_key=FAKE_KEY,
        max_retries=0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(anthropic_api)),
    )

    def make_llm():
        client = LlmClient(settings, factory, sdk=sdk)
        client._today = lambda: TODAY
        return client

    deps.llm = make_llm
    settings.anthropic_api_key = SecretStr(FAKE_KEY)  # как будто ключ пришёл из .env

    with caplog.at_level(logging.DEBUG):
        run_id = pipeline.run("schedule", deps)

    with factory() as db:
        run = db.get(Run, run_id)
    assert any(e["stage"] == "llm" for e in run.errors)  # 401 дошёл до конвейера
    snapshot = json.dumps(deps.state.snapshot(), ensure_ascii=False, default=str)
    for text in (caplog.text, snapshot, json.dumps(run.errors, ensure_ascii=False)):
        assert FAKE_KEY not in text
        assert "sk-ant" not in text
    from_env = Settings(_env_file=None, anthropic_api_key=FAKE_KEY)
    assert FAKE_KEY not in repr(from_env)
    assert FAKE_KEY not in str(from_env.model_dump())


def test_simultaneous_manual_clicks_start_one_run(deps, factory):
    """Десять одновременных нажатий кнопки -> ровно один прогон."""
    worker = Worker(deps)
    barrier = threading.Barrier(10)
    results = []

    def click():
        barrier.wait()
        results.append(worker.trigger_manual())

    threads = [threading.Thread(target=click) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert pipeline.RUN_LOCK.acquire(timeout=30)
    pipeline.RUN_LOCK.release()

    assert sum(r.started for r in results) == 1
    assert all("уже идёт" in r.reason or "не чаще" in r.reason for r in results if not r.started)
    with factory() as db:
        assert len(list(db.scalars(select(Run)))) == 1


def test_scheduled_run_skipped_while_manual_runs(deps, factory):
    """Плановый прогон во время ручного пропускается, а не идёт вторым."""
    assert pipeline.RUN_LOCK.acquire(blocking=False)
    try:
        assert pipeline.run("schedule", deps) is None
    finally:
        pipeline.RUN_LOCK.release()
    assert any("пропущен" in e["message"] for e in deps.state.snapshot()["events"])
