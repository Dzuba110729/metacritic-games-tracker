"""Поиск летсплея на YouTube и загрузка субтитров через yt-dlp (без скачивания видео)."""

import html
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import yt_dlp
from yt_dlp.utils import YoutubeDLError

log = logging.getLogger(__name__)

SEARCH_SIZE = 10
MIN_DURATION_SEC = 5 * 60
# Сколько кандидатов проверяем на субтитры: каждая проверка — отдельный запрос к YouTube.
MAX_CANDIDATES = 5
MAX_WORDS = 15_000
SOCKET_TIMEOUT_SEC = 15
SUB_LANGS = ("en", "ru")
# Субтитры качаем только с YouTube: URL приходит из ответа yt-dlp, проверяем хост.
SUBTITLE_HOSTS = ("youtube.com", "www.youtube.com")
# Предел размера дорожки субтитров: час речи в vtt — около 1 МБ.
MAX_SUBTITLE_BYTES = 5_000_000

BASE_OPTS: dict[str, Any] = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "noplaylist": True,
    "socket_timeout": SOCKET_TIMEOUT_SEC,
}


class LetsplayError(Exception):
    """Сбой YouTube. status — что записать в карточку: blocked или error."""

    status = "error"


class Blocked(LetsplayError):
    status = "blocked"


@dataclass
class Video:
    video_id: str
    url: str
    title: str
    channel: str | None
    views: int
    subtitle_url: str
    subtitle_lang: str
    auto_subtitles: bool


@dataclass
class SearchResult:
    """video — найденный ролик; иначе reason: not_found (подходящих нет) или no_subtitles."""

    video: Video | None
    reason: str | None = None


YdlFactory = Callable[[dict[str, Any]], yt_dlp.YoutubeDL]


def _norm(text: str) -> str:
    """Только буквы и цифры в нижнем регистре: «Hollow Knight: Silksong» -> hollowknightsilksong."""
    return "".join(c for c in text.casefold() if c.isalnum())


MIN_SHORT_TITLE = 6


def title_matches(game_title: str, video_title: str) -> bool:
    """Название игры есть в заголовке ролика. Для «Серия: Подзаголовок» хватает подзаголовка
    («Warhammer 40,000: Boltgun 2» -> «Boltgun 2»), если он не слишком короткий."""
    video = _norm(video_title)
    names = {_norm(game_title)}
    if ":" in game_title:
        short = _norm(game_title.rsplit(":", 1)[1])
        if len(short) >= MIN_SHORT_TITLE:
            names.add(short)
    return any(name and name in video for name in names)


def _wrap(exc: Exception) -> LetsplayError:
    text = str(exc)
    low = text.lower()
    if any(m in low for m in ("sign in to confirm", "429", "too many requests", "not a bot")):
        return Blocked(text[:300])
    return LetsplayError(text[:300])


def _pick_subtitles(info: dict[str, Any]) -> tuple[str, str, bool] | None:
    """(url vtt, язык, автоматические?) или None. Порядок: обычные en, ru, затем авто en, ru."""
    for auto, key in ((False, "subtitles"), (True, "automatic_captions")):
        tracks: dict[str, list[dict]] = info.get(key) or {}
        for want in SUB_LANGS:
            # У авто-субтитров «en-orig» — исходная дорожка, «en» может быть её копией.
            names = [f"{want}-orig", want] + sorted(k for k in tracks if k.startswith(want + "-"))
            for name in names:
                vtt = next((f for f in tracks.get(name) or [] if f.get("ext") == "vtt"), None)
                if vtt and vtt.get("url"):
                    return vtt["url"], want, auto
    return None


