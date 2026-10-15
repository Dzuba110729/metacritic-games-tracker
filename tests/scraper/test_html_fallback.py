from datetime import date

from app.scraper.html_fallback import parse_game_html

from .conftest import load


def test_parse_game_html():
    out = parse_game_html(load("game_page_borderlands-4.html"))
    assert out["title"] == "Borderlands 4"
    assert out["developer"] == "Gearbox Software"
    assert out["publisher"] == "2K Games"
    assert out["release_date"] == date(2025, 9, 12)
    assert out["genres"]
    assert out["cover_url"].startswith("https://www.metacritic.com/a/img/")
    assert "PC" in out["platform_names"]


def test_parse_game_html_several_developers():
    html = (
        '<div data-testid="hero-summary-developer"><p><span class="mr-1">Developer:</span>'
        '<span class="text-gray-800">Evil Empire</span><span class="text-gray-800">, </span>'
        '<a href="/company/motion-twin/">Motion Twin</a></p></div>'
    )
    assert parse_game_html(html)["developer"] == "Evil Empire, Motion Twin"


def test_parse_game_html_empty():
    assert parse_game_html("<html><body>nothing</body></html>") == {}
