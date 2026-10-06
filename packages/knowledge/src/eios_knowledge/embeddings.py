"""Embedding providers. The default is a deterministic, dependency-free local feature hasher.

Local-first: no model download, no network, reproducible in tests. It captures lexical overlap
(including identifier parts and bigrams), not deep semantics; a neural embedder can be plugged in
behind :class:`EmbeddingProvider` (see ADR 0007) without touching callers.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from itertools import pairwise
from typing import Protocol

from eios_knowledge.text import tokenize
from eios_storage.tables._common import EMBEDDING_DIM


class EmbeddingProvider(Protocol):
    @property
    def model_id(self) -> str: ...

    @property
    def dim(self) -> int: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbedder:
    """Signed feature hashing of unigrams and bigrams, sub-linear tf, L2-normalised."""

    model_id = "hashing-v1"

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    def embed_one(self, text: str) -> list[float]:
        tokens = tokenize(text)
        features = Counter(tokens)
        features.update(f"{a} {b}" for a, b in pairwise(tokens))
        vector = [0.0] * self._dim
        for feature, count in features.items():
            digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
            value = int.from_bytes(digest, "big")
            index = value % self._dim
            sign = 1.0 if (value >> 63) & 1 else -1.0
            vector[index] += sign * (1.0 + math.log(count))
        norm = math.sqrt(sum(v * v for v in vector))
        return [v / norm for v in vector] if norm else vector

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_one(t) for t in texts]
