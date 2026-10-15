#!/usr/bin/env bash
# Обновление сервиса на сервере: свежий код из git и пересборка контейнера.
# Запуск: /opt/metacritic-games-tracker/scripts/deploy.sh
set -euo pipefail

cd "$(dirname "$0")/.."

git pull --ff-only
# Папка базы и кэша модели; в контейнере приложение работает от uid 1000.
mkdir -p data
chown 1000:1000 data
docker compose -p metacritic -f docker-compose.yml -f docker-compose.prod.yml up -d --build
docker compose -p metacritic -f docker-compose.yml -f docker-compose.prod.yml ps
