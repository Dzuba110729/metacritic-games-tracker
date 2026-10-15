from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.db import init_db, make_engine, make_sessionmaker
from app.letsplay.youtube import Video
from app.llm.client import LlmClient
from tests.llm.conftest import FakeMessages

FIXTURES = Path(__file__).parent.parent / "fixtures"


def make_video(**kw) -> Video:
    data = {
        "video_id": "abcdefghijk",
        "url": "https://www.youtube.com/watch?v=abcdefghijk",
        "title": "Silksong lets play #1",
        "channel": "Some Channel",
        "views": 12345,
        "subtitle_url": "https://www.youtube.com/api/timedtext?v=abcdefghijk&lang=en",
        "subtitle_lang": "en",
        "auto_subtitles": True,
    }
    data.update(kw)
    return Video(**data)


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
def llm(factory, fake):
    settings = Settings(_env_file=None, llm_daily_budget_usd=3.0)
    return LlmClient(settings, factory, sdk=SimpleNamespace(messages=fake))
