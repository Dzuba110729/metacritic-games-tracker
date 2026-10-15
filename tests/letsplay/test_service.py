import threading
from datetime import datetime, timedelta

import pytest

from app.letsplay.service import LetsplayHook, needs_check
from app.letsplay.summarize import summarize_letsplay
from app.letsplay.youtube import Blocked, LetsplayError, SearchResult
from app.llm.client import BudgetExceeded, LlmBadResponse
from app.models import Game
from tests.letsplay.conftest import make_video

NOW = datetime(2026, 10, 15, 12, 0)
TRANSCRIPT = " ".join(["word"] * 300)
SUMMARY = {
    "impression": "Блогеру понравилось.",
    "pros": ["Бои", "Музыка"],
    "cons": ["Карта"],
    "recommend": "да",
}


def hook(**kw):
    defaults = {
        "finder": lambda title: SearchResult(make_video()),
        "transcriber": lambda video: TRANSCRIPT,
        "summarizer": lambda llm, title, text: SUMMARY,
        "now": lambda: NOW,
    }
    defaults.update(kw)
    return LetsplayHook(**defaults)


def game(**kw):
    return Game(slug="silksong", title="Silksong", **kw)


LLM = object()  # хук только передаёт клиент в summarizer


def test_ok_record():
    g = game()
    assert hook()(None, g, LLM) == "ok"
    lp = g.letsplay
    assert lp["status"] == "ok"
    assert lp["video_id"] == "abcdefghijk" and lp["channel"] == "Some Channel"
    assert lp["views"] == 12345 and lp["summary"] == SUMMARY
    assert lp["checked_at"] == "2026-10-15T12:00:00"


def raise_(exc):
    def f(*args):
        raise exc

    return f


@pytest.mark.parametrize(
    ("kw", "status"),
    [
        ({"finder": lambda t: SearchResult(None, "not_found")}, "not_found"),
        ({"finder": lambda t: SearchResult(None, "no_subtitles")}, "no_subtitles"),
        ({"finder": raise_(Blocked("HTTP Error 429"))}, "blocked"),
        ({"finder": raise_(LetsplayError("boom"))}, "error"),
        ({"transcriber": raise_(Blocked("Sign in to confirm"))}, "blocked"),
        ({"summarizer": raise_(LlmBadResponse("bad"))}, "error"),
        # Мало речи в субтитрах — настоящий summarizer отказывается до вызова LLM.
        ({"transcriber": lambda v: "hi", "summarizer": summarize_letsplay}, "no_subtitles"),
    ],
)
def test_statuses(kw, status):
    g = game()
    assert hook(**kw)(None, g, LLM) == status
    assert g.letsplay["status"] == status
    assert g.letsplay["summary"] is None


def test_without_llm_keeps_video_and_retries_later():
    g = game()
    assert hook()(None, g, None) == "error"
    assert g.letsplay["video_id"] == "abcdefghijk"


def test_budget_exceeded_is_recorded_and_reraised():
    g = game()
    with pytest.raises(BudgetExceeded):
        hook(summarizer=raise_(BudgetExceeded("limit")))(None, g, LLM)
    assert g.letsplay["status"] == "error"


def test_unexpected_exception_is_recorded_and_reraised():
    g = game()
    with pytest.raises(KeyError):
        hook(finder=raise_(KeyError("x")))(None, g, LLM)
    assert g.letsplay["status"] == "error"


def test_timeout():
    release = threading.Event()

    def slow(title):
        release.wait(5)
        return SearchResult(None, "not_found")

    g = game()
    try:
        assert hook(finder=slow, timeout=0.05)(None, g, LLM) == "error"
    finally:
        release.set()
    assert "таймаут" in g.letsplay["error"]


def test_ok_is_not_rechecked_and_others_wait_a_day():
    calls = []

    def finder(title):
        calls.append(title)
        return SearchResult(None, "not_found")

    ok = game(letsplay={"status": "ok", "checked_at": "2020-01-01T00:00:00"})
    assert hook(finder=finder)(None, ok, LLM) is None

    recent = (NOW - timedelta(hours=23)).isoformat()
    g = game(letsplay={"status": "not_found", "checked_at": recent})
    assert hook(finder=finder)(None, g, LLM) is None
    assert calls == []

    g.letsplay = {"status": "blocked", "checked_at": (NOW - timedelta(hours=25)).isoformat()}
    assert hook(finder=finder)(None, g, LLM) == "not_found"
    assert calls == ["Silksong"]


def test_needs_check_bad_date():
    assert needs_check(None, NOW)
    assert needs_check({"status": "error", "checked_at": "garbage"}, NOW)
