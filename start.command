#!/bin/bash
set -e
cd "$(dirname "$0")"
if ! command -v docker >/dev/null 2>&1; then
  echo "Docker не найден. Установите Docker Desktop и запустите этот файл снова."
  exit 1
fi
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Создан .env. Перед публикацией поменяйте SECRET_KEY и ADMIN_PASSWORD."
fi
docker compose up --build
