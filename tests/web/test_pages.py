from datetime import date

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import Game, PlatformScore
from app.similar.embeddings import to_bytes
from app.web.security import http_url, is_metacritic_url, youtube_id

COVER = "https://www.metacritic.com/a/img/catalog/provider/7/2/{}.jpg"


def _vec(seed: int) -> bytes:
    v = np.random.default_rng(seed).normal(size=384).astype(np.float32)
    return to_bytes(v / np.linalg.norm(v))


def _game(slug, title, release, scores, **kw):
    g = Game(
        slug=slug,
        title=title,
        cover_url=COVER.format(slug),
        genres=["Action"],
        release_date=release,
        metacritic_url=f"https://www.metacritic.com/game/{slug}/",
        **kw,
    )
    g.scores = [PlatformScore(platform=p, metascore=m, userscore=u) for p, m, u in scores]
    return g


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        _env_file=None, database_url=f"sqlite:///{tmp_path}/web.db", scheduler_enabled=False
    )
    with TestClient(create_app(settings)) as c:
        with c.app.state.sessionmaker() as db:
            db.add_all(
                [
                    _game(
                        "alpha",
                        "Alpha Strike",
                        date(2026, 10, 1),
                        [("PC", 70, 9.1), ("PlayStation 5", 85, None)],
                        embedding=_vec(1),
                        critic_summary={
                            "status": "ok",
                            "likes": ["Стрельба"],
                            "dislikes": ["Баги"],
                            "verdict": "Хорошо.",
                            "based_on": 12,
                        },
                        user_summary={"status": "few_reviews", "based_on": 2},
                    ),
                    _game(
                        "beta",
                        "Бета-тест 100%",
                        date(2026, 10, 10),
                        [("PC", 60, 5.0)],
                        embedding=_vec(2),
                    ),
                    _game(
                        "gamma",
                        "Gamma <script>alert(1)</script>",
                        date(2026, 9, 1),
                        [("Nintendo Switch", None, 7.0)],
                        embedding=_vec(3),
                        video_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    ),
                    _game(
                        "delta",
                        "Delta",
                        None,
                        [],
                        letsplay={
                            "status": "ok",
                            "video_id": "abcdefghijk",
                            "url": "https://www.youtube.com/watch?v=abcdefghijk",
                            "title": "Delta <b>lets play</b>",
                            "channel": "Блогер",
                            "views": 1234567,
                            "summary": {
                                "impression": "Блогеру понравилось.",
                                "pros": ["Бои"],
                                "cons": [],
                                "recommend": "да",
                            },
                            "subtitles": "en (авто)",
                            "checked_at": "2026-10-15T10:00:00",
                        },
                    ),
                ]
            )
            db.commit()
        yield c


def titles(html: str) -> list[str]:
    import re

    return re.findall(r'<h2 class="card__title">(.*?)</h2>', html)


