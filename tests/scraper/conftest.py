import json
from pathlib import Path

import pytest
import respx

from app.db import init_db, make_engine, make_sessionmaker
from app.scraper.client import BACKEND, MetacriticClient

FIXTURES = Path(__file__).parent.parent / "fixtures"


def load(name: str):
    text = (FIXTURES / name).read_text()
    return json.loads(text) if name.endswith(".json") else text


@pytest.fixture
def client():
    with MetacriticClient("test-key", min_interval=0, retry_wait_max=0) as c:
        yield c


@pytest.fixture
def mock_api():
    with respx.mock(base_url=BACKEND, assert_all_called=False) as router:
        yield router


@pytest.fixture
def db():
    engine = make_engine("sqlite://")
    init_db(engine)
    with make_sessionmaker(engine)() as session:
        yield session
    engine.dispose()