def find_letsplay(game_title: str, ydl_factory: YdlFactory = yt_dlp.YoutubeDL) -> SearchResult:
    """Самый просматриваемый летсплей длиннее 5 минут с субтитрами en/ru."""
    query = f'ytsearch{SEARCH_SIZE}:"{game_title}" lets play gameplay'
    try:
        with ydl_factory({**BASE_OPTS, "extract_flat": "in_playlist"}) as ydl:
            found = ydl.extract_info(query, download=False) or {}
    except YoutubeDLError as exc:
        raise _wrap(exc) from exc

    candidates = [
        e
        for e in found.get("entries") or []
        if e
        and e.get("id")
        and (e.get("duration") or 0) >= MIN_DURATION_SEC
        and title_matches(game_title, e.get("title") or "")
    ]
    if not candidates:
        return SearchResult(None, "not_found")
    candidates.sort(key=lambda e: e.get("view_count") or 0, reverse=True)

    with ydl_factory(BASE_OPTS) as ydl:
        for entry in candidates[:MAX_CANDIDATES]:
            url = f"https://www.youtube.com/watch?v={entry['id']}"
            try:
                info = ydl.extract_info(url, download=False) or {}
            except YoutubeDLError as exc:
                err = _wrap(exc)
                if isinstance(err, Blocked):
                    raise err from exc
                log.info("ролик %s пропущен: %s", entry["id"], err)
                continue
            subs = _pick_subtitles(info)
            if subs is None:
                continue
            sub_url, lang, auto = subs
            return SearchResult(
                Video(
                    video_id=info.get("id") or entry["id"],
                    url=url,
                    title=info.get("title") or entry.get("title") or "",
                    channel=info.get("channel") or info.get("uploader") or entry.get("channel"),
                    views=int(info.get("view_count") or entry.get("view_count") or 0),
                    subtitle_url=sub_url,
                    subtitle_lang=lang,
                    auto_subtitles=auto,
                )
            )
    return SearchResult(None, "no_subtitles")


TAG_RE = re.compile(r"<[^>]*>")
NOISE_RE = re.compile(r"\[(?:music|applause|laughter|музыка|аплодисменты|смех)\]", re.IGNORECASE)
SPACE_RE = re.compile(r"\s+")


def clean_vtt(vtt: str, max_words: int = MAX_WORDS) -> str:
    """Текст субтитров без таймингов, тегов и повторов строк автосубтитров."""
    lines: list[str] = []
    skip_block = False
    for raw in vtt.splitlines():
        line = raw.strip()
        if not line:
            skip_block = False
            continue
        if skip_block:
            continue
        if line.startswith(("NOTE", "STYLE", "REGION")):
            skip_block = True
            continue
        if line == "WEBVTT" or line.startswith(("WEBVTT ", "Kind:", "Language:")):
            continue
        if "-->" in line or line.isdigit():
            continue
        text = NOISE_RE.sub(" ", html.unescape(TAG_RE.sub("", line)))
        text = SPACE_RE.sub(" ", text).strip()
        # Автосубтитры повторяют предыдущую строку в начале каждого нового фрагмента.
        if not text or text in lines[-2:]:
            continue
        lines.append(text)
    words = " ".join(lines).split()
    return " ".join(words[:max_words])


def get_transcript(video: Video, ydl_factory: YdlFactory = yt_dlp.YoutubeDL) -> str:
    """Скачивает только дорожку субтитров (vtt) и возвращает очищенный текст."""
    host = (urlsplit(video.subtitle_url).hostname or "").lower()
    if urlsplit(video.subtitle_url).scheme != "https" or host not in SUBTITLE_HOSTS:
        raise LetsplayError(f"субтитры не с YouTube: {host}")
    try:
        with ydl_factory(BASE_OPTS) as ydl:
            resp = ydl.urlopen(video.subtitle_url)
            # urlopen идёт по редиректам: проверяем и конечный адрес.
            final_host = urlsplit(getattr(resp, "url", None) or video.subtitle_url).hostname or ""
            if final_host.lower() not in SUBTITLE_HOSTS:
                raise LetsplayError(f"субтитры перенаправлены не на YouTube: {final_host}")
            raw = resp.read(MAX_SUBTITLE_BYTES + 1)
    except (YoutubeDLError, OSError) as exc:  # DownloadError, HTTPError, сетевые ошибки
        raise _wrap(exc) from exc
    if len(raw) > MAX_SUBTITLE_BYTES:
        raise LetsplayError("субтитры слишком большие")
    return clean_vtt(raw.decode("utf-8", errors="replace"))
