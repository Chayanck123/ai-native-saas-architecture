"""Small text helpers shared by the offline model and the critic."""

from __future__ import annotations

import re

_WORD = re.compile(r"[a-z0-9]+")
_CITATION = re.compile(r"\s*\[\d+\]")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")

_STOP = frozenset(
    """a an and are as at be by can do does for from has have how i if in is it its of on or
    our so that the their there this to was we what when where which who why will with you
    your about into than then them they these those not no yes please""".split()  # noqa: SIM905
)


def content_terms(text: str) -> list[str]:
    words = (w for w in _WORD.findall(text.lower()) if w not in _STOP)
    return [w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w for w in words]


def strip_citations(text: str) -> str:
    return _CITATION.sub("", text)


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.split(text.strip()) if s.strip()]


def cited_numbers(text: str) -> set[int]:
    return {int(n) for n in re.findall(r"\[(\d+)\]", text)}
