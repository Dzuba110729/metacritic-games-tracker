import copy
import hashlib
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
import respx

from app.config import Settings
from app.db import init_db, make_engine, make_sessionmaker
from app.llm.client import LlmClient
from app.scraper.client import BACKEND, MetacriticClient
from app.worker.pipeline import Deps
from app.worker.state import RunState

FIXTURES = Path(__file__).parent.parent / "fixtures"
TODAY = date(2026, 10, 15)
EMPTY_REVIEWS = {"data": {"items": [], "totalResults": 0}, "links": {}}


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


class FakeMessages:
    """Фейковый sdk.messages: на любой запрос — валидное резюме."""

    def __init__(self):
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        text = json.dumps(
            {
                "likes": ["Стрельба", "Лут", "Кооператив"],
                "dislikes": ["Баги", "Оптимизация", "Сюжет"],
                "verdict": "Хорошая игра с недостатками.",
            },
            ensure_ascii=False,
        )
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=1000, output_tokens=200),
        )


def fake_embedder(text: str) -> np.ndarray:
    seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:4], "little")
    vec = np.random.default_rng(seed).normal(size=384).astype(np.float32)
    return vec / np.linalg.norm(vec)


class MetacriticMock:
    """Фикстуры Metacritic на любые slug: composer Borderlands 4 с подменённым названием."""

    def __init__(self, router: respx.MockRouter):
        self.fail_slugs: set[str] = set()
        self.new_releases = load("finder_new_releases.json")
        self.browse = load("finder_browse_page2.json")
        self.composer = load("composer_borderlands-4.json")
        self.stats = load("stats_user_borderlands-4_pc.json")
        self.reviews = {
            ("critic", "positive"): load("reviews_critic_positive_0.json"),
            ("critic", "negative"): load("reviews_critic_negative_0.json"),
            ("critic", "neutral"): load("reviews_critic_neutral_0.json"),
            ("user", "positive"): load("reviews_user_positive_0.json"),
            ("user", "negative"): load("reviews_user_negative_0.json"),
        }
        router.get("/finder/metacritic/web").mock(side_effect=self._finder)
        router.get(url__regex=r"/composer/metacritic/pages/games/(?P<slug>[^/]+)/web").mock(
            side_effect=self._composer
        )
        router.get(url__regex=r"/reviews/metacritic/user/games/[^/]+/platform/.+/stats/web").mock(
            return_value=httpx.Response(200, json=self.stats)
        )
        router.get(url__regex=r"/reviews/metacritic/(?P<kind>critic|user)/games/[^/]+/web").mock(
            side_effect=self._reviews
        )

    def _finder(self, request):
        params = request.url.params
        data = self.new_releases if "metaScoreMin" in params else self.browse
        return httpx.Response(200, json=data)

    def _composer(self, request, slug):
        if slug in self.fail_slugs:
            return httpx.Response(500)
        payload = copy.deepcopy(self.composer)
        for comp in payload["components"]:
            if comp["meta"]["componentName"] == "product":
                comp["data"]["item"]["title"] = f"Game {slug}"
        return httpx.Response(200, json=payload)

    def _reviews(self, request, kind):
        params = request.url.params
        if params.get("offset") != "0":
            return httpx.Response(200, json=EMPTY_REVIEWS)
        data = self.reviews.get((kind, params["filterBySentiment"]), EMPTY_REVIEWS)
        return httpx.Response(200, json=data)


@pytest.fixture
def factory(tmp_path):
    # Файл, а не память: ручной прогон идёт в другом потоке и должен видеть ту же базу.
    engine = make_engine(f"sqlite:///{tmp_path}/games.db")
    init_db(engine)
    yield make_sessionmaker(engine)
    engine.dispose()


@pytest.fixture
def settings():
    return Settings(_env_file=None, llm_daily_budget_usd=3.0, force_run_cooldown_min=10)


@pytest.fixture
def metacritic():
    with respx.mock(base_url=BACKEND, assert_all_called=False) as router:
        yield MetacriticMock(router)


@pytest.fixture
def fake_llm():
    return FakeMessages()


@pytest.fixture
def deps(settings, factory, metacritic, fake_llm):
    def make_llm():
        client = LlmClient(settings, factory, sdk=SimpleNamespace(messages=fake_llm))
        client._today = lambda: TODAY  # бюджет считаем за тот же «сегодня», что и прогон
        return client

    return Deps(
        settings=settings,
        factory=factory,
        state=RunState(),
        metacritic=lambda: MetacriticClient("test-key", min_interval=0, retry_wait_max=0),
        llm=make_llm,
        embedder=fake_embedder,
        today=lambda: TODAY,
    )
