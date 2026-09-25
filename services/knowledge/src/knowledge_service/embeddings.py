"""Vector embeddings: text in, fixed-length list of numbers out.

Texts with similar meaning land close together in vector space, so "find
passages relevant to this question" becomes "find the nearest vectors".

Two interchangeable providers:

* :class:`HashingEmbedder` - deterministic, offline, free. It hashes words,
  word pairs and character trigrams into a fixed number of buckets (the "hashing
  trick"). It captures lexical overlap rather than deep meaning, which is enough
  for local development, tests and demos with no API key.
* :class:`VoyageEmbedder` - semantic embeddings from Voyage AI (the embedding
  provider Anthropic recommends), for production.
"""

from __future__ import annotations

import hashlib
import itertools
import math
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx

from knowledge_service.text import estimate_tokens, terms
from platform_core.retry import PermanentError, RetryableError

InputType = Literal["document", "query"]


@dataclass(frozen=True)
class EmbeddingBatch:
    vectors: list[list[float]]
    tokens: int
    model: str


class Embedder(Protocol):
    @property
    def dimensions(self) -> int: ...

    @property
    def model(self) -> str: ...

    async def embed(self, texts: list[str], *, input_type: InputType) -> EmbeddingBatch: ...


class HashingEmbedder:
    def __init__(self, dimensions: int = 384) -> None:
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def model(self) -> str:
        return f"hashing-v1-{self._dimensions}"

    def _bucket(self, feature: str) -> tuple[int, float]:
        digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "big") % self._dimensions
        sign = 1.0 if digest[4] & 1 else -1.0  # signed hashing cancels collision bias
        return index, sign

    def embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self._dimensions
        words = terms(text)
        features: list[tuple[str, float]] = [(f"w:{w}", 1.0) for w in words]
        features += [(f"b:{a}_{b}", 0.6) for a, b in itertools.pairwise(words)]
        for word in words:
            padded = f"#{word}#"
            features += [(f"c:{padded[i : i + 3]}", 0.2) for i in range(len(padded) - 2)]
        for feature, weight in features:
            index, sign = self._bucket(feature)
            vector[index] += sign * weight
        norm = math.sqrt(sum(v * v for v in vector))
        return [v / norm for v in vector] if norm else vector

    async def embed(self, texts: list[str], *, input_type: InputType) -> EmbeddingBatch:
        return EmbeddingBatch(
            vectors=[self.embed_one(t) for t in texts],
            tokens=sum(estimate_tokens(t) for t in texts),
            model=self.model,
        )


class VoyageEmbedder:
    """Voyage AI embeddings over its REST API (``POST /v1/embeddings``)."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "voyage-3.5",
        dimensions: int = 1024,
        base_url: str = "https://api.voyageai.com/v1",
        batch_size: int = 128,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._model = model
        self._dimensions = dimensions
        self._batch_size = batch_size
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def model(self) -> str:
        return self._model

    async def embed(self, texts: list[str], *, input_type: InputType) -> EmbeddingBatch:
        vectors: list[list[float]] = []
        tokens = 0
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            body: dict[str, Any] = {
                "input": batch,
                "model": self._model,
                "input_type": input_type,
                "output_dimension": self._dimensions,
            }
            try:
                response = await self._client.post("/embeddings", json=body)
            except httpx.TransportError as exc:
                raise RetryableError(f"voyage transport error: {exc}") from exc
            if response.status_code == 429 or response.status_code >= 500:
                retry_after = response.headers.get("retry-after")
                raise RetryableError(
                    f"voyage returned {response.status_code}",
                    retry_after=float(retry_after) if retry_after else None,
                )
            if response.status_code >= 400:
                raise PermanentError(f"voyage rejected request: {response.text[:300]}")
            payload = response.json()
            data = sorted(payload["data"], key=lambda item: item["index"])
            vectors.extend([float(x) for x in item["embedding"]] for item in data)
            tokens += int(payload.get("usage", {}).get("total_tokens", 0))
        return EmbeddingBatch(vectors=vectors, tokens=tokens, model=self._model)

    async def aclose(self) -> None:
        await self._client.aclose()


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0
