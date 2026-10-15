# Metacritic: внутренний JSON API

Исследовано 2026-10-15: страницы `www.metacritic.com` (Nuxt SSR) содержат в payload адреса
`backend.metacritic.com`, которые фронтенд вызывает сам. Все эндпоинты ниже отвечают `200` на
обычный `GET` с браузерным `User-Agent`, без cookies. Параметр `apiKey` сейчас не обязателен,
но фронтенд его передаёт — мы тоже передаём (`METACRITIC_API_KEY`, публичное значение по умолчанию).

База: `https://backend.metacritic.com`. Тип продукта «игра»: `mcoTypeId=13`.

## 1. New Releases (блок на `/game/`)

```
GET /finder/metacritic/web?sortBy=-releaseDate&metaScoreMin=1&offset=0&limit=20&mcoTypeId=13
```

Порядок slug совпадает с блоком «New Releases» в HTML `https://www.metacritic.com/game/`
(проверено сравнением). Блок показывает только игры с оценкой критиков (`metaScoreMin=1`).

Ответ: `data.items[]` — карточки (`slug`, `title`, `releaseDate`, `image`, `platforms[{id,name}]`,
`criticScoreSummary.score`, `userScore.score`, `genres`, `description`). `data.totalResults`.
Фикстура: `tests/fixtures/finder_new_releases.json`.

## 2. Browse «все игры, новые» (`/browse/game/all/all/all-time/new/?page=N`)

```
GET /finder/metacritic/web?sortBy=-releaseDate&productType=games&offset=<(N-1)*24>&limit=24&mcoTypeId=13
```

24 игры на страницу, включая игры без оценок. Список slug совпадает с HTML страницей `?page=2`
(проверено). `links.next.href` — следующая страница. Формат элементов как в п. 1.
Фикстура: `tests/fixtures/finder_browse_page2.json`.

## 3. Карточка игры

```
GET /composer/metacritic/pages/games/<slug>/web?apiKey=<key>
```

Ответ: `components[]`, у каждого `meta.componentName` и `data`. Нужное:

- `product` → `data.item`: `title`, `description`, `releaseDate`, `genres[].name`,
  `production.companies[]` (`typeName` = `Developer` / `Publisher`),
  `images[]` (`typeName` = `cardImage` — вертикальная обложка, `mainImage` — горизонтальная;
  URL = `https://www.metacritic.com/a/img/catalog` + `bucketPath`),
  `video.embedUrl` (трейлер на JW Player), `platforms[]` — `slug`, `name`,
  `criticScoreSummary.score` / `reviewCount` **по каждой платформе**.
- `critic-score-summary`, `user-score-summary` — сводка по ведущей платформе (`isLeadPlatform`).

Сам `product` доступен отдельно: `GET /games/metacritic/<slug>/web`.
Фикстура: `tests/fixtures/composer_borderlands-4.json` (оставлены только 3 компонента).

## 4. Оценки по платформам

Metascore по платформам есть в п. 3. User score по платформе — отдельный запрос:

```
GET /reviews/metacritic/user/games/<slug>/platform/<platform-slug>/stats/web
GET /reviews/metacritic/critic/games/<slug>/platform/<platform-slug>/stats/web
```

`data.item`: `score` (user — шкала 0–10, critic — 0–100), `reviewCount`, `positiveCount`,
`neutralCount`, `negativeCount`. Нет отзывов → `score: 0`, `reviewCount: 0` (считаем «нет оценки»).
Параметр `?platform=` у `/stats/web` **игнорируется** — платформа только в пути.
Фикстуры: `tests/fixtures/stats_user_borderlands-4_<platform>.json`.

## 5. Отзывы

```
GET /reviews/metacritic/<critic|user>/games/<slug>/web?offset=0&limit=50&filterBySentiment=<all|positive|neutral|negative>&sort=<score|date>
GET /reviews/metacritic/<critic|user>/games/<slug>/platform/<platform-slug>/web?...   # одна платформа
```

- `data.items[]`: `quote`, `score`, `date`, `author`, `platform`; у критиков ещё `publicationName`, `url`.
- `data.totalResults`, `links.next.href` (null на последней странице).
- **Критики: сервер режет `limit` до 10** — для 15 отзывов нужно 2 запроса (`offset=0`, `offset=10`).
  Игроки: `limit` до 50 работает.
- Тональность: positive (critic ≥75, user ≥7), neutral, negative (critic <50, user <5).
  У хорошо оценённых игр негативных отзывов критиков часто нет (пустой список).

Фикстуры: `tests/fixtures/reviews_<kind>_<sentiment>_<offset>.json` (игра `borderlands-4`).

## 6. Запасной путь — HTML страницы игры

`https://www.metacritic.com/game/<slug>/` содержит:

- `<script type="application/ld+json">` с `@type: VideoGame`: `name`, `description`,
  `datePublished`, `image`, `genre`, `gamePlatform[]`, `publisher[]`, `aggregateRating.ratingValue`.
- `[data-testid=hero-title] h1` — название, `[data-testid=hero-summary-developer]` — разработчики.

Используется, только если JSON API не отдал поле (см. `app/scraper/html_fallback.py`).
Фикстура: `tests/fixtures/game_page_borderlands-4.html` (обрезана до нужных блоков).

## Ограничения

- Лимиты запросов не задокументированы. Ходим не чаще 1 запроса в секунду, при 429/5xx повторяем
  с экспоненциальной паузой.
- Это внутренний API: формат может поменяться без предупреждения. Тесты на фикстурах ловят
  изменения разбора, но не изменения самого API.
