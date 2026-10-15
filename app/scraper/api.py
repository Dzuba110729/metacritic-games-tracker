"""Разбор ответов внутреннего API Metacritic. Эндпоинты описаны в docs/metacritic-api.md."""

import logging
from datetime import date
from typing import Any

import httpx

from app.scraper.client import SITE, MetacriticClient
from app.scraper.html_fallback import parse_game_html
from app.scraper.schemas import GameData, PlatformScoreData, Review, ReviewKind, Sentiment

log = logging.getLogger(__name__)

GAME_TYPE_ID = 13
NEW_RELEASES_LIMIT = 20
BROWSE_PAGE_SIZE = 24
# Сервер отдаёт не больше 10 отзывов критиков за запрос, игроков — до 50.
REVIEWS_PAGE_CAP: dict[ReviewKind, int] = {"critic": 10, "user": 50}
IMAGE_BASE = f"{SITE}/a/img/catalog"


class GameNotFoundError(LookupError):
    pass


def _slugs(payload: dict[str, Any]) -> list[str]:
    items = (payload.get("data") or {}).get("items") or []
    return [it["slug"] for it in items if it.get("slug")]


def list_new_releases(client: MetacriticClient) -> list[str]:
    """Блок New Releases на /game/: 20 последних игр с оценкой критиков."""
    payload = client.get_json(
        "/finder/metacritic/web",
        {
            "sortBy": "-releaseDate",
            "metaScoreMin": 1,
            "offset": 0,
            "limit": NEW_RELEASES_LIMIT,
            "mcoTypeId": GAME_TYPE_ID,
        },
    )
    return _slugs(payload)


def list_browse_page(client: MetacriticClient, page: int) -> list[str]:
    """Страница /browse/game/all/all/all-time/new/?page=N (N с 1), 24 игры."""
    if page < 1:
        raise ValueError("page starts at 1")
    payload = client.get_json(
        "/finder/metacritic/web",
        {
            "sortBy": "-releaseDate",
            "productType": "games",
            "offset": (page - 1) * BROWSE_PAGE_SIZE,
            "limit": BROWSE_PAGE_SIZE,
            "mcoTypeId": GAME_TYPE_ID,
        },
    )
    return _slugs(payload)


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _image_url(images: list[dict[str, Any]]) -> str | None:
    by_type = {img.get("typeName"): img for img in images or []}
    for kind in ("cardImage", "mainImage"):
        img = by_type.get(kind)
        if img and img.get("bucketPath"):
            return IMAGE_BASE + img["bucketPath"]
    return None


def _companies(item: dict[str, Any], type_name: str) -> str | None:
    companies = (item.get("production") or {}).get("companies") or []
    names = [c["name"] for c in companies if c.get("typeName") == type_name and c.get("name")]
    return ", ".join(dict.fromkeys(names)) or None


def _component(payload: dict[str, Any], name: str) -> dict[str, Any] | None:
    for comp in payload.get("components") or []:
        if (comp.get("meta") or {}).get("componentName") == name:
            return ((comp.get("data") or {}).get("item")) or None
    return None


def _positive_int(value: Any) -> int | None:
    return int(value) if isinstance(value, int | float) and value > 0 else None


def parse_product(slug: str, item: dict[str, Any]) -> GameData:
    platforms = []
    for p in item.get("platforms") or []:
        critic = p.get("criticScoreSummary") or {}
        platforms.append(
            PlatformScoreData(
                platform=p.get("name") or p.get("slug") or "?",
                slug=p.get("slug") or "",
                metascore=_positive_int(critic.get("score")),
                critic_count=_positive_int(critic.get("reviewCount")),
            )
        )
    video = item.get("video") or {}
    return GameData(
        slug=slug,
        title=item.get("title") or "",
        cover_url=_image_url(item.get("images") or []),
        developer=_companies(item, "Developer"),
        publisher=_companies(item, "Publisher"),
        description=(item.get("description") or "").strip() or None,
        genres=[g["name"] for g in item.get("genres") or [] if g.get("name")],
        release_date=_parse_date(item.get("releaseDate")),
        video_url=video.get("embedUrl") or video.get("url"),
        metacritic_url=f"{SITE}/game/{slug}/",
        platforms=platforms,
    )


def apply_user_stats(score: PlatformScoreData, stats: dict[str, Any]) -> None:
    item = (stats.get("data") or {}).get("item") or {}
    score.user_count = _positive_int(item.get("reviewCount"))
    # Пока отзывов мало (или их нет), API отдаёт score 0: на сайте это «tbd», а не ноль.
    value = item.get("score")
    score.userscore = float(value) if isinstance(value, int | float) and value > 0 else None


