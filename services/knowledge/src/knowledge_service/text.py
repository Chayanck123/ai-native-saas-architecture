"""Text normalisation shared by chunking, lexical search and the offline embedder."""

from __future__ import annotations

import math
import re

_TOKEN = re.compile(r"[a-z0-9]+")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")

STOPWORDS = frozenset(
    """a about above after again against all am an and any are as at be because been before
    being below between both but by can did do does doing down during each few for from
    further had has have having he her here hers herself him himself his how i if in into
    is it its itself just me more most my myself no nor not now of off on once only or
    other our ours ourselves out over own same she should so some such than that the their
    theirs them themselves then there these they this those through to too under until up
    very was we were what when where which while who whom why will with you your yours
    yourself yourselves""".split()  # noqa: SIM905
)


def stem(token: str) -> str:
    """A deliberately tiny suffix stripper: 'refunds' -> 'refund', 'policies' -> 'policy'."""
    if len(token) <= 4 or token.isdigit():
        return token
    for suffix, replacement in (("ies", "y"), ("sses", "ss"), ("ing", ""), ("ed", "")):
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            return token[: -len(suffix)] + replacement
    if token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def terms(text: str) -> list[str]:
    """Lower-case, split on non-alphanumerics, drop stopwords, stem."""
    return [stem(tok) for tok in _TOKEN.findall(text.lower()) if tok not in STOPWORDS]


def split_sentences(paragraph: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_END.split(paragraph.strip()) if s.strip()]


def estimate_tokens(text: str) -> int:
    """Rough LLM token estimate (~4 characters per token for English)."""
    return max(1, math.ceil(len(text) / 4))
