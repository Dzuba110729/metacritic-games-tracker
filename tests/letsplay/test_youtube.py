import io

import pytest
from yt_dlp.utils import DownloadError

from app.letsplay.youtube import (
    Blocked,
    LetsplayError,
    _pick_subtitles,
    clean_vtt,
    find_letsplay,
    get_transcript,
    title_matches,
)
from tests.letsplay.conftest import FIXTURES, make_video

VTT = {"ext": "vtt", "url": "https://www.youtube.com/api/timedtext?v=x&fmt=vtt"}


def entry(vid, title, duration=1800, views=100):
    return {"id": vid, "title": title, "duration": duration, "view_count": views, "channel": "Ch"}


class FakeYdl:
    """Мок yt_dlp.YoutubeDL: поиск отдаёт entries, ролики — info по id."""

    def __init__(self, entries=(), infos=None, search_error=None, video_errors=None, vtt=b""):
        self.entries = list(entries)
        self.infos = infos or {}
        self.search_error = search_error
        self.video_errors = video_errors or {}
        self.vtt = vtt
        self.opened: list[str] = []
        self.checked: list[str] = []

    def __call__(self, opts):
        assert opts["skip_download"] is True
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, query, download):
        assert download is False
        if query.startswith("ytsearch"):
            if self.search_error:
                raise self.search_error
            return {"entries": self.entries}
        vid = query.rsplit("=", 1)[1]
        self.checked.append(vid)
        if vid in self.video_errors:
            raise self.video_errors[vid]
        return self.infos.get(vid, {"id": vid})

    def urlopen(self, url):
        self.opened.append(url)
        return io.BytesIO(self.vtt)


def test_clean_vtt_fixture():
    text = clean_vtt((FIXTURES / "letsplay_auto.vtt").read_text())
    assert text == (
        "welcome back everyone this combat feels amazing "
        "but the map is confusing & slow okay let's go"
    )


def test_clean_vtt_truncates_words():
    vtt = "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n" + " ".join(["слово"] * 50)
    assert len(clean_vtt(vtt, max_words=10).split()) == 10


@pytest.mark.parametrize(
    ("game", "video", "ok"),
    [
        ("Hollow Knight: Silksong", "Hollowknight Silksong #1 | Joe", True),
        ("Hollow Knight: Silksong", "HOLLOW KNIGHT SILKSONG - Part 1", True),
        ("Hollow Knight: Silksong", "Hollow Knight full playthrough", False),
        ("Warhammer 40,000: Boltgun 2", "BOLTGUN 2 Gameplay Walkthrough", True),
        ("Castlevania: Ark", "Ark survival lets play", False),  # подзаголовок короче 6 букв
        ("", "anything", False),
    ],
)
def test_title_matches(game, video, ok):
    assert title_matches(game, video) is ok


def test_picks_most_viewed_with_subtitles():
    ydl = FakeYdl(
        entries=[
            entry("short000001", "Silksong trailer", duration=120, views=10**9),
            entry("other000001", "Other game lets play", views=10**8),
            entry("popular0001", "Silksong lets play part 1", views=5000),
            entry("nosubs00001", "Silksong walkthrough", views=9000),
            entry("lesspopular", "Silksong gameplay", views=100),
        ],
        infos={
            "popular0001": {
                "id": "popular0001",
                "title": "Silksong lets play part 1",
                "channel": "Blogger",
                "view_count": 5001,
                "automatic_captions": {"en-orig": [VTT], "de": [VTT]},
            },
            "nosubs00001": {"id": "nosubs00001", "subtitles": {}, "automatic_captions": {}},
            "lesspopular": {"id": "lesspopular", "subtitles": {"ru": [VTT]}},
        },
    )
    found = find_letsplay("Silksong", ydl_factory=ydl)
    assert found.video is not None
    assert found.video.video_id == "popular0001"
    assert found.video.views == 5001
    assert found.video.channel == "Blogger"
    assert found.video.auto_subtitles is True and found.video.subtitle_lang == "en"
    # Короткий ролик и ролик без названия игры даже не проверялись.
    assert ydl.checked == ["nosubs00001", "popular0001"]


def test_manual_subtitles_preferred():
    info = {"subtitles": {"ru": [VTT]}, "automatic_captions": {"en": [VTT]}}
    assert _pick_subtitles(info) == (VTT["url"], "ru", False)
    assert _pick_subtitles({"automatic_captions": {"fr": [VTT]}}) is None


def test_not_found_and_no_subtitles():
    assert find_letsplay("Silksong", ydl_factory=FakeYdl()).reason == "not_found"
    ydl = FakeYdl(entries=[entry("nosubs00001", "Silksong part 1")])
    assert find_letsplay("Silksong", ydl_factory=ydl).reason == "no_subtitles"


def test_search_blocked_and_error():
    blocked = FakeYdl(search_error=DownloadError("Sign in to confirm you're not a bot"))
    with pytest.raises(Blocked):
        find_letsplay("Silksong", ydl_factory=blocked)
    broken = FakeYdl(search_error=DownloadError("Unable to download webpage"))
    with pytest.raises(LetsplayError) as info:
        find_letsplay("Silksong", ydl_factory=broken)
    assert info.value.status == "error"


def test_unavailable_video_is_skipped():
    ydl = FakeYdl(
        entries=[entry("private0001", "Silksong 1", views=900), entry("good0000001", "Silksong 2")],
        video_errors={"private0001": DownloadError("Private video")},
        infos={"good0000001": {"id": "good0000001", "automatic_captions": {"en": [VTT]}}},
    )
    assert find_letsplay("Silksong", ydl_factory=ydl).video.video_id == "good0000001"


def test_get_transcript_downloads_only_vtt():
    ydl = FakeYdl(vtt=(FIXTURES / "letsplay_auto.vtt").read_bytes())
    text = get_transcript(make_video(), ydl_factory=ydl)
    assert text.startswith("welcome back everyone")
    assert ydl.opened == [make_video().subtitle_url]


def test_get_transcript_rejects_foreign_host():
    ydl = FakeYdl()
    with pytest.raises(LetsplayError):
        get_transcript(make_video(subtitle_url="https://evil.example/sub.vtt"), ydl_factory=ydl)
    assert ydl.opened == []
