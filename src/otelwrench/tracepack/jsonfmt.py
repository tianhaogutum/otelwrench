# SPDX-License-Identifier: Apache-2.0
"""Precision-safe JSON parsing and JavaScript-compatible JSON serialization.

The parser keeps integer tokens exact (safe integers become ``int``, larger ones
become :class:`JSBigInt`) and rejects duplicate keys. Decimal/exponent tokens keep
their spelling in a :class:`DecimalToken` until the field's declared type is known:
an integer timestamp needs exact arithmetic; a double follows IEEE-754 decoding.

The serializers reproduce the byte output of the original Node.js implementation
(``JSON.stringify(value, replacer, 2)`` and tracepack canonical JSON v1), including
JavaScript number formatting, ``-0``, lone-surrogate escaping, the integer-like key
ordering of JavaScript objects and UTF-16 code-unit key sorting.
"""
from __future__ import annotations

import json as _stdjson
import math
import re
from collections.abc import Mapping
from typing import Any

from .errors import JSONSyntaxError

MAX_SAFE_INTEGER = 2**53 - 1
MIN_SAFE_INTEGER = -(2**53 - 1)


class _Undefined:
    """JavaScript ``undefined``: an option value that means "use the default"."""

    __slots__ = ()
    _instance: _Undefined | None = None

    def __new__(cls) -> _Undefined:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNDEFINED"

    def __bool__(self) -> bool:
        return False


UNDEFINED = _Undefined()


class JSBigInt(int):
    """An integer JSON token outside the JavaScript safe-integer range (a ``BigInt``)."""

    __slots__ = ()

    def __repr__(self) -> str:
        return f"JSBigInt({int.__repr__(self)})"

    def __str__(self) -> str:
        return int.__repr__(self)


def big_int(digits: str) -> int:
    """``int(digits)`` without Python's 4300-digit conversion limit."""
    if len(digits) <= 4000:
        return int(digits)
    negative = digits.startswith("-")
    body = digits[1:] if negative else digits
    value = 0
    for start in range(0, len(body), 4000):
        chunk = body[start:start + 4000]
        value = value * 10 ** len(chunk) + int(chunk)
    return -value if negative else value


class DecimalToken:
    """A decimal or exponent JSON number token whose original spelling is retained."""

    __slots__ = ("raw", "number", "integer")

    def __init__(self, raw: str):
        self.raw = raw
        number = float(raw)
        if not math.isfinite(number):
            raise JSONSyntaxError("Nonfinite numeric token")
        self.number = number
        lowered = raw.lower()
        mantissa, _, exponent = lowered.partition("e")
        exponent = exponent or "0"
        digits = mantissa.replace("-", "").replace(".", "")
        fraction_digits = len(mantissa) - mantissa.index(".") - 1 if "." in mantissa else 0
        trailing_zeros = len(digits) - len(digits.rstrip("0"))
        shift = int(exponent) - fraction_digits
        self.integer: int | None
        if not re.search("[1-9]", digits):
            self.integer = 0
        elif shift >= -trailing_zeros:
            integral = digits + "0" * shift if shift >= 0 else digits[: len(digits) + shift]
            self.integer = big_int(("-" if raw.startswith("-") else "") + integral)
        else:
            self.integer = None

    def __repr__(self) -> str:
        return f"DecimalToken({self.raw!r})"


_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_INTEGER = re.compile(r"-?[0-9]+")
_STRING = re.compile(r'"(?:[^"\\]|\\[\s\S])*"')
_SURROGATE = re.compile("[\ud800-\udfff]")


def utf16_len(text: str) -> int:
    """JavaScript ``string.length`` (UTF-16 code units)."""
    if text.isascii():
        return len(text)
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def u16(text: str) -> bytes:
    """Sort key matching JavaScript's UTF-16 code-unit string ordering."""
    return text.encode("utf-16-be", "surrogatepass")


def _string_error(token: str, error: ValueError) -> str:
    message = getattr(error, "msg", str(error))
    position = getattr(error, "pos", 0)
    if message.startswith("Invalid \\uXXXX"):
        message = "Bad Unicode escape"
    elif message.startswith("Invalid \\escape"):
        message = "Bad escaped character"
    elif message.startswith("Invalid control character"):
        message = "Bad control character in string literal"
    return f"{message} in JSON at position {utf16_len(token[:position])}"