FALLBACK_FIELDS = ("title", "description", "developer", "publisher", "genres", "release_date")


def _fill_from_html(game: GameData, html: str) -> None:
    """Запасной путь: дописывает поля, которых не было в JSON API."""
    parsed = parse_game_html(html)
    for name in (*FALLBACK_FIELDS, "cover_url"):
        if not getattr(game, name) and parsed.get(name):
            setattr(game, name, parsed[name])
    if not game.platforms and parsed.get("platform_names"):
        game.platforms = [
            PlatformScoreData(platform=n, slug="", metascore=None) for n in parsed["platform_names"]
        ]
        if parsed.get("metascore") and len(game.platforms) == 1:
            game.platforms[0].metascore = parsed["metascore"]


def get_game(client: MetacriticClient, slug: str, *, with_user_scores: bool = True) -> GameData:
    try:
        payload = client.get_json(f"/composer/metacritic/pages/games/{slug}/web")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise GameNotFoundError(slug) from exc
        raise
    item = _component(payload, "product")
    game = parse_product(slug, item) if item else GameData(slug=slug, title="")

    if with_user_scores:
        for score in game.platforms:
            if not score.slug:
                continue
            stats = client.get_json(
                f"/reviews/metacritic/user/games/{slug}/platform/{score.slug}/stats/web"
            )
            apply_user_stats(score, stats)

    missing = [f for f in FALLBACK_FIELDS if not getattr(game, f)]
    if missing or not game.cover_url:
        log.info("metacritic %s: JSON без полей %s, читаем HTML", slug, missing)
        try:
            _fill_from_html(game, client.get_html(f"/game/{slug}/"))
        except httpx.HTTPError as exc:
            log.warning("metacritic %s: HTML fallback failed: %s", slug, exc)
    if not game.title:
        raise GameNotFoundError(slug)
    return game


def _parse_review(kind: ReviewKind, sentiment: Sentiment, raw: dict[str, Any]) -> Review | None:
    quote = (raw.get("quote") or "").strip()
    if not quote:
        return None
    return Review(
        kind=kind,
        sentiment=sentiment,
        score=raw.get("score"),
        quote=quote,
        author=raw.get("author") or None,
        publication=raw.get("publicationName") or None,
        date=raw.get("date"),
        platform=raw.get("platform") if isinstance(raw.get("platform"), str) else None,
    )


def _fetch_reviews(
    client: MetacriticClient,
    slug: str,
    kind: ReviewKind,
    sentiment: Sentiment,
    want: int,
    offset: int = 0,
) -> tuple[list[Review], int, int]:
    """До want отзывов одной тональности, начиная с offset.

    Возвращает (отзывы, всего на сайте, offset для следующего запроса).
    """
    out: list[Review] = []
    total = 0
    cap = REVIEWS_PAGE_CAP[kind]
    while len(out) < want:
        payload = client.get_json(
            f"/reviews/metacritic/{kind}/games/{slug}/web",
            {
                "offset": offset,
                "limit": cap,
                "filterBySentiment": sentiment,
                "sort": "date",
            },
        )
        data = payload.get("data") or {}
        total = data.get("totalResults") or 0
        items = data.get("items") or []
        for raw in items:
            offset += 1
            if review := _parse_review(kind, sentiment, raw):
                out.append(review)
                if len(out) == want:
                    break
        next_href = ((payload.get("links") or {}).get("next") or {}).get("href")
        if not items or not next_href or offset >= total:
            break
    return out, total, offset


def get_reviews(
    client: MetacriticClient, slug: str, kind: ReviewKind, limit: int = 30
) -> list[Review]:
    """До limit отзывов: поровну позитивных и негативных.

    Если одной стороны не хватает, добираем нейтральными (они ближе к балансу),
    а затем другой стороной.
    """
    half = limit // 2
    pos, pos_total, pos_next = _fetch_reviews(client, slug, kind, "positive", half)
    neg, neg_total, neg_next = _fetch_reviews(client, slug, kind, "negative", limit - half)

    short = limit - len(pos) - len(neg)
    neutral: list[Review] = []
    if short > 0:
        neutral, _, _ = _fetch_reviews(client, slug, kind, "neutral", short)
        short -= len(neutral)
    if short > 0 and pos_total > pos_next:
        more, _, _ = _fetch_reviews(client, slug, kind, "positive", short, offset=pos_next)
        pos += more
        short -= len(more)
    if short > 0 and neg_total > neg_next:
        more, _, _ = _fetch_reviews(client, slug, kind, "negative", short, offset=neg_next)
        neg += more
    return pos + neg + neutral
