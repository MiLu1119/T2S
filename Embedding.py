"""Pluggable local embedding providers for semantic Schema retrieval."""

from __future__ import annotations

from pathlib import Path
from threading import Lock
from typing import Iterable

import numpy as np


class FastEmbedProvider:
    def __init__(self, model_name: str, cache_dir: str | Path, threads: int = 4):
        from fastembed import TextEmbedding
        self.model_name = model_name
        self.model = TextEmbedding(
            model_name=model_name, cache_dir=str(cache_dir), threads=max(1, threads)
        )
        self._lock = Lock()

    def embed(self, texts: Iterable[str]) -> np.ndarray:
        values = list(texts)
        if not values:
            return np.empty((0, 0), dtype=np.float32)
        with self._lock:
            matrix = np.asarray(list(self.model.embed(values, batch_size=64)), dtype=np.float32)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        return matrix / np.maximum(norms, 1e-12)

