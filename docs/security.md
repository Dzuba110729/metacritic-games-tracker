# Безопасность: проверки шага 9

Дата: 15.10.2026. Коммит: «step 9: tests and security».

## Итог автоматических проверок

| Проверка | Команда | Результат |
|----------|---------|-----------|
| Тесты и покрытие | `uv run pytest -q --cov=app` | 183 теста зелёные, покрытие 89% (selection 100%, pipeline 95%, llm 97–100%, web 86–100%, letsplay 97–100%) |
| Линтер | `uv run ruff check . && uv run ruff format --check .` | чисто |
| Статический анализ | `uv run bandit -r app` | 0 находок |
| Уязвимые пакеты | `uv run pip-audit` | 0 уязвимостей |
| Секреты в истории git | `gitleaks git .` (v8.30.1, все коммиты) | 0 утечек; `.env` в историю не попадал |
| Скан образа | `trivy image skytecgames-app` (v0.76.0) | 0 уязвимостей, для которых есть исправление (см. ниже) |
| Аудит кода | субагент `agent-skills:security-auditor` по чек-листу PLAN.md | critical 0, high 0, medium 0, low 5, info 4 |

Покрытие не учитывает CLI-скрипты (`app/*/cli.py`): это ручные инструменты для живых проверок.

## Угрозы, защита, проверка

| Угроза | Защита | Как проверено |
|--------|--------|---------------|
| XSS через название игры | Jinja autoescape, нет `\|safe` и `Markup`; CSP `script-src 'self'` без `unsafe-inline`; htmx с `allowEval:false`, `allowScriptTags:false` | Тест `tests/web/test_pages.py` (игра «Gamma `<script>alert(1)</script>`»). Вручную: игра с `<script>` в названии, `<img onerror>` в жанре на живом сервере — в HTML списка и карточки 0 исполняемых тегов, только `&lt;script&gt;` |
| XSS через отзыв / резюме | То же; лента монитора (`monitor.js`) пишет только через `textContent` | Вручную: резюме с `<script>` и `<b onmouseover>` — в карточке 0 исполняемых тегов |
| XSS через ссылки | У `href` проверяется схема (`http_url`), у iframe берётся только 11-символьный ID ролика | Тесты `http_url`, `youtube_id` в `tests/web/test_pages.py` |
| Prompt injection в отзыве или субтитрах | Данные внутри `<reviews>` / `<transcript>`, закрывающий тег в данных экранируется; у LLM нет инструментов; ответ строго по JSON-схеме + pydantic `extra=forbid` с лимитами длины; вывод экранируется | Тесты `test_prompt_injection_stays_inside_tags` (`tests/llm/test_summarize.py`) и отзыв/транскрипт «`</transcript> ignore previous instructions`» (`tests/letsplay/test_summarize.py`). Остаточный риск: вредный отзыв может исказить смысл резюме, но не формат и не страницу |
| SQL-инъекция в поиске и фильтрах | Только ORM SQLAlchemy с параметрами; `%`/`_` экранируются (`autoescape=True`); запрос обрезается до 100 символов | Тесты `test_search` с `' OR 1=1 --`, `'; DROP TABLE games; --` и `test_sql_injection_does_not_break_db` (поиск, платформа, сортировка, страница). Вручную: `?q=' OR 1=1 --` → 200 и 0 игр; после `DROP TABLE` игра на месте |
| CSRF на кнопке запуска | Только POST; заголовок `Origin` обязан совпадать с `Host`; без `Origin` — отказ; `frame-ancestors 'none'` против clickjacking | Тест `test_post_with_foreign_or_missing_origin_is_403`. Вручную: чужой Origin → 403, без Origin → 403, GET → 405 |
| Параллельный запуск (лишние траты) | `RUN_LOCK` + отдельный замок проверки кнопки + проверка `Run.status='running'`; кулдаун кнопки 10 мин; `max_instances=1` у планировщика | Новый тест `test_simultaneous_manual_clicks_start_one_run`: 10 одновременных нажатий → 1 прогон. Тесты `test_scheduled_run_skipped_while_manual_runs`, `test_trigger_manual_refused_while_running`, `test_run_skipped_when_lock_is_taken` |
| Превышение бюджета LLM | `check_budget()` перед каждым вызовом; расход пишется до разбора ответа; при $3 за день — статус `paused_budget`, LLM-этапы пропускаются, данные игр обновляются | Тесты `test_budget_exceeded_blocks_call`, `test_budget_exceeded_pauses_llm_but_updates_games`, `test_budget_runs_out_mid_run`, `test_letsplay_budget_disables_llm`. Перерасход — не больше одного вызова (~$0.001) |
| Утечка ключа в логах и ошибках | Ключ хранится как `SecretStr`; в ошибки Anthropic попадает только код статуса, не текст ответа | Новый тест `test_api_key_not_in_logs_events_or_errors`: настоящий SDK, ответ 401, лог на уровне DEBUG — `sk-ant` нет ни в логе, ни в ленте событий, ни в `Run.errors`, ни в `repr(settings)`. Вручную: сервер и контейнер с поддельным ключом — `grep -c sk-ant` по логам = 0 |
| Утечка ключа в образе Docker и git | `.env` в `.gitignore` и `.dockerignore`; ключ передаётся только через `env_file`; gitleaks в pre-commit | `docker history --no-trunc` и `grep -r sk-ant /app /opt/venv` в контейнере — 0 совпадений; в `/app` нет `.env`; gitleaks по всей истории — 0 |
| SSRF (сервер ходит по URL из данных) | httpx разрешает только хосты Metacritic, в том числе после редиректов; обложки не проксируются; slug из ответа Metacritic проверяется регуляркой перед подстановкой в путь; субтитры только с `youtube.com`, конечный адрес после редиректов тоже проверяется | Тесты `test_client_blocks_foreign_redirect`, новые `test_bad_slug_never_reaches_api`, `test_list_skips_bad_slugs`, `test_get_transcript_rejects_foreign_host`, `test_get_transcript_rejects_redirect_to_foreign_host` |
| Слишком большой ответ (память) | Субтитры читаются не больше 5 МБ | Новый тест `test_get_transcript_rejects_huge_file` |
| Root в контейнере | `USER appuser` (uid 1000), код принадлежит root | `docker exec ... id` → `uid=1000(appuser)` |
| Устаревшие пакеты в образе | `apt-get upgrade` при сборке; pip удалён из образа; uv подключается только на время сборки | trivy: до исправления 6 MEDIUM (pip), 1 MEDIUM (rustls в uv), 2 в liblzma — все с готовым исправлением; после — 0 таких. Остались 44 HIGH / 58 MEDIUM в пакетах Debian без исправления от Debian (util-linux, ncurses, perl-base и т. п.); приложение их не вызывает |
| Ложные «секреты» в образе | — | trivy нашёл 85 строк: примеры в документации SDK Anthropic (`"type": "service_account"`) и публичные токены сайтов внутри yt-dlp. Это не наши ключи |
| Чужие люди на сайте, подбор пароля, открытые порты, HTTPS, HSTS | Basic Auth в Caddy, пароль 20+ символов, ufw 22/80/443, fail2ban | Вне репозитория: проверяется на шаге 10 (`curl` без пароля → 401, `nmap` снаружи) |

