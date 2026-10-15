import json
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.db import init_db, make_engine, make_sessionmaker
from app.llm.client import LlmClient


class FakeMessages:
    """Подменяет sdk.messages: отдаёт заготовленные ответы и запоминает запросы."""

    def __init__(self):
        self.calls: list[dict] = []
        self.replies: list = []

    def reply(self, data, *, stop_reason="end_turn", input_tokens=1000, output_tokens=200):
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        self.replies.append(
            SimpleNamespace(
                content=[
                    SimpleNamespace(type="thinking", thinking=""),
                    SimpleNamespace(type="text", text=text),
                ],
                stop_reason=stop_reason,
                usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
            )
        )

    def fail(self, exc: Exception):
        self.replies.append(exc)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def settings():
    return Settings(_env_file=None, llm_daily_budget_usd=3.0)


@pytest.fixture
def factory():
    engine = make_engine("sqlite://")
    init_db(engine)
    yield make_sessionmaker(engine)
    engine.dispose()


@pytest.fixture
def fake():
    return FakeMessages()


@pytest.fixture
def llm(settings, factory, fake):
    return LlmClient(settings, factory, sdk=SimpleNamespace(messages=fake))
