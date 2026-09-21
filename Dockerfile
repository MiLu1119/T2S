FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY --chown=10001:10001 . .
RUN mkdir -p /app/runtime /app/data/uploads && chown -R 10001:10001 /app/runtime /app/data

USER 10001:10001

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3).read()"

CMD ["uvicorn", "Web_app:app", "--host", "0.0.0.0", "--port", "8080"]
