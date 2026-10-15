"""Проверка внешних URL и заголовки безопасности."""

from urllib.parse import urlsplit

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

METACRITIC_HOST = "metacritic.com"
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}

CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' https://fonts.googleapis.com",
        "font-src https://fonts.gstatic.com",
        "img-src 'self' https://metacritic.com https://*.metacritic.com",
        "frame-src https://www.youtube-nocookie.com",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)

SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
}


def is_metacritic_url(url: str | None) -> bool:
    """https-ссылка на metacritic.com или его поддомен."""
    if not url:
        return False
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and (
        host == METACRITIC_HOST or host.endswith("." + METACRITIC_HOST)
    )


def http_url(url: str | None) -> str | None:
    """Ссылка для href: только http/https. javascript: и прочие схемы отбрасываем."""
    if not url:
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return url.strip()


def youtube_id(url: str | None) -> str | None:
    """ID ролика YouTube из ссылки или None, если это не YouTube."""
    if not url:
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or host not in YOUTUBE_HOSTS:
        return None
    if host == "youtu.be":
        vid = parts.path.lstrip("/")
    elif parts.path.startswith(("/embed/", "/shorts/")):
        vid = parts.path.split("/")[2]
    else:
        vid = next(
            (p[2:] for p in parts.query.split("&") if p.startswith("v=")),
            "",
        )
    ok = len(vid) == 11 and all(c.isascii() and (c.isalnum() or c in "-_") for c in vid)
    return vid if ok else None


class SecurityHeadersMiddleware:
    """Чистый ASGI-middleware: не буферизует ответ, подходит и для SSE (шаг 7)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)


def same_origin(origin: str | None, host: str | None) -> bool:
    """Заголовок Origin указывает на этот же сайт. Без Origin — отказ: браузеры шлют его
    в каждом POST. Схему не сравниваем: за Caddy приложение видит http, а браузер — https."""
    if not origin or not host:
        return False
    try:
        netloc = urlsplit(origin.strip()).netloc
    except ValueError:
        return False
    return bool(netloc) and netloc.lower() == host.strip().lower()
