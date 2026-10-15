import json
import threading
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import Game, Run
from app.web.security import same_origin
from app.worker import pipeline
from app.worker.state import RunState, utcnow

ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        _env_file=None, database_url=f"sqlite:///{tmp_path}/mon.db", scheduler_enabled=False
    )
    with TestClient(create_app(settings)) as c:
        c.app.state.worker.deps.state = RunState()
        yield c


@pytest.fixture
def fake_run(monkeypatch):
    """Подменяет прогон: ничего не делает, только отпускает RUN_LOCK, как настоящий."""
    calls = []
    done = threading.Event()

    def run(trigger, deps, lock_held=False):
        calls.append(trigger)
        if lock_held:
            pipeline.RUN_LOCK.release()
        done.set()

    monkeypatch.setattr(pipeline, "run", run)
    return calls, done


def add_run(client, **kw):
    with client.app.state.sessionmaker() as db:
        db.add(Run(**{"trigger": "schedule", "status": "ok", "started_at": utcnow(), **kw}))
        db.commit()


def test_page_renders_with_nav_link(client):
    add_run(
        client,
        status="partial",
        games_found=20,
        games_processed=19,
        finished_at=utcnow(),
        errors=[{"slug": "bad-game", "stage": "llm", "error": "<b>boom</b>"}],
    )
    resp = client.get("/monitor")
    assert resp.status_code == 200
    assert '<a href="/monitor" aria-current="page">Мониторинг</a>' in resp.text
    assert "Запустить сейчас" in resp.text
    assert 'hx-post="/monitor/run"' in resp.text
    assert "<details" in resp.text and "bad-game" in resp.text
    assert "&lt;b&gt;boom&lt;/b&gt;" in resp.text  # текст ошибки экранирован
    assert "connect-src 'self'" in resp.headers["content-security-policy"]


def test_main_page_has_monitor_link(client):
    assert 'href="/monitor"' in client.get("/").text


def test_sse_sends_state_event(client):
    client.app.state.sse_limit = 1
    st = client.app.state.worker.deps.state
    st.start(7, "manual")
    st.set_stage("llm", "Alpha <Strike>")
    st.update(games_found=20, games_processed=3)
    st.event("Тестовое событие", "warning")
    with client.app.state.sessionmaker() as db:
        db.add(Game(slug="a", title="A"))
        db.commit()

    resp = client.get("/monitor/stream")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["cache-control"] == "no-cache"

    blocks = [b for b in resp.text.split("\n\n") if b.startswith("event:")]
    assert len(blocks) == 1
    lines = blocks[0].split("\n")
    assert lines[0] == "event: state"
    assert lines[1].startswith("data: ")
    data = json.loads(lines[1][len("data: ") :])
    assert data["status"] == "running"
    assert data["status_label"] == "Работает"
    assert data["stage"] == "Резюме отзывов"
    assert data["current_game"] == "Alpha <Strike>"
    assert (data["processed"], data["batch_size"]) == (3, 20)
    assert data["total_games"] == 1
    assert data["llm_budget"] == 3.0
    assert data["events"][0]["message"] == "Тестовое событие"
    assert data["events"][0]["level"] == "warning"


def test_status_budget_and_error(client):
    from app.web.monitor import collect

    assert collect(client.app)["status"] == "idle"
    add_run(client, status="failed", finished_at=utcnow())
    assert collect(client.app)["status"] == "error"
    add_run(client, status="paused_budget", finished_at=utcnow())
    assert collect(client.app)["status"] == "budget"


def test_errors_today_counts_only_today(client):
    from app.web.monitor import collect

    err = {"slug": "x", "stage": "llm", "error": "e"}
    add_run(client, errors=[err, err])
    add_run(client, errors=[err], started_at=utcnow() - timedelta(days=2))
    assert collect(client.app)["errors_today"] == 2


def test_post_starts_run(client, fake_run):
    calls, done = fake_run
    resp = client.post("/monitor/run", headers=ORIGIN)
    assert resp.status_code == 200
    assert "Запущено" in resp.text
    assert done.wait(2)
    assert calls == ["manual"]
    assert not pipeline.RUN_LOCK.locked()


def test_post_while_running_is_refused(client, fake_run):
    calls, _ = fake_run
    assert pipeline.RUN_LOCK.acquire(blocking=False)
    try:
        resp = client.post("/monitor/run", headers=ORIGIN)
    finally:
        pipeline.RUN_LOCK.release()
    assert resp.status_code == 200
    assert "уже идёт" in resp.text
    assert "notice--warn" in resp.text
    assert calls == []


def test_post_running_row_in_db_is_refused(client, fake_run):
    calls, _ = fake_run
    add_run(client, status="running")
    assert "уже идёт" in client.post("/monitor/run", headers=ORIGIN).text
    assert calls == []


def test_repeat_post_before_cooldown_is_refused(client, fake_run):
    calls, _ = fake_run
    add_run(client, trigger="manual", started_at=utcnow() - timedelta(minutes=3))
    resp = client.post("/monitor/run", headers=ORIGIN)
    assert resp.status_code == 200
    assert "Попробуйте через 7 мин" in resp.text
    assert calls == []


def test_post_after_cooldown_is_allowed(client, fake_run):
    calls, done = fake_run
    add_run(client, trigger="manual", started_at=utcnow() - timedelta(minutes=11))
    assert "Запущено" in client.post("/monitor/run", headers=ORIGIN).text
    assert done.wait(2)
    assert calls == ["manual"]


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.example"},
        {"Origin": "http://testserver.evil.example"},
        {"Origin": "null"},
        {},
    ],
)
def test_post_with_foreign_or_missing_origin_is_403(client, fake_run, headers):
    calls, _ = fake_run
    resp = client.post("/monitor/run", headers=headers)
    assert resp.status_code == 403
    assert calls == []


def test_runs_partial(client):
    add_run(client, games_found=20, games_processed=20, finished_at=utcnow())
    resp = client.get("/monitor/runs")
    assert resp.status_code == 200
    assert "<table" in resp.text and "успешно" in resp.text
    assert "<html" not in resp.text


@pytest.mark.parametrize(
    ("origin", "host", "ok"),
    [
        ("https://1-2-3-4.sslip.io", "1-2-3-4.sslip.io", True),  # за Caddy: https снаружи
        ("http://localhost:8000", "localhost:8000", True),
        ("http://localhost:8001", "localhost:8000", False),
        ("https://evil.example", "1-2-3-4.sslip.io", False),
        (None, "localhost", False),
        ("http://localhost", None, False),
    ],
)
def test_same_origin(origin, host, ok):
    assert same_origin(origin, host) is ok
