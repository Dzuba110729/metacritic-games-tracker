from datetime import timedelta

from sqlalchemy import func, select

from app.models import Game, LlmUsage, PlatformScore, Run
from app.worker import pipeline
from app.worker.pipeline import mark_stale_runs

from .conftest import TODAY


def _games(factory):
    with factory() as db:
        return list(db.scalars(select(Game)))


def _run(factory, run_id):
    with factory() as db:
        return db.get(Run, run_id)


def test_run_processes_20_games_with_summaries(deps, factory, fake_llm):
    run_id = pipeline.run("schedule", deps)

    run = _run(factory, run_id)
    assert run.status == "ok"
    assert run.source == "new_releases"
    assert run.games_found == 20
    assert run.games_processed == 20
    assert run.errors == []
    assert run.finished_at is not None

    games = _games(factory)
    assert len(games) == 20
    for g in games:
        assert g.last_processed_date == TODAY
        assert g.critic_summary["status"] == "ok"
        assert g.user_summary["status"] == "ok"
        assert g.critic_summary["likes"]
        assert g.reviews_hash
        assert g.embedding is not None
    with factory() as db:
        assert db.scalar(select(func.count(func.distinct(PlatformScore.game_id)))) == 20
    assert fake_llm.calls == 40  # критики + игроки на каждую игру

    snap = deps.state.snapshot()
    assert not snap["running"]
    assert snap["games_processed"] == 20
    assert any("завершён" in e["message"] for e in snap["events"])


def test_second_run_same_day_takes_other_games(deps, factory):
    pipeline.run("schedule", deps)
    first = {g.slug for g in _games(factory)}

    run_id = pipeline.run("schedule", deps)

    run = _run(factory, run_id)
    assert run.status == "ok"
    assert run.source == "browse"
    assert run.page == 1
    games = _games(factory)
    assert len(games) == 40
    second = {g.slug for g in games} - first
    assert len(second) == 20
    assert all(g.last_processed_date == TODAY for g in games)


def test_one_game_error_does_not_break_run(deps, factory, metacritic):
    metacritic.fail_slugs = {"enshrouded"}

    run_id = pipeline.run("schedule", deps)

    run = _run(factory, run_id)
    assert run.status == "partial"
    assert run.games_processed == 19
    assert len(run.errors) == 1
    assert run.errors[0]["slug"] == "enshrouded"
    assert run.errors[0]["stage"] == "metacritic"
    assert len(_games(factory)) == 19
    assert deps.state.snapshot()["errors"] == 1


def test_budget_exceeded_pauses_llm_but_updates_games(deps, factory, fake_llm, settings):
    with factory() as db:
        db.add(LlmUsage(date=TODAY, input_tokens=0, output_tokens=0, cost_usd=5.0))
        db.commit()

    run_id = pipeline.run("schedule", deps)

    run = _run(factory, run_id)
    assert run.status == "paused_budget"
    assert run.games_processed == 20
    assert fake_llm.calls == 0
    games = _games(factory)
    assert len(games) == 20
    assert all(g.critic_summary is None for g in games)
    assert all(g.embedding is not None and g.last_processed_date == TODAY for g in games)
    assert deps.state.snapshot()["llm_enabled"] is False


def test_budget_runs_out_mid_run(deps, factory, fake_llm, settings):
    # Каждое резюме стоит $0.0002; потолок кончится после 5 игр (10 вызовов).
    settings.llm_daily_budget_usd = 0.002

    run_id = pipeline.run("schedule", deps)

    run = _run(factory, run_id)
    assert run.status == "paused_budget"
    assert run.games_processed == 20
    assert fake_llm.calls == 10
    with_summary = [g for g in _games(factory) if g.critic_summary]
    assert len(with_summary) == 5


def test_without_api_key_games_are_saved_without_summaries(deps, factory, settings):
    deps.llm = None  # настоящий путь: ключа в настройках нет

    run_id = pipeline.run("schedule", deps)

    run = _run(factory, run_id)
    assert run.status == "ok"
    assert all(g.critic_summary is None for g in _games(factory))
    assert any("ANTHROPIC_API_KEY" in e["message"] for e in deps.state.snapshot()["events"])


def test_run_skipped_when_lock_is_taken(deps, factory):
    assert pipeline.RUN_LOCK.acquire(blocking=False)
    try:
        assert pipeline.run("schedule", deps) is None
    finally:
        pipeline.RUN_LOCK.release()
    with factory() as db:
        assert db.scalar(select(func.count(Run.id))) == 0


def test_run_skipped_when_db_has_running_run(deps, factory):
    with factory() as db:
        db.add(Run(trigger="schedule", status="running"))
        db.commit()

    assert pipeline.run("schedule", deps) is None
    assert not pipeline.RUN_LOCK.locked()


def test_mark_stale_runs(factory):
    with factory() as db:
        db.add_all([Run(trigger="schedule", status="running"), Run(trigger="manual", status="ok")])
        db.commit()

    assert mark_stale_runs(factory) == 1

    with factory() as db:
        statuses = sorted(db.scalars(select(Run.status)))
    assert statuses == ["failed", "ok"]


def test_listing_failure_marks_run_failed(deps, factory, metacritic):
    metacritic.new_releases = None  # finder ответит JSON null -> разбор упадёт

    run_id = pipeline.run("schedule", deps)

    run = _run(factory, run_id)
    assert run.status == "failed"
    assert run.errors and run.errors[0]["slug"] is None
    assert run.finished_at - run.started_at < timedelta(minutes=1)
    assert not deps.state.snapshot()["running"]


def test_letsplay_is_last_stage_and_youtube_errors_do_not_break_run(deps, factory):
    seen = []

    def letsplay(db, game, llm):
        # К этапу летсплея эмбеддинг уже посчитан, LLM прогона передана.
        assert game.embedding is not None and llm is not None
        seen.append(game.slug)
        status = "blocked" if len(seen) % 2 else "ok"
        game.letsplay = {"status": status, "error": "HTTP Error 429" if status != "ok" else None}
        return status

    deps.letsplay = letsplay
    run_id = pipeline.run("schedule", deps)

    run = _run(factory, run_id)
    assert run.status == "ok"  # blocked — статус в карточке, не ошибка прогона
    assert len(seen) == 20
    assert {g.letsplay["status"] for g in _games(factory)} == {"ok", "blocked"}
    events = [e for e in deps.state.snapshot()["events"] if "YouTube" in e["message"]]
    assert events and events[0]["level"] == "warning"


def test_letsplay_crash_is_game_error(deps, factory):
    def letsplay(db, game, llm):
        raise RuntimeError("boom")

    deps.letsplay = letsplay
    run_id = pipeline.run("schedule", deps)

    run = _run(factory, run_id)
    assert run.status == "partial"
    assert run.games_processed == 20
    assert {e["stage"] for e in run.errors} == {"letsplay"}
    assert all(g.last_processed_date == TODAY for g in _games(factory))


def test_letsplay_budget_disables_llm(deps, factory, fake_llm):
    from app.llm.client import BudgetExceeded

    calls = []

    def letsplay(db, game, llm):
        calls.append(llm)
        if llm is not None:
            raise BudgetExceeded("limit")
        return None

    deps.letsplay = letsplay
    run_id = pipeline.run("schedule", deps)

    assert _run(factory, run_id).status == "paused_budget"
    assert calls[0] is not None and calls[1:] == [None] * 19
    assert fake_llm.calls == 2  # резюме отзывов только у первой игры
