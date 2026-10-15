from datetime import date

import httpx
import pytest
import respx

from app.scraper.api import (
    GameNotFoundError,
    _slugs,
    get_game,
    get_reviews,
    list_browse_page,
    list_new_releases,
)
from app.scraper.client import SITE, HostNotAllowedError

from .conftest import load

SLUG = "borderlands-4"
REVIEWS = f"/reviews/metacritic/{{kind}}/games/{SLUG}/web"


def test_list_new_releases(client, mock_api):
    route = mock_api.get("/finder/metacritic/web").respond(json=load("finder_new_releases.json"))
    slugs = list_new_releases(client)
    assert len(slugs) == 20
    assert "castlevania-belmonts-curse" in slugs
    params = route.calls.last.request.url.params
    assert params["sortBy"] == "-releaseDate"
    assert params["metaScoreMin"] == "1"
    assert params["mcoTypeId"] == "13"
    assert params["apiKey"] == "test-key"


def test_list_browse_page_offset(client, mock_api):
    route = mock_api.get("/finder/metacritic/web").respond(json=load("finder_browse_page2.json"))
    slugs = list_browse_page(client, 2)
    assert len(slugs) == 24
    assert slugs[0] == "hunt-me"
    params = route.calls.last.request.url.params
    assert params["offset"] == "24"
    assert params["limit"] == "24"
    assert "metaScoreMin" not in params


def _mock_game(mock_api):
    mock_api.get(f"/composer/metacritic/pages/games/{SLUG}/web").respond(
        json=load(f"composer_{SLUG}.json")
    )
    for plat in ("pc", "playstation-5", "xbox-series-x", "nintendo-switch-2"):
        mock_api.get(f"/reviews/metacritic/user/games/{SLUG}/platform/{plat}/stats/web").respond(
            json=load(f"stats_user_{SLUG}_{plat}.json")
        )


def test_get_game_parses_composer_and_platform_scores(client, mock_api):
    _mock_game(mock_api)
    game = get_game(client, SLUG)

    assert game.title == "Borderlands 4"
    assert game.developer == "Gearbox Software"
    assert game.publisher == "2K Games"
    assert game.genres == ["FPS"]
    assert game.release_date == date(2025, 9, 12)
    assert game.description.startswith("The definitive looter shooter")
    assert game.cover_url.startswith(f"{SITE}/a/img/catalog/provider/")
    assert game.video_url.startswith("https://cdn.jwplayer.com/")
    assert game.metacritic_url == f"{SITE}/game/{SLUG}/"

    by_slug = {p.slug: p for p in game.platforms}
    assert set(by_slug) == {"playstation-5", "xbox-series-x", "pc", "nintendo-switch-2"}
    assert (by_slug["pc"].metascore, by_slug["pc"].critic_count) == (81, 93)
    assert (by_slug["pc"].userscore, by_slug["pc"].user_count) == (4.7, 928)
    assert by_slug["playstation-5"].platform == "PlayStation 5"
    assert by_slug["playstation-5"].userscore == 5.5
    # Нет ни одной оценки — None, а не 0.
    ns2 = by_slug["nintendo-switch-2"]
    assert (ns2.metascore, ns2.userscore, ns2.user_count) == (None, None, None)


def test_get_game_html_fallback_fills_missing_fields(client, mock_api):
    payload = load(f"composer_{SLUG}.json")
    product = payload["components"][0]["data"]["item"]
    product["description"] = None
    product["production"]["companies"] = []
    mock_api.get(f"/composer/metacritic/pages/games/{SLUG}/web").respond(json=payload)
    with respx.mock(base_url=SITE, assert_all_called=True) as site:
        site.get(f"/game/{SLUG}/").respond(text=load(f"game_page_{SLUG}.html"))
        game = get_game(client, SLUG, with_user_scores=False)
    assert game.developer == "Gearbox Software"
    assert game.description.startswith("The definitive looter shooter")
    assert game.title == "Borderlands 4"  # из JSON, не перезаписан


def test_get_game_404(client, mock_api):
    mock_api.get("/composer/metacritic/pages/games/nope/web").respond(404)
    with pytest.raises(GameNotFoundError):
        get_game(client, "nope")


