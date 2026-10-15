# Переписка с Claude Code

Проект сделан в 12 сессиях Claude Code: одна сессия планирования и по одной на каждый шаг
[PLAN.md](../PLAN.md). Сами логи сессий (JSONL) **приложены к письму** архивом `ai-chats.zip`,
в репозиторий они не входят. Внутри архива `ai-chats/README.md` объясняет, как их читать.

| Файл в архиве | Что делали |
|---------------|-----------|
| `00-plan.jsonl` | Планирование: разбор ТЗ, выбор стека, PLAN.md и CLAUDE.md, запуск и приёмка сессий шагов |
| `01-skeleton.jsonl` | Шаг 1. Каркас проекта: uv, FastAPI, SQLAlchemy + SQLite, Docker, pre-commit с gitleaks |
| `02-scraper.jsonl` | Шаг 2. Сборщик Metacritic: внутренний JSON API, отзывы, отбор 20 игр в час |
| `03-llm.jsonl` | Шаг 3. Резюме отзывов через Claude Haiku 5.5: JSON-схема, дневной бюджет $3 |
| `04-similar.jsonl` | Шаг 4. Похожие игры: эмбеддинги bge-small через fastembed |
| `05-worker.jsonl` | Шаг 5. Планировщик APScheduler и конвейер прогона |
| `06-web.jsonl` | Шаг 6. Веб-интерфейс: Jinja2 + HTMX, список, фильтры, карточка игры |
| `07-monitor.jsonl` | Шаг 7 (доп. часть 2). Мониторинг прогонов через SSE и кнопка ручного запуска |
| `08-letsplay.jsonl` | Шаг 8 (доп. часть 1). Летсплеи YouTube через yt-dlp и конспект субтитров |
| `09-security.jsonl` | Шаг 9. Тесты и аудит безопасности (+ лог субагента-аудитора в `09-security/subagents/`) |
| `10-deploy.jsonl` | Шаг 10. Деплой на VPS: Docker Compose, nginx, certbot, Basic Auth |
| `11-docs.jsonl` | Шаг 11. README, выгрузка переписки, черновик письма |

Перед упаковкой [scripts/export_chats.py](../scripts/export_chats.py) заменяет личные данные
и секреты на `[скрыто]` / `[REDACTED]`, затем проверяет результат gitleaks и поиском ключей.
