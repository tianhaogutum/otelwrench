# SPDX-License-Identifier: Apache-2.0
"""Strict JSON input: standard grammar plus rejection of duplicate object keys.

Numbers are decoded as IEEE-754 doubles (like ``JSON.parse``), so ``1`` and
``1.0`` are the same value and out-of-range literals become infinities. The
non-standard ``NaN``/``Infinity`` literals Python's decoder accepts are rejected.
"""
from __future__ import annotations

import json
from typing import Any

from ._js import stringify
from .validation import FlapsimError

_WHITESPACE = " \t\n\r"
_NUMBER_END = set(" \t\n\r,}]")


def _reject_constant(name: str) -> Any:
    raise ValueError(f"Unexpected token '{name[0]}', \"{name}\" is not valid JSON")


def _decode(text: str) -> Any:
    return json.loads(text, parse_int=float, parse_constant=_reject_constant)


def parse_json(text: str, source: str = "JSON") -> Any:
    """Parse ``text``; ``source`` prefixes every error message."""
    try:
        value = _decode(text)
    except RecursionError:
        raise FlapsimError(f"{source}: JSON nesting exceeds supported depth 100") from None
    except json.JSONDecodeError as error:
        raise FlapsimError(f"{source}: invalid JSON: {error.msg} at position {error.pos}") from None
    except ValueError as error:
        raise FlapsimError(f"{source}: invalid JSON: {error}") from None

    # Second pass over the (already valid) text detects duplicate keys, which
    # the decoder would silently resolve to the last value.
    pos = 0
    length = len(text)

    def whitespace() -> None:
        nonlocal pos
        while pos < length and text[pos] in _WHITESPACE:
            pos += 1

    def string() -> str:
        nonlocal pos
        start = pos
        pos += 1
        while pos < length:
            if text[pos] == "\\":
                pos += 2
            else:
                ch = text[pos]
                pos += 1
                if ch == '"':
                    break
        return json.loads(text[start:pos])

    def visit(depth: int) -> None:
        nonlocal pos
        if depth > 100:
            raise FlapsimError(f"{source}: JSON nesting exceeds supported depth 100")
        whitespace()
        ch = text[pos] if pos < length else ""
        if ch == "{":
            pos += 1
            whitespace()
            seen: set[str] = set()
            if pos < length and text[pos] != "}":
                while True:
                    whitespace()
                    key = string()
                    if key in seen:
                        raise FlapsimError(f"{source}: duplicate JSON object key {stringify(key)}")
                    seen.add(key)
                    whitespace()
                    pos += 1  # colon; syntax was checked by the decoder
                    visit(depth + 1)
                    whitespace()
                    if pos >= length or text[pos] != ",":
                        break
                    pos += 1
            pos += 1
        elif ch == "[":
            pos += 1
            whitespace()
            if pos < length and text[pos] != "]":
                while True:
                    visit(depth + 1)
                    whitespace()
                    if pos >= length or text[pos] != ",":
                        break
                    pos += 1
            pos += 1
        elif ch == '"':
            string()
        else:
            while pos < length and text[pos] not in _NUMBER_END:
                pos += 1

    visit(0)
    return value
