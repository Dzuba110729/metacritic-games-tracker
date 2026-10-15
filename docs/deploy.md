# Деплой на VPS

Сервис работает на общем VPS `188.130.141.107` (Ubuntu 22.04, 2 vCPU, 4 ГБ RAM).
Адрес: **https://188-130-141-107.sslip.io** (логин `reviewer`, пароль — в письме).

На сервере уже живут чужие сайты (`[скрыто]`, `[скрыто]`, CRM на `[скрыто]`),
[скрыто], VPN [скрыто] и [скрыто]. Деплой сделан **только добавлениями**: ufw, sshd, docker-демон,
чужие конфиги nginx, контейнеры и сети не менялись.

## Что и где лежит

| Что | Где |
|-----|-----|
| Код (git clone) | `/opt/metacritic-games-tracker` |
| Секреты (ключ Claude и настройки) | `/opt/metacritic-games-tracker/.env`, права `600`, пишется вручную |
| База SQLite и кэш модели эмбеддингов | `/opt/metacritic-games-tracker/data/` (владелец uid 1000 — пользователь в контейнере) |
| Docker Compose | проект `metacritic`, файлы `docker-compose.yml` + `docker-compose.prod.yml` |
| Контейнер | `metacritic-app-1`, порт `127.0.0.1:8001` (только локально), лимит памяти 1 ГБ |
| Docker-сеть | `metacritic_net`, подсеть `172.30.0.0/24` (не пересекается с 172.17/16, 172.18/16, [скрыто]) |
| Логи контейнера | json-file с ротацией: 3 файла по 10 МБ (`docker logs metacritic-app-1`) |
| Сайт nginx | `/etc/nginx/sites-available/metacritic` + симлинк в `sites-enabled` (шаблон до certbot — `deploy/nginx-metacritic.conf`) |
| Пароль сайта (Basic Auth) | `/etc/nginx/.htpasswd-metacritic`, `root:www-data`, `640` |
| Сертификат Let's Encrypt | `/etc/letsencrypt/live/188-130-141-107.sslip.io/`, продлевает уже работающий таймер certbot |
| Снимок сервера до деплоя | `/root/games-deploy-backup/` (копия `/etc/nginx`, `ss`, `docker ps`, `ufw`, коды сайтов до и после) |

Схема: браузер → nginx `:443` (HTTPS, Basic Auth, HSTS) → `127.0.0.1:8001` → контейнер `:8000`.
Порт 80 отдаёт только редирект на HTTPS. Снаружи порт 8001 закрыт (ufw его не пускает, а Docker
публикует его только на `127.0.0.1`).

Для живого монитора (`/monitor/stream`, SSE) в nginx отдельный `location`:
`proxy_buffering off`, `proxy_read_timeout 1h`.

## Первичная установка (как было сделано)

1. Снимок: `mkdir /root/games-deploy-backup`, копия `/etc/nginx`, вывод `ss -tulnp`, `docker ps -a`,
   `ufw status verbose`, коды ответа существующих сайтов.
2. `git clone https://github.com/Dzuba110729/metacritic-games-tracker.git /opt/metacritic-games-tracker`.
3. Секреты (вводит владелец сервера сам, в чат не попадают):
   ```bash
   cd /opt/metacritic-games-tracker && cp .env.example .env && chmod 600 .env
   read -rsp 'Ключ: ' K && sed -i "s|^ANTHROPIC_API_KEY=.*|ANTHROPIC_API_KEY=$K|" .env && unset K && echo
   printf 'reviewer:%s\n' "$(openssl passwd -apr1)" > /etc/nginx/.htpasswd-metacritic
   chown root:www-data /etc/nginx/.htpasswd-metacritic && chmod 640 /etc/nginx/.htpasswd-metacritic
   ```
   `htpasswd` на сервере не установлен, поэтому хэш пароля делает `openssl passwd -apr1`.
4. `scripts/deploy.sh` — сборка образа и запуск контейнера.
5. nginx: файл `sites-available/metacritic`, симлинк, `nginx -t`, только потом `systemctl reload nginx`.
6. `certbot --nginx -d 188-130-141-107.sslip.io --cert-name 188-130-141-107.sslip.io --redirect`,
   снова `nginx -t` и `reload`.

