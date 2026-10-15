"""Обёртка над Anthropic SDK: structured outputs, учёт токенов и дневной потолок расходов."""

import json
import logging
from datetime import date
from typing import Any

import anthropic
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.models import LlmUsage
from app.scraper.selection import today_in

log = logging.getLogger(__name__)


class LlmError(Exception):
    """Вызов LLM не удался. retryable — имеет ли смысл повторить позже."""

    retryable = False


class BudgetExceeded(LlmError):
    """Дневной потолок расходов исчерпан: воркер ставит паузу до завтра."""


class LlmRateLimited(LlmError):
    retryable = True

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class LlmConnectionError(LlmError):
    retryable = True


class LlmApiError(LlmError):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = status_code >= 500


class LlmBadResponse(LlmError):
    """Модель ответила, но не JSON по схеме (отказ, обрыв по max_tokens)."""


def spent_today(db: Session, day: date) -> float:
    row = db.get(LlmUsage, day)
    return row.cost_usd if row else 0.0


class LlmClient:
    """Один экземпляр на прогон. Пишет расход в LlmUsage отдельной короткой сессией,
    чтобы учёт не зависел от транзакции вызывающего кода."""

    def __init__(
        self,
        settings: Settings,
        db_factory: sessionmaker[Session],
        sdk: Any | None = None,
    ):
        self.settings = settings
        self.db_factory = db_factory
        if sdk is None:
            key = settings.anthropic_api_key.get_secret_value() or None
            sdk = anthropic.Anthropic(api_key=key, max_retries=2, timeout=120.0)
        self.sdk = sdk
        # Итог за время жизни клиента — для CLI и логов прогона.
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost_usd = 0.0

    def _today(self) -> date:
        return today_in(self.settings.tz)

    def cost_of(self, input_tokens: int, output_tokens: int) -> float:
        s = self.settings
        return (
            input_tokens * s.llm_price_input_per_mtok + output_tokens * s.llm_price_output_per_mtok
        ) / 1_000_000

    def check_budget(self) -> None:
        with self.db_factory() as db:
            spent = spent_today(db, self._today())
        if spent >= self.settings.llm_daily_budget_usd:
            raise BudgetExceeded(
                f"потрачено ${spent:.4f} из ${self.settings.llm_daily_budget_usd:.2f} за сегодня"
            )

    def _record(self, input_tokens: int, output_tokens: int) -> None:
        cost = self.cost_of(input_tokens, output_tokens)
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cost_usd += cost
        with self.db_factory() as db:
            day = self._today()
            row = db.get(LlmUsage, day)
            if row is None:
                row = LlmUsage(date=day, input_tokens=0, output_tokens=0, cost_usd=0.0)
                db.add(row)
            row.input_tokens += input_tokens
            row.output_tokens += output_tokens
            row.cost_usd += cost
            db.commit()

    def complete_json(self, system: str, user: str, schema: dict, max_tokens: int = 4000) -> dict:
        """Один запрос с ответом строго по JSON-схеме. Возвращает разобранный dict."""
        self.check_budget()
        try:
            response = self.sdk.messages.create(
                model=self.settings.llm_model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": schema},
                },
            )
        except anthropic.RateLimitError as e:
            retry_after = e.response.headers.get("retry-after")
            raise LlmRateLimited(
                "лимит запросов Anthropic", float(retry_after) if retry_after else None
            ) from e
        except anthropic.APIStatusError as e:
            raise LlmApiError(f"ошибка Anthropic API {e.status_code}", e.status_code) from e
        except anthropic.APIConnectionError as e:
            raise LlmConnectionError("нет связи с Anthropic API") from e

        # Токены оплачены в любом случае — учитываем до разбора ответа.
        self._record(response.usage.input_tokens, response.usage.output_tokens)

        if response.stop_reason != "end_turn":
            raise LlmBadResponse(f"ответ не завершён: stop_reason={response.stop_reason}")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise LlmBadResponse("в ответе нет текстового блока")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise LlmBadResponse("ответ не JSON") from e
