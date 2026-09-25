"""Ranking maths for hybrid search.

* **Dense retrieval** (vectors) finds passages that *mean* the same thing, even
  with different words ("reimbursement" ~ "refund").
* **Lexical retrieval** (BM25) finds passages containing the *exact* words -
  vital for product names, error codes and IDs that embeddings blur.
* **Reciprocal Rank Fusion** merges the two ranked lists without having to
  calibrate their incomparable scores::

      RRF(d) = sum over methods m of 1 / (k + rank_m(d)),   k = 60

* A **feedback multiplier** nudges passages that users rated helpful - the data
  flywheel that makes retrieval better the more the product is used.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence


class BM25:
    """Okapi BM25 over pre-tokenised documents."""

    def __init__(self, documents: Mapping[str, Sequence[str]], *, k1: float = 1.5, b: float = 0.75):
        self._k1 = k1
        self._b = b
        self._tf = {doc_id: Counter(tokens) for doc_id, tokens in documents.items()}
        self._length = {doc_id: len(tokens) for doc_id, tokens in documents.items()}
        self._n = len(documents)
        self._avgdl = (sum(self._length.values()) / self._n) if self._n else 0.0
        self._df: Counter[str] = Counter()
        for tf in self._tf.values():
            self._df.update(tf.keys())

    def idf(self, term: str) -> float:
        df = self._df.get(term, 0)
        return math.log(1.0 + (self._n - df + 0.5) / (df + 0.5))

    def score(self, query_terms: Sequence[str], doc_id: str) -> float:
        tf = self._tf[doc_id]
        length_norm = 1 - self._b + self._b * (self._length[doc_id] / (self._avgdl or 1.0))
        total = 0.0
        for term in set(query_terms):
            freq = tf.get(term, 0)
            if freq:
                total += self.idf(term) * freq * (self._k1 + 1) / (freq + self._k1 * length_norm)
        return total

    def search(self, query_terms: Sequence[str], limit: int) -> list[tuple[str, float]]:
        scored = [(doc_id, self.score(query_terms, doc_id)) for doc_id in self._tf]
        ranked = sorted((item for item in scored if item[1] > 0), key=lambda i: -i[1])
        return ranked[:limit]


def reciprocal_rank_fusion(
    rankings: Mapping[str, Sequence[str]], *, k: int = 60
) -> dict[str, float]:
    scores: dict[str, float] = defaultdict(float)
    for ranked_ids in rankings.values():
        for rank, item_id in enumerate(ranked_ids, start=1):
            scores[item_id] += 1.0 / (k + rank)
    return dict(scores)


def feedback_quality(upvotes: int, downvotes: int) -> float:
    """Laplace-smoothed helpfulness in (0, 1); 0.5 means "no signal yet"."""
    return (upvotes + 1) / (upvotes + downvotes + 2)


def feedback_multiplier(upvotes: int, downvotes: int, weight: float) -> float:
    """Scale in [1 - weight, 1 + weight]; neutral (1.0) without feedback."""
    return 1.0 + weight * (2.0 * feedback_quality(upvotes, downvotes) - 1.0)