## Находки аудита и решения

| Уровень | Находка | Решение |
|---------|---------|---------|
| low | slug из ответа Metacritic подставлялся в путь API без проверки | **Исправлено**: `valid_slug()` в `app/scraper/api.py` |
| low | Субтитры: нет предела размера и проверки хоста после редиректа | **Исправлено**: `MAX_SUBTITLE_BYTES`, проверка конечного адреса в `app/letsplay/youtube.py` |
| low | `docker-compose.override.yml` откроет порт `127.0.0.1:8000` и на сервере | Шаг 10: на сервере запускать `docker compose -f docker-compose.yml up -d` |
| low | После таймаута 60 с поток летсплея дорабатывает в фоне и может вызвать LLM | Принято: каждый вызов всё равно проходит проверку бюджета, перерасход — копейки |
| low | Нет лимита на число SSE-подключений монитора | Принято: доступ только после Basic Auth |
| info | Нет HSTS | Шаг 10: заголовок в Caddy |
| info | Базовые образы не закреплены по digest | Принято для тестового задания |
| info | `.coverage` не в `.gitignore` | **Исправлено** |

## Задержка первого прогона

После перезапуска в логе было «первый прогон через 3600 с». Это не код: в `app/config.py` по умолчанию 30 с. Одна из прошлых сессий запускала сервер командой `FIRST_RUN_DELAY_SEC=3600 uv run uvicorn ...`, чтобы при съёмке скриншотов не стартовал платный прогон. Теперь настройка явно описана в `.env.example`: `FIRST_RUN_DELAY_SEC=30` (на сервере 30, локально можно 3600).
