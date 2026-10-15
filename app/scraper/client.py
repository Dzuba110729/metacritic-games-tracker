"""HTTP-клиент Metacritic: браузерный User-Agent, пауза, повторы, белый список хостов."""

import logging
import threading
import time
from typing import Any

import httpx
from tenacity import (
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger(__name__)

BACKEND = "https://backend.metacritic.com"
SITE = "https://www.metacritic.com"
ALLOWED_HOSTS = frozenset({"metacritic.com", "www.metacritic.com", "backend.metacritic.com"})
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)


class HostNotAllowedError(ValueError):
    pass


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return isinstance(exc, httpx.TransportError)


def _check_host(request: httpx.Request) -> None:
    # Хук срабатывает и на редиректы: на чужой хост не уйдём даже по Location.
    if request.url.host not in ALLOWED_HOSTS:
        raise HostNotAllowedError(f"host not allowed: {request.url.host}")


class MetacriticClient:
    def __init__(
        self,
        api_key: str,
        *,
        min_interval: float = 1.0,
        timeout: float = 20.0,
        attempts: int = 3,
        retry_wait_max: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.api_key = api_key
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last_request = 0.0
        self._retrying = Retrying(
            retry=retry_if_exception(_is_retryable),
            stop=stop_after_attempt(attempts),
            wait=wait_exponential(multiplier=1, min=0, max=retry_wait_max),
            reraise=True,
        )
        self._http = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/html"},
            timeout=timeout,
            follow_redirects=True,
            event_hooks={"request": [_check_host]},
            transport=transport,
        )

    def __enter__(self) -> "MetacriticClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _throttle(self) -> None:
        # Не чаще одного запроса в min_interval секунд, даже из нескольких потоков.
        with self._lock:
            wait = self._last_request + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

    def _get(self, url: str, params: dict[str, Any] | None) -> httpx.Response:
        for attempt in self._retrying:
            with attempt:
                self._throttle()
                resp = self._http.get(url, params=params)
                if resp.status_code == 429 or resp.status_code >= 500:
                    log.warning("metacritic %s -> %s", resp.request.url, resp.status_code)
                resp.raise_for_status()
                return resp
        raise AssertionError("unreachable")  # pragma: no cover

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = {"apiKey": self.api_key, **(params or {})}
        return self._get(f"{BACKEND}{path}", params).json()

    def get_html(self, path: str) -> str:
        return self._get(f"{SITE}{path}", None).text