## Как обновить

```bash
ssh root@188.130.141.107 /opt/metacritic-games-tracker/scripts/deploy.sh
```

Скрипт делает `git pull --ff-only` и
`docker compose -p metacritic -f docker-compose.yml -f docker-compose.prod.yml up -d --build`.
`docker-compose.override.yml` на сервере не подключается: он открыл бы порт 8000 в обход пароля
(и порт 8000 занят CRM).

Сменить пароль сайта — повторить команду `printf ... openssl passwd -apr1 ...` из шага 3, nginx
перечитывает файл сам. Сменить ключ Claude — команда `read ... sed ...` из шага 3, затем
`docker compose -p metacritic -f docker-compose.yml -f docker-compose.prod.yml up -d`.

## Как откатить

```bash
rm /etc/nginx/sites-enabled/metacritic && nginx -t && systemctl reload nginx
cd /opt/metacritic-games-tracker && docker compose -p metacritic -f docker-compose.yml -f docker-compose.prod.yml down
```

`down` удаляет только контейнер и сеть `metacritic_net`. Чтобы убрать всё полностью, дополнительно:
`docker image rm metacritic-app`, `certbot delete --cert-name 188-130-141-107.sslip.io`,
`rm -r /opt/metacritic-games-tracker /etc/nginx/sites-available/metacritic /etc/nginx/.htpasswd-metacritic`.
Исходное состояние `/etc/nginx` лежит в `/root/games-deploy-backup/nginx`.

## Проверки после деплоя (15.10.2026)

| Проверка | Результат |
|----------|-----------|
| Чужие сайты `[скрыто]`, `[скрыто]`, `[скрыто]` | 200, 200, 405 — как до деплоя (405 — обычный ответ CRM на `HEAD`) |
| Порты (`ss -tulnp`) | добавился только `127.0.0.1:8001`; остальное на месте |
| Контейнеры | CRM `[скрыто]` работает, добавился `metacritic-app-1` (healthy) |
| VPN `[скрыто]`, UDP [скрыто] | интерфейс поднят, порт слушается |
| ufw | без изменений |
| `/etc/nginx` против снимка | добавлены только `sites-available/metacritic`, симлинк и `.htpasswd-metacritic` |
| `http://188-130-141-107.sslip.io` | 301 на `https://` |
| `https://188-130-141-107.sslip.io` без пароля | 401, заголовок `Strict-Transport-Security` есть |
| `188.130.141.107:8001` снаружи | не отвечает |
| Главная, карточка, `/monitor`, `/monitor/stream` | 200, SSE отдаёт события |
| Кнопка «Запустить сейчас» | во время прогона: «Прогон уже идёт»; чужой `Origin` → 403 |
| Claude API | резюме пишутся (`POST /v1/messages` 200) |
| YouTube | летсплеи и субтитры находятся, IP сервера не заблокирован |
| Память | контейнер ~380 МБ из лимита 1 ГБ |

## Особенности сервера

- **Старый виртуальный процессор.** `QEMU Virtual CPU` без SSSE3/SSE4. NumPy 2.3+ собран под
  x86-64-v2 и падает при импорте, поэтому в `pyproject.toml` стоит `numpy>=2,<2.3`.
  `onnxruntime` и эмбеддинги на этом процессоре проверены.
- **Защита SSH от частых входов** (`ufw limit 22/tcp`): больше ~6 подключений за 30 секунд —
  временный отказ. Для серии команд держать одно соединение (`ControlMaster`).
- **Модель эмбеддингов** (`BAAI/bge-small-en-v1.5`, ~65 МБ) при первом запуске скачивается
  с Hugging Face в `data/models` — это единственный запрос за пределы списка разрешённых доменов,
  дальше модель берётся из кэша.
- **Диск.** После деплоя свободно ~3.6 ГБ. ~2.5 ГБ занимает кэш сборки Docker. Его чистит
  `docker builder prune`, а prune на общем сервере без согласия владельца не запускаем.