def test_index_lists_all_new_first(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert titles(resp.text) == [
        "Бета-тест 100%",
        "Alpha Strike",
        "Gamma &lt;script&gt;alert(1)&lt;/script&gt;",
        "Delta",
    ]


def test_platform_filter(client):
    resp = client.get("/", params={"platform": "PlayStation 5"})
    assert titles(resp.text) == ["Alpha Strike"]
    assert '<option value="PlayStation 5" selected>' in resp.text


def test_unknown_platform_gives_empty_state(client):
    resp = client.get("/", params={"platform": "Dreamcast"})
    assert titles(resp.text) == []
    assert "Ничего не нашли" in resp.text


@pytest.mark.parametrize(
    ("q", "expected"),
    [
        ("alpha", ["Alpha Strike"]),
        ("ALPHA", ["Alpha Strike"]),
        ("бета", ["Бета-тест 100%"]),  # кириллица без учёта регистра
        ("БЕТА", ["Бета-тест 100%"]),
        ("%", ["Бета-тест 100%"]),  # '%' ищется как символ, а не как шаблон LIKE
        ("_", []),
        ("<script>", ["Gamma &lt;script&gt;alert(1)&lt;/script&gt;"]),
        ("нет такой", []),
        # SQL-инъекции: ищутся как обычный текст, запрос параметризован
        ("' OR 1=1 --", []),
        ("alpha' OR '1'='1", []),
        ("'; DROP TABLE games; --", []),
    ],
)
def test_search(client, q, expected):
    resp = client.get("/", params={"q": q})
    assert resp.status_code == 200
    assert titles(resp.text) == expected


def test_sql_injection_does_not_break_db(client):
    for params in (
        {"q": "'; DROP TABLE games; --"},
        {"platform": "PC' OR 1=1 --"},
        {"sort": "metascore; DROP TABLE games"},
        {"page": "1 OR 1=1"},
    ):
        assert client.get("/", params=params).status_code in (200, 422)
    resp = client.get("/")
    assert len(titles(resp.text)) == 4


def test_search_query_is_escaped(client):
    resp = client.get("/", params={"q": "<script>alert(1)</script>"})
    assert "<script>alert(1)</script>" not in resp.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in resp.text


@pytest.mark.parametrize(
    ("sort", "expected"),
    [
        ("metascore", ["alpha", "beta", "gamma", "delta"]),  # 85 (лучшая), 60, без оценок
        ("userscore", ["alpha", "gamma", "beta", "delta"]),  # 9.1, 7.0, 5.0
        ("new", ["beta", "alpha", "gamma", "delta"]),
        ("garbage", ["beta", "alpha", "gamma", "delta"]),
    ],
)
def test_sort(client, sort, expected):
    import re

    resp = client.get("/", params={"sort": sort})
    assert re.findall(r'href="/game/([a-z]+)"', resp.text) == expected


def test_htmx_returns_partial(client):
    resp = client.get("/", params={"q": "alpha"}, headers={"HX-Request": "true"})
    assert "<html" not in resp.text
    assert titles(resp.text) == ["Alpha Strike"]
    restore = client.get("/", headers={"HX-Request": "true", "HX-History-Restore-Request": "true"})
    assert "<html" in restore.text


def test_pagination(client):
    with client.app.state.sessionmaker() as db:
        db.add_all(Game(slug=f"g{i:02d}", title=f"Filler {i:02d}") for i in range(30))
        db.commit()
    first = client.get("/", params={"q": "filler"})
    assert len(titles(first.text)) == 24
    assert "Страница 1 из 2" in first.text
    assert 'href="/?q=filler&amp;page=2"' in first.text
    second = client.get("/", params={"q": "filler", "page": "2"})
    assert len(titles(second.text)) == 6
    # Номер страницы вне диапазона и мусор не ломают страницу.
    assert client.get("/", params={"page": "abc"}).status_code == 200
    assert len(titles(client.get("/", params={"q": "filler", "page": "99"}).text)) == 6


def test_game_page(client):
    resp = client.get("/game/alpha")
    assert resp.status_code == 200
    text = resp.text
    assert "Alpha Strike" in text
    assert "Стрельба" in text and "Баги" in text and "Хорошо." in text
    assert "По 12 отзывам" in text
    assert "Мало отзывов: пока 2 отзыва" in text
    assert 'href="https://www.metacritic.com/game/alpha/"' in text
    # Похожие игры — ссылки на карточки.
    assert 'href="/game/beta"' in text and 'href="/game/gamma"' in text
    assert 'id="letsplay"' in text


def test_game_page_youtube_and_escaping(client):
    text = client.get("/game/gamma").text
    assert "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ" in text
    assert "<script>alert(1)</script>" not in text
    assert "Резюме готовится" in text


def test_game_page_without_scores(client):
    text = client.get("/game/delta").text
    assert "Оценок пока нет" in text
    assert "Похожие игры появятся" in text


@pytest.mark.parametrize("slug", ["nope", "..%2Fetc", "alpha%00"])
def test_unknown_slug_404(client, slug):
    resp = client.get(f"/game/{slug}")
    assert resp.status_code == 404
    assert "Такой страницы нет" in resp.text


@pytest.mark.parametrize("path", ["/", "/game/alpha", "/game/nope", "/static/style.css"])
def test_security_headers(client, path):
    h = client.get(path).headers
    csp = h["content-security-policy"]
    assert "script-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp
    assert h["x-content-type-options"] == "nosniff"
    assert h["referrer-policy"] == "strict-origin-when-cross-origin"
    assert h["x-frame-options"] == "DENY"


def test_foreign_cover_not_rendered(client):
    with client.app.state.sessionmaker() as db:
        g = db.query(Game).filter_by(slug="delta").one()
        g.cover_url = "https://evil.example/x.jpg"
        g.metacritic_url = "javascript:alert(1)"
        db.commit()
    text = client.get("/game/delta").text
    assert "evil.example" not in text
    assert "javascript:" not in text


def test_url_helpers():
    assert is_metacritic_url("https://www.metacritic.com/a/img/x.jpg")
    assert is_metacritic_url("https://metacritic.com/x.jpg")
    assert not is_metacritic_url("http://www.metacritic.com/x.jpg")
    assert not is_metacritic_url("https://metacritic.com.evil.io/x.jpg")
    assert not is_metacritic_url("https://evilmetacritic.com/x.jpg")
    assert not is_metacritic_url(None)
    assert http_url("javascript:alert(1)") is None
    assert http_url("https://cdn.jwplayer.com/players/x.html")
    assert youtube_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert youtube_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=1") == "dQw4w9WgXcQ"
    assert youtube_id('https://www.youtube.com/watch?v=bad"id') is None
    assert youtube_id("https://cdn.jwplayer.com/players/x.html") is None


def test_game_page_letsplay(client):
    text = client.get("/game/delta").text
    assert "https://www.youtube-nocookie.com/embed/abcdefghijk" in text
    assert "1\u00a0234\u00a0567 просмотров" in text
    assert "Блогер" in text and "Блогеру понравилось." in text
    assert "Советует играть:</b> да" in text
    assert "Претензий блогер не высказал" in text
    assert "<b>lets play</b>" not in text  # заголовок ролика экранирован


@pytest.mark.parametrize(
    ("letsplay", "expected"),
    [
        (None, "Летсплей ещё не искали"),
        ({"status": "not_found"}, "нет летсплея длиннее 5 минут"),
        ({"status": "no_subtitles"}, "без субтитров"),
        ({"status": "blocked"}, "YouTube временно ограничил"),
        ({"status": "error", "url": "javascript:alert(1)"}, "Конспект пока не получился"),
    ],
)
def test_game_page_letsplay_statuses(client, letsplay, expected):
    with client.app.state.sessionmaker() as db:
        db.add(Game(slug="lp", title="LP", letsplay=letsplay))
        db.commit()
    text = client.get("/game/lp").text
    assert expected in text
    assert "javascript:" not in text
