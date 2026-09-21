"""Entry point for the source-free PyInstaller release."""

from __future__ import annotations

import os

import uvicorn


if __name__ == "__main__":
    uvicorn.run(
        "Web_app:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
        proxy_headers=False,
        log_level=os.getenv("LOG_LEVEL", "info"),
    )
