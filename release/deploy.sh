#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required." >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "Cannot access the Docker daemon." >&2
  exit 1
fi
if docker compose version >/dev/null 2>&1; then
  compose() { docker compose "$@"; }
elif command -v docker-compose >/dev/null 2>&1; then
  compose() { docker-compose "$@"; }
else
  echo "Docker Compose is required." >&2
  exit 1
fi

if [ ! -x ./verisql-server ]; then
  echo "Missing executable: verisql-server" >&2
  exit 1
fi
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env. Replace all CHANGE_ME/YOUR_* values, then run ./deploy.sh again." >&2
  exit 1
fi
if grep -Eq '=(CHANGE_ME|YOUR_)' .env; then
  echo "Replace all CHANGE_ME and YOUR_* placeholders in .env." >&2
  exit 1
fi

compose config -q
compose pull redis app
compose up -d --wait
compose ps

port=$(sed -n 's/^WEB_PORT=//p' .env | tail -n 1)
echo "VeriSQL is ready at http://SERVER_IP:${port:-8080}"