def parse_json(text: str) -> Any:
    """Parse one JSON document, rejecting duplicate keys and nesting beyond 128 levels."""
    if not isinstance(text, str):
        raise TypeError("parse_json expects str")
    n = len(text)
    i = 0

    def fail(message: str) -> None:
        raise JSONSyntaxError(f"{message} at JSON offset {utf16_len(text[:i])}")

    def ws() -> None:
        nonlocal i
        while i < n and text[i] in " \t\n\r":
            i += 1

    def string() -> str:
        nonlocal i
        match = _STRING.match(text, i)
        if match is None:
            # Mirror the original scanner's final offset for the error message.
            i += 1
            while i < n:
                c = text[i]
                i += 1
                if c == "\\":
                    i += 1
            fail("Unterminated string")
        token = match.group(0)
        i = match.end()
        try:
            return _stdjson.loads(token)
        except ValueError as error:
            raise JSONSyntaxError(_string_error(token, error)) from None

    def value(depth: int) -> Any:
        nonlocal i
        if depth > 128:
            fail("Maximum JSON nesting (128) exceeded")
        ws()
        c = text[i] if i < n else ""
        if c == '"':
            return string()
        if c == "{":
            i += 1
            ws()
            out: dict[str, Any] = {}
            if i < n and text[i] == "}":
                i += 1
                return out
            while True:
                ws()
                if not (i < n and text[i] == '"'):
                    fail("Expected object key")
                key = string()
                ws()
                if key in out:
                    fail(f"Duplicate object key {js_quote(key)}")
                colon = text[i] if i < n else ""
                i += 1
                if colon != ":":
                    fail("Expected colon")
                out[key] = value(depth + 1)
                ws()
                end = text[i] if i < n else ""
                i += 1
                if end == "}":
                    return out
                if end != ",":
                    fail("Expected comma or closing brace")
        if c == "[":
            i += 1
            ws()
            items: list[Any] = []
            if i < n and text[i] == "]":
                i += 1
                return items
            while True:
                items.append(value(depth + 1))
                ws()
                end = text[i] if i < n else ""
                i += 1
                if end == "]":
                    return items
                if end != ",":
                    fail("Expected comma or closing bracket")
        for literal, result in (("true", True), ("false", False), ("null", None)):
            if text.startswith(literal, i):
                i += len(literal)
                return result
        match = _NUMBER.match(text, i)
        if match is None:
            fail("Expected JSON value")
        token = match.group(0)
        i = match.end()
        if _INTEGER.fullmatch(token):
            number = big_int(token)
            if MIN_SAFE_INTEGER <= number <= MAX_SAFE_INTEGER:
                # Number("-0") is negative zero in JavaScript.
                return -0.0 if token == "-0" else number
            return JSBigInt(number)
        return DecimalToken(token)

    out = value(0)
    ws()
    if i != n:
        fail("Trailing content")
    return out


def js_number(value: float) -> str:
    """JavaScript ``Number.prototype.toString()`` for a finite float."""
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    text = repr(abs(value))
    mantissa, _, exp = text.partition("e")
    exponent = int(exp) if exp else 0
    integral, _, fraction = mantissa.partition(".")
    all_digits = integral + fraction
    stripped = all_digits.lstrip("0")
    point = len(integral) + exponent - (len(all_digits) - len(stripped))
    digits = stripped.rstrip("0")
    k = len(digits)
    if k <= point <= 21:
        body = digits + "0" * (point - k)
    elif 0 < point <= 21:
        body = digits[:point] + "." + digits[point:]
    elif -6 < point <= 0:
        body = "0." + "0" * (-point) + digits
    else:
        e = point - 1
        exp_text = ("+" if e >= 0 else "-") + str(abs(e))
        body = (digits if k == 1 else digits[0] + "." + digits[1:]) + "e" + exp_text
    return sign + body


def js_quote(text: str) -> str:
    """JavaScript ``JSON.stringify(string)``, including lone-surrogate escapes."""
    if _SURROGATE.search(text):
        # Pair adjacent surrogates as JavaScript would, then escape lone ones.
        text = text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "surrogatepass")
        encoded = _stdjson.dumps(text, ensure_ascii=False)
        return _SURROGATE.sub(lambda m: "\\u%04x" % ord(m.group(0)), encoded)
    return _stdjson.dumps(text, ensure_ascii=False)


_ARRAY_INDEX = re.compile(r"0|[1-9][0-9]*")


def js_keys(obj: Mapping[Any, Any]) -> list[Any]:
    """``Object.keys`` order: array-index keys ascending first, then insertion order."""
    indexes = []
    others = []
    for key in obj:
        if isinstance(key, str) and _ARRAY_INDEX.fullmatch(key) and len(key) <= 10 and int(key) < 2**32 - 1:
            indexes.append(key)
        else:
            others.append(key)
    if not indexes:
        return others
    indexes.sort(key=int)
    return indexes + others


def _scalar(value: Any) -> str | None:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return int.__repr__(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return "null"
        if value == 0 and math.copysign(1.0, value) < 0:
            return "-0"
        return js_number(value)
    if isinstance(value, DecimalToken):
        return value.raw
    if isinstance(value, str):
        return js_quote(value)
    return None


def _pretty(value: Any, indent: str) -> str | None:
    scalar = _scalar(value)
    if scalar is not None:
        return scalar
    if isinstance(value, (list, tuple)):
        if not value:
            return "[]"
        inner = indent + "  "
        items = [_pretty(item, inner) for item in value]
        return "[\n" + ",\n".join(inner + ("null" if item is None else item) for item in items) + "\n" + indent + "]"
    if isinstance(value, Mapping):
        inner = indent + "  "
        members = []
        for key in js_keys(value):
            encoded = _pretty(value[key], inner)
            if encoded is not None:
                members.append(inner + js_quote(str(key)) + ": " + encoded)
        if not members:
            return "{}"
        return "{\n" + ",\n".join(members) + "\n" + indent + "}"
    return None


def stringify(value: Any) -> str:
    """``JSON.stringify(value, null, 2) + "\\n"`` with exact integers and ``-0``."""
    encoded = _pretty(value, "")
    return ("undefined" if encoded is None else encoded) + "\n"


def canonical_json(value: Any) -> str:
    """tracepack canonical JSON v1.

    Sorted UTF-16 object keys, array order retained, compact finite IEEE-754 numbers
    (including ``-0``) and exact integer tokens. This is a local format contract, not
    RFC 8785/JCS (which erases negative zero).
    """
    if isinstance(value, DecimalToken):
        return canonical_json(value.number)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical_json(item) for item in value) + "]"
    if isinstance(value, Mapping):
        keys = sorted(value.keys(), key=lambda k: u16(str(k)))
        return "{" + ",".join(js_quote(str(key)) + ":" + canonical_json(value[key]) for key in keys) + "}"
    if isinstance(value, float) and not math.isfinite(value):
        raise TypeError("canonical JSON requires finite numbers")
    encoded = _scalar(value)
    if encoded is None:
        raise TypeError("unsupported canonical JSON value")
    return encoded
