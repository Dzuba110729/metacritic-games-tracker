from datetime import date

import anthropic
import httpx2
import pytest

from app.llm.client import (
    BudgetExceeded,
    LlmApiError,
    LlmBadResponse,
    LlmConnectionError,
    LlmRateLimited,
    spent_today,
)
from app.models import LlmUsage
from app.scraper.selection import today_in

SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}
REQ = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def test_request_uses_structured_output_and_model(llm, fake):
    fake.reply({"ok": 1})
    assert llm.complete_json("sys", "user", SCHEMA) == {"ok": 1}
    call = fake.calls[0]
    assert call["model"] == "claude-haiku-5-5"
    assert call["output_config"]["format"] == {"type": "json_schema", "schema": SCHEMA}
    assert "temperature" not in call  # Haiku 5.5 отклоняет sampling-параметры


def test_usage_recorded(llm, fake, factory, settings):
    fake.reply({}, input_tokens=1_000_000, output_tokens=1_000_000)
    llm.complete_json("s", "u", SCHEMA)
    assert llm.cost_usd == pytest.approx(0.60)
    with factory() as db:
        row = db.get(LlmUsage, today_in(settings.tz))
        assert (row.input_tokens, row.output_tokens) == (1_000_000, 1_000_000)
        assert row.cost_usd == pytest.approx(0.60)


def test_budget_exceeded_blocks_call(llm, fake, factory, settings):
    with factory() as db:
        db.add(LlmUsage(date=today_in(settings.tz), input_tokens=0, output_tokens=0, cost_usd=3.0))
        db.commit()
    with pytest.raises(BudgetExceeded):
        llm.complete_json("s", "u", SCHEMA)
    assert fake.calls == []


def test_yesterday_spend_does_not_count(llm, fake, factory):
    with factory() as db:
        db.add(LlmUsage(date=date(2000, 1, 1), input_tokens=0, output_tokens=0, cost_usd=99))
        db.commit()
        assert spent_today(db, date(2000, 1, 2)) == 0
    fake.reply({})
    llm.complete_json("s", "u", SCHEMA)


def test_refusal_and_truncation_are_bad_response(llm, fake):
    fake.reply("", stop_reason="refusal")
    with pytest.raises(LlmBadResponse):
        llm.complete_json("s", "u", SCHEMA)
    fake.reply('{"a": ', stop_reason="max_tokens")
    with pytest.raises(LlmBadResponse):
        llm.complete_json("s", "u", SCHEMA)
    assert llm.input_tokens == 2000  # оплаченные токены всё равно учтены


def test_rate_limit(llm, fake):
    resp = httpx2.Response(429, headers={"retry-after": "7"}, request=REQ)
    fake.fail(anthropic.RateLimitError("slow down", response=resp, body=None))
    with pytest.raises(LlmRateLimited) as e:
        llm.complete_json("s", "u", SCHEMA)
    assert e.value.retry_after == 7 and e.value.retryable


@pytest.mark.parametrize("status,retryable", [(400, False), (529, True)])
def test_api_status_error(llm, fake, status, retryable):
    resp = httpx2.Response(status, request=REQ)
    fake.fail(anthropic.APIStatusError("boom", response=resp, body=None))
    with pytest.raises(LlmApiError) as e:
        llm.complete_json("s", "u", SCHEMA)
    assert e.value.status_code == status and e.value.retryable is retryable


def test_connection_error(llm, fake):
    fake.fail(anthropic.APIConnectionError(request=REQ))
    with pytest.raises(LlmConnectionError):
        llm.complete_json("s", "u", SCHEMA)
