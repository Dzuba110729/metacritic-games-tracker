"""Страницы сайта: список игр и карточка игры."""

from collections.abc import Iterator
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader
from sqlalchemy.orm import Session

from app.similar.search import similar_games
from app.web import queries
from app.web.security import http_url, is_metacritic_url, youtube_id

WEB_DIR = Path(__file__).parent
STATIC_DIR = WEB_DIR / "static"

MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def ru_date(value: date | None) -> str:
    if value is None:
        return "дата неизвестна"
    return f"{value.day} {MONTHS[value.month - 1]} {value.year}"


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def tone(score: float | None, scale: int = 100) -> str:
    """Цвет оценки по правилу Metacritic: 75+ зелёный, 50–74 жёлтый, ниже — красный."""
    if score is None:
        return "none"
    value = score * 100 / scale
    if value >= 75:
        return "good"
    if value >= 50:
        return "mixed"
    return "bad"


def userscore_text(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}"


def cover(url: str | None) -> str | None:
    return url if is_metacritic_url(url) else None


env = Environment(
    loader=FileSystemLoader(WEB_DIR / "templates"),
    autoescape=True,
    trim_blocks=True,
    lstrip_blocks=True,
)
env.filters.update(
    ru_date=ru_date, tone=tone, userscore=userscore_text, cover=cover, http_url=http_url
)
env.globals.update(plural=plural, youtube_id=youtube_id, SORTS=queries.SORTS)
templates = Jinja2Templates(env=env)

router = APIRouter()


def get_db(request: Request) -> Iterator[Session]:
    with request.app.state.sessionmaker() as session:
        yield session


def _int(value: str, default: int = 1) -> int:
    try:
        return int(value)
    except ValueError:
        return default


@router.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    q: str = "",
    platform: str = "",
    sort: str = "new",
    page: str = "1",
    db: Session = Depends(get_db),  # noqa: B008
):
    if sort not in queries.SORTS:
        sort = "new"
    q = q.strip()[: queries.MAX_QUERY_LEN]
    result = queries.list_games(db, q=q, platform=platform, sort=sort, page=_int(page))

    params = {"q": q, "platform": platform, "sort": "" if sort == "new" else sort}

    def page_url(n: int) -> str:
        query = urlencode({k: v for k, v in {**params, "page": n if n > 1 else ""}.items() if v})
        return "/?" + query if query else "/"

    context = {
        "q": q,
        "platform": platform,
        "sort": sort,
        "platforms": queries.platforms(db),
        "result": result,
        "page_url": page_url,
    }
    # HTMX-запрос от формы или пагинации — отдаём только список. Восстановление истории — всю
    # страницу.
    partial = request.headers.get("HX-Request") == "true" and not request.headers.get(
        "HX-History-Restore-Request"
    )
    template = "_results.html" if partial else "index.html"
    response = templates.TemplateResponse(request, template, context)
    response.headers["Vary"] = "HX-Request"
    return response


@router.get("/game/{slug}", response_class=HTMLResponse)
def game_page(request: Request, slug: str, db: Session = Depends(get_db)):  # noqa: B008
    game = queries.get_game(db, slug)
    if game is None:
        return templates.TemplateResponse(request, "404.html", {}, status_code=404)
    scores = sorted(game.scores, key=lambda s: s.platform)
    metascores = [s.metascore for s in scores if s.metascore is not None]
    userscores = [s.userscore for s in scores if s.userscore is not None]
    context = {
        "game": game,
        "scores": scores,
        "metascore": max(metascores, default=None),
        "userscore": max(userscores, default=None),
        "similar": similar_games(db, game.id, k=5),
    }
    return templates.TemplateResponse(request, "game.html", context)


async def not_found(request: Request, exc: Exception) -> HTMLResponse:
    """Любой неизвестный адрес — та же страница 404, что и для чужого slug."""
    return templates.TemplateResponse(request, "404.html", {}, status_code=404)
