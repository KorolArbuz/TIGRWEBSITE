#!/bin/bash
set -e
cd "$(dirname "$0")"
if ! command -v docker >/dev/null 2>&1; then
  echo "Docker не найден. Установите Docker Desktop и запустите этот файл снова."
  exit 1
fi
if [ ! -f .env ]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install --require-hashes -r requirements.txt
  .venv/bin/python scripts/setup_security.py --development
fi
docker compose up --build
