"""Запасной разбор HTML страницы игры.

Используется, только если JSON API (composer) не отдал поле: название, описание, разработчик,
жанры, дата, обложка. Источники в HTML — блок JSON-LD (schema.org VideoGame) и hero-блок.
"""

import json
from datetime import date
from typing import Any

from selectolax.lexbor import LexborHTMLParser


def _ld_json(tree: LexborHTMLParser) -> dict[str, Any]:
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text())
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("@type") == "VideoGame":
            return data
    return {}


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def parse_game_html(html: str) -> dict[str, Any]:
    """Возвращает только найденные поля (ключи как в GameData)."""
    tree = LexborHTMLParser(html)
    ld = _ld_json(tree)
    out: dict[str, Any] = {}

    h1 = tree.css_first('[data-testid="hero-title"] h1')
    title = (h1.text(strip=True) if h1 else None) or ld.get("name")
    if title:
        out["title"] = title

    if ld.get("description"):
        out["description"] = ld["description"].strip()

    dev = tree.css_first('[data-testid="hero-summary-developer"] p')
    if dev:
        names = [n.text(strip=True) for n in dev.css("span.text-gray-800, a")]
        names = [n for n in names if n and n != ","]
        if names:
            out["developer"] = ", ".join(names)

    publishers = [p.get("name") for p in ld.get("publisher") or [] if isinstance(p, dict)]
    if publishers:
        out["publisher"] = ", ".join(p for p in publishers if p)

    genre = ld.get("genre")
    if genre:
        out["genres"] = [genre] if isinstance(genre, str) else list(genre)

    if d := _parse_date(ld.get("datePublished")):
        out["release_date"] = d

    if isinstance(ld.get("image"), str):
        out["cover_url"] = ld["image"]

    if ld.get("gamePlatform"):
        out["platform_names"] = list(ld["gamePlatform"])

    rating = ld.get("aggregateRating") or {}
    if isinstance(rating.get("ratingValue"), int | float):
        out["metascore"] = int(rating["ratingValue"])

    return out
