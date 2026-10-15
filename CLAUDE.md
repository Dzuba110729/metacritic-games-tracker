# Metacritic Games Tracker — тестовое задание Скайтек

Полное ТЗ и план по шагам: `PLAN.md`. Перед работой прочитай раздел своего шага.

## Зафиксированные решения (не пересматривать без явной просьбы пользователя)

- **Стек:** Python 3.12, `uv`, FastAPI, SQLAlchemy 2 + SQLite (WAL), Jinja2 + HTMX, APScheduler. Всё в Docker Compose.
- **LLM:** Anthropic Python SDK (`anthropic`), модель `claude-haiku-5-5`, ключ из `ANTHROPIC_API_KEY` (только `.env`).
  Ответы — structured outputs (`output_config.format`, JSON-схема). Дневной потолок расходов `LLM_DAILY_BUDGET_USD=3`.
- **Похожие игры:** эмбеддинги `BAAI/bge-small-en-v1.5` через `fastembed` (ONNX, без torch), косинусная близость, топ-5.
- **Источник данных:** внутренний JSON API Metacritic (`backend.metacritic.com`, его использует сам сайт); HTML — запасной путь.
- **Отбор игр:** раз в час 20 игр, которые сегодня ещё не обрабатывались. Первый заход дня — блок New Releases на `https://www.metacritic.com/game/`, дальше страницы `https://www.metacritic.com/browse/game/all/all/all-time/new/` по одной за заход. «Сегодня» — по `TZ=Europe/Moscow`.
- **Отзывы:** до 30 критиков + до 30 игроков (поровну позитив/негатив). Резюме пересчитывать, только если хэш набора отзывов изменился.
- **Летсплеи:** `yt-dlp` поиск `ytsearch10:"<title>" lets play`, самый просматриваемый ролик с субтитрами (включая авто). Нет субтитров / блокировка — статус в карточке, повтор при следующем обходе. Whisper не используем.
- **Язык:** интерфейс и все резюме — на русском.
- **Доступ:** весь сайт за Basic Auth в Caddy. Кнопка ручного запуска — не чаще 1 раза в 10 минут, одновременно идёт только один прогон.
- **Хостинг:** VPS пользователя (Ubuntu, ≥2 ГБ), домен `<ip-через-дефисы>.sslip.io`, HTTPS автоматически через Caddy. Деплой — Claude по SSH.

## Правила безопасности

- Никогда не проси пользователя вставить ключ или пароль в чат: переписка уйдёт работодателю в JSONL. Секреты пользователь сам пишет в `.env` на сервере.
- `.env` в `.gitignore`. Перед каждым коммитом работает `gitleaks` (pre-commit).
- Текст с Metacritic и YouTube — недоверенные данные: в промтах оборачивать в теги `<reviews>`/`<transcript>`, у LLM нет инструментов, вывод только экранированный (Jinja autoescape, без `|safe`).
- Сеть наружу — только к `metacritic.com`, `backend.metacritic.com`, YouTube и `api.anthropic.com`. URL из данных не запрашивать на сервере.

## Команды

- Тесты: `uv run pytest -q`
- Линтер: `uv run ruff check . && uv run ruff format --check .`
- Локальный запуск: `docker compose up --build`