def _mock_reviews(mock_api):
    for kind, sentiment, offset in [
        ("critic", "positive", 0),
        ("critic", "positive", 10),
        ("critic", "negative", 0),
        ("critic", "neutral", 0),
        ("user", "positive", 0),
        ("user", "negative", 0),
    ]:
        mock_api.get(
            REVIEWS.format(kind=kind),
            params={"filterBySentiment": sentiment, "offset": str(offset)},
        ).respond(json=load(f"reviews_{kind}_{sentiment}_{offset}.json"))


def test_get_reviews_user_balanced(client, mock_api):
    _mock_reviews(mock_api)
    reviews = get_reviews(client, SLUG, "user", limit=30)
    assert len(reviews) == 30
    sentiments = [r.sentiment for r in reviews]
    assert sentiments.count("positive") == 15
    assert sentiments.count("negative") == 15
    assert all(r.quote and r.kind == "user" for r in reviews)
    assert all(r.score >= 7 for r in reviews if r.sentiment == "positive")
    assert all(r.score < 5 for r in reviews if r.sentiment == "negative")


def test_get_reviews_critic_paginates_and_fills_shortfall(client, mock_api):
    # Негатива у критиков нет: 15 позитивных, добор нейтральными, потом позитивом.
    _mock_reviews(mock_api)
    # Добор позитива с offset=15: хвост второй страницы, дальше страниц нет.
    tail = load("reviews_critic_positive_10.json")
    tail["data"]["items"] = tail["data"]["items"][5:]
    tail["links"]["next"]["href"] = None
    mock_api.get(
        REVIEWS.format(kind="critic"), params={"filterBySentiment": "positive", "offset": "15"}
    ).respond(json=tail)
    # Нейтральных на сайте 13, в фикстуре 10: вторая страница пустая.
    mock_api.get(
        REVIEWS.format(kind="critic"), params={"filterBySentiment": "neutral", "offset": "10"}
    ).respond(json={"data": {"items": [], "totalResults": 10}, "links": {}})
    reviews = get_reviews(client, SLUG, "critic", limit=30)
    sentiments = [r.sentiment for r in reviews]
    assert sentiments.count("negative") == 0
    assert sentiments.count("positive") == 20  # в фикстурах две страницы по 10
    assert sentiments.count("neutral") == 10
    assert len({(r.publication, r.quote) for r in reviews}) == len(reviews)  # без дублей
    assert reviews[0].publication


def test_client_retries_on_429(client, mock_api):
    route = mock_api.get("/finder/metacritic/web").mock(
        side_effect=[httpx.Response(429), httpx.Response(503), httpx.Response(200, json={})]
    )
    assert list_new_releases(client) == []
    assert route.call_count == 3


def test_client_gives_up_after_3_attempts(client, mock_api):
    route = mock_api.get("/finder/metacritic/web").respond(500)
    with pytest.raises(httpx.HTTPStatusError):
        list_new_releases(client)
    assert route.call_count == 3


def test_client_does_not_retry_404(client, mock_api):
    route = mock_api.get("/finder/metacritic/web").respond(404)
    with pytest.raises(httpx.HTTPStatusError):
        list_new_releases(client)
    assert route.call_count == 1


def test_client_blocks_foreign_redirect(client, mock_api):
    mock_api.get("/finder/metacritic/web").respond(
        302, headers={"Location": "https://evil.example.com/x"}
    )
    with pytest.raises(HostNotAllowedError):
        list_new_releases(client)


@pytest.mark.parametrize("slug", ["../../admin", "game?apiKey=x", "a/b", "", "UPPER", "a" * 300])
def test_bad_slug_never_reaches_api(client, slug):
    # respx не настроен: любой запрос к сети упал бы с ошибкой «не замокано»
    with respx.mock(assert_all_called=False):
        with pytest.raises(GameNotFoundError):
            get_game(client, slug)
        with pytest.raises(GameNotFoundError):
            get_reviews(client, slug, "critic")


def test_list_skips_bad_slugs():
    payload = {"data": {"items": [{"slug": "ok-game"}, {"slug": "../x"}, {"slug": None}]}}
    assert _slugs(payload) == ["ok-game"]
