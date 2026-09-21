#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")/.."

.venv/bin/pyinstaller --noconfirm --clean --onefile \
  --name verisql-server \
  --add-data web:web \
  --add-data schema_metadata.json:. \
  --collect-all uvicorn \
  --collect-all langgraph \
  --collect-all opentelemetry.instrumentation.fastapi \
  --collect-all psycopg \
  --collect-all psycopg_pool \
  --collect-all pymysql \
  --collect-all cryptography \
  --collect-all sqlglot \
  --collect-all openai \
  --collect-all fastembed \
  --collect-all onnxruntime \
  --hidden-import Web_app \
  --hidden-import langgraph.checkpoint.sqlite \
  --hidden-import langgraph.checkpoint.redis \
  release_entry.py

install -m 0755 dist/verisql-server release/verisql-server
chmod 0755 release/deploy.sh
mkdir -p release/models
cp -a models/fastembed release/models/
(cd release && sha256sum verisql-server docker-compose.yaml deploy.sh > SHA256SUMS)
tar -C release -czf dist/verisql-agent-linux-amd64.tar.gz \
  verisql-server docker-compose.yaml .env.example deploy.sh README.txt VERSION SHA256SUMS models
sha256sum dist/verisql-agent-linux-amd64.tar.gz > dist/verisql-agent-linux-amd64.tar.gz.sha256
