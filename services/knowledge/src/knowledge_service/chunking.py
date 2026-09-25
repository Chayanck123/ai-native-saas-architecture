"""Split documents into retrieval-sized chunks.

Why chunk at all? An embedding summarises its whole input into one vector, so a
50-page manual as one vector matches everything vaguely and nothing precisely.
Small, coherent passages match precisely, and only the relevant ones are handed
to the LLM (cheaper and fewer distractions).

Strategy: respect structure first (Markdown headings, paragraphs), then pack
whole sentences up to a token budget, overlapping one sentence between
neighbours so an idea that straddles a boundary is still retrievable. Each chunk
is prefixed with its heading path for context.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from knowledge_service.text import estimate_tokens, split_sentences

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


@dataclass(frozen=True)
class ChunkDraft:
    ordinal: int
    text: str
    token_count: int
    heading: str | None


def _sections(text: str) -> list[tuple[str | None, list[str]]]:
    """Group paragraphs under their (nested) Markdown heading path."""
    sections: list[tuple[str | None, list[str]]] = []
    path: list[str] = []
    paragraphs: list[str] = []
    lines: list[str] = []

    def end_paragraph() -> None:
        if lines:
            paragraphs.append(" ".join(line.strip() for line in lines))
            lines.clear()

    def end_section() -> None:
        end_paragraph()
        if paragraphs:
            sections.append((" > ".join(path) or None, list(paragraphs)))
            paragraphs.clear()

    for line in text.splitlines():
        heading = _HEADING.match(line.strip())
        if heading:
            end_section()
            del path[len(heading.group(1)) - 1 :]
            path.append(heading.group(2).strip())
        elif line.strip():
            lines.append(line)
        else:
            end_paragraph()
    end_section()
    return sections


def _split_long(sentence: str, target_tokens: int) -> list[str]:
    """Break a run-on "sentence" (e.g. a table row dump) into word windows."""
    if estimate_tokens(sentence) <= target_tokens:
        return [sentence]
    words = sentence.split()
    pieces: list[str] = []
    current: list[str] = []
    for word in words:
        if current and estimate_tokens(" ".join([*current, word])) > target_tokens:
            pieces.append(" ".join(current))
            current = []
        current.append(word)
    if current:
        pieces.append(" ".join(current))
    return pieces


def _overlap(window: list[str], sentences: int, max_tokens: int) -> list[str]:
    """Trailing sentences to repeat in the next chunk, capped at ``max_tokens``."""
    carried: list[str] = []
    for sentence in reversed(window[-sentences:] if sentences else []):
        if sum(estimate_tokens(s) for s in carried) + estimate_tokens(sentence) > max_tokens:
            break
        carried.insert(0, sentence)
    return carried


def chunk_text(
    text: str, *, target_tokens: int = 180, overlap_sentences: int = 1
) -> list[ChunkDraft]:
    if target_tokens < 20:
        raise ValueError("target_tokens must be at least 20")
    drafts: list[ChunkDraft] = []

    def emit(heading: str | None, sentences: list[str]) -> None:
        body = " ".join(sentences)
        full = f"{heading}: {body}" if heading else body
        drafts.append(ChunkDraft(len(drafts), full, estimate_tokens(full), heading))

    for heading, paragraphs in _sections(text):
        sentences = [
            piece
            for paragraph in paragraphs
            for sentence in (split_sentences(paragraph) or [paragraph])
            for piece in _split_long(sentence, target_tokens)
        ]
        window: list[str] = []
        window_tokens = 0
        fresh = 0  # sentences in the window not yet emitted in any chunk
        for sentence in sentences:
            tokens = estimate_tokens(sentence)
            if fresh and window_tokens + tokens > target_tokens:
                emit(heading, window)
                window = _overlap(window, overlap_sentences, target_tokens // 2)
                window_tokens = sum(estimate_tokens(s) for s in window)
                fresh = 0
            window.append(sentence)
            window_tokens += tokens
            fresh += 1
        if fresh:
            emit(heading, window)
    return drafts
