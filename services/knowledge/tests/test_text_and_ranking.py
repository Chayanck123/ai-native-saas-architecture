from __future__ import annotations

import itertools
import json

import httpx
import pytest

from knowledge_service.chunking import chunk_text
from knowledge_service.embeddings import HashingEmbedder, VoyageEmbedder, cosine
from knowledge_service.retrieval import (
    BM25,
    feedback_multiplier,
    feedback_quality,
    reciprocal_rank_fusion,
)
from knowledge_service.text import stem, terms
from platform_core.retry import PermanentError, RetryableError

HANDBOOK = """# Billing
## Refund policy
Customers can request a full refund within 30 days of purchase. After 30 days, refunds are
prorated for annual plans only. Refunds are processed to the original payment method.

## Invoices
Invoices are emailed on the first day of each month.
"""


def test_terms_normalise_stem_and_drop_stopwords() -> None:
    assert terms("The Refunds are processed for Policies!") == ["refund", "process", "policy"]
    assert stem("classes") == "class"
    assert stem("data") == "data"


def test_chunks_carry_heading_path_and_respect_budget() -> None:
    chunks = chunk_text(HANDBOOK, target_tokens=30)
    assert chunks[0].heading == "Billing > Refund policy"
    assert chunks[0].text.startswith("Billing > Refund policy: Customers can request")
    assert chunks[-1].heading == "Billing > Invoices"
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    body_budget = 30 + 10  # heading prefix is extra context
    assert all(c.token_count <= body_budget for c in chunks)


def test_chunks_overlap_by_one_sentence() -> None:
    text = " ".join(f"Sentence number {i} talks about topic {i}." for i in range(12))
    chunks = chunk_text(text, target_tokens=25, overlap_sentences=1)
    assert len(chunks) > 2
    for previous, current in itertools.pairwise(chunks):
        last_sentence = previous.text.split(". ")[-1].rstrip(".")
        assert current.text.startswith(last_sentence)


def test_run_on_text_is_split_into_windows() -> None:
    run_on = "word " * 400
    chunks = chunk_text(run_on, target_tokens=50)
    assert len(chunks) >= 7
    assert all(c.token_count <= 55 for c in chunks)


def test_empty_text_produces_no_chunks() -> None:
    assert chunk_text("   \n\n  ") == []


def test_hashing_embeddings_are_deterministic_normalised_and_meaningful() -> None:
    embedder = HashingEmbedder(256)
    a = embedder.embed_one("full refund within 30 days")
    assert a == HashingEmbedder(256).embed_one("full refund within 30 days")
    assert sum(v * v for v in a) == pytest.approx(1.0)
    related = embedder.embed_one("how many days do I have to get a refund")
    unrelated = embedder.embed_one("SAML single sign-on certificate expired")
    assert cosine(a, related) > cosine(a, unrelated)


def test_bm25_prefers_rare_matching_terms() -> None:
    index = BM25(
        {
            "a": terms("error ERR-4521 means the certificate expired"),
            "b": terms("the certificate is valid and the error log is empty"),
            "c": terms("refunds are processed within five days"),
        }
    )
    ranked = index.search(terms("ERR-4521"), limit=3)
    assert [doc for doc, _ in ranked] == ["a"]
    assert index.search(terms("unrelated gibberish"), limit=3) == []


def test_reciprocal_rank_fusion_rewards_agreement() -> None:
    fused = reciprocal_rank_fusion({"dense": ["x", "y", "z"], "lexical": ["y", "w"]}, k=60)
    assert max(fused, key=lambda key: fused[key]) == "y"  # ranked by both methods
    assert fused["y"] == pytest.approx(1 / 62 + 1 / 61)
    assert fused["w"] == pytest.approx(1 / 62)


def test_feedback_multiplier_is_neutral_without_signal_and_bounded() -> None:
    assert feedback_quality(0, 0) == 0.5
    assert feedback_multiplier(0, 0, 0.2) == 1.0
    assert 1.0 < feedback_multiplier(5, 0, 0.2) <= 1.2
    assert 0.8 <= feedback_multiplier(0, 5, 0.2) < 1.0


async def test_voyage_embedder_request_shape_and_error_mapping() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        assert request.headers["authorization"] == "Bearer vk-test"
        if body["input"] == ["overloaded"]:
            return httpx.Response(429, headers={"retry-after": "3"})
        if body["input"] == ["bad"]:
            return httpx.Response(400, json={"detail": "invalid input"})
        data = [{"index": i, "embedding": [float(i), 1.0]} for i in range(len(body["input"]))]
        return httpx.Response(
            200, json={"data": list(reversed(data)), "usage": {"total_tokens": 42}}
        )

    embedder = VoyageEmbedder("vk-test", dimensions=2, transport=httpx.MockTransport(handler))
    batch = await embedder.embed(["first", "second"], input_type="document")
    assert batch.vectors == [[0.0, 1.0], [1.0, 1.0]]  # re-ordered by "index"
    assert batch.tokens == 42
    assert seen[0] == {
        "input": ["first", "second"],
        "model": "voyage-3.5",
        "input_type": "document",
        "output_dimension": 2,
    }
    with pytest.raises(RetryableError) as retryable:
        await embedder.embed(["overloaded"], input_type="query")
    assert retryable.value.retry_after == 3.0
    with pytest.raises(PermanentError):
        await embedder.embed(["bad"], input_type="query")
    await embedder.aclose()
