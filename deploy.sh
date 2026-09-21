#!/usr/bin/env sh
set -eu

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is not installed. Install Docker Engine and the Compose plugin first." >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "Cannot access the Docker daemon. Start Docker and grant the current user Docker access." >&2
  exit 1
fi

if ! docker buildx version >/dev/null 2>&1; then
  echo "Docker Buildx is not installed. Install the official Docker Buildx plugin." >&2
  exit 1
fi

if docker compose version >/dev/null 2>&1; then
  compose() { docker compose "$@"; }
elif command -v docker-compose >/dev/null 2>&1; then
  compose() { docker-compose "$@"; }
else
  echo "Docker Compose is not installed. Install the Compose plugin first." >&2
  exit 1
fi

if [ ! -f .env ]; then
  cp .env.docker.example .env
  echo "Created .env from .env.docker.example. Replace every CHANGE_ME/YOUR_* value, then run ./deploy.sh again." >&2
  exit 1
fi

if grep -Eq '=(CHANGE_ME|YOUR_)' .env; then
  echo "Refusing to deploy: replace every CHANGE_ME and YOUR_* placeholder in .env." >&2
  exit 1
fi

compose config -q
compose up -d --build --wait
compose ps

port=$(sed -n 's/^WEB_PORT=//p' .env | tail -n 1)
port=${port:-8080}
echo "VeriSQL is ready at http://SERVER_IP:${port}"
