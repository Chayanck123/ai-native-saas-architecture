"""Identifier helpers.

IDs are prefixed (``doc_...``, ``run_...``) so they are self-describing in logs,
URLs and support tickets, and time-ordered so that database indexes on them stay
append-friendly.
"""

from __future__ import annotations

import os
import time

_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


def _base36(number: int, width: int) -> str:
    chars = []
    while number:
        number, rem = divmod(number, 36)
        chars.append(_ALPHABET[rem])
    return "".join(reversed(chars)).rjust(width, "0")


def new_id(prefix: str = "") -> str:
    """Return a sortable, collision-resistant identifier such as ``doc_0lzx5m2k9c...``.

    48 bits of millisecond time followed by 80 random bits (like a ULID), base36 encoded.
    """
    millis = int(time.time() * 1000) & ((1 << 48) - 1)
    rand = int.from_bytes(os.urandom(10), "big")
    body = _base36(millis, 10) + _base36(rand, 16)
    return f"{prefix}_{body}" if prefix else body
