VeriSQL linux/amd64 source-free release

Requirements:
- Linux x86_64
- Docker Engine
- Docker Compose plugin (or docker-compose)
- Internet access to pull debian:bookworm-slim and redis:7.4-alpine

Deploy:
1. cp .env.example .env
2. Edit .env and replace every CHANGE_ME / YOUR_* value
3. chmod +x deploy.sh verisql-server
4. ./deploy.sh
5. Open http://SERVER_IP:8080

This package contains the compiled executable, not project Python source files.
Persistent data is stored in Docker named volumes.
Do not run `docker compose down -v` unless permanent data deletion is intended.
