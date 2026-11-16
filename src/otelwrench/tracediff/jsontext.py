# SPDX-License-Identifier: Apache-2.0
"""Strict JSON input and JavaScript-compatible JSON output.

Reports are byte-compatible with the original JavaScript implementation, so this
module reproduces the relevant ECMAScript rules instead of relying on Python's
``json`` defaults:

* ``js_number`` is ``Number.prototype.toString`` (``100.0`` -> ``100``,
  ``1e-7`` -> ``1e-7``, ``1e21`` -> ``1e+21``, ``-0`` -> ``0``).
* ``stringify`` is ``JSON.stringify`` (two-space indent or compact, lone UTF-16
  surrogates escaped, non-finite numbers as ``null``).
* ``js_sorted`` orders strings by UTF-16 code units like JavaScript's default
  ``Array.prototype.sort`` and the ``<`` operator, not by code points.
* ``js_keys`` lists object keys in ECMAScript property order (array-index keys
  first, ascending), which decides which invalid key is reported first.
* ``parse_json`` rejects duplicate keys and reports UTF-16 offsets.
"""
from __future__ import annotations

from json.encoder import encode_basestring
import json
import math
import re

MAX_SAFE_INTEGER = 9007199254740991


class _Undefined:
    """Marker for a missing property (JavaScript ``undefined``), distinct from ``None`` (``null``)."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNDEFINED"

    def __bool__(self) -> bool:
        return False


UNDEFINED = _Undefined()


class InputError(Exception):
    """Malformed or unsupported input, configuration or usage (exit code 2)."""

    @property
    def message(self) -> str:
        return str(self.args[0]) if self.args else ""


# ---------------------------------------------------------------------------
# Number semantics

def is_number(value) -> bool:
    """``typeof value === 'number'`` (booleans are not numbers)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_finite(value) -> bool:
    """``typeof value === 'number' && Number.isFinite(value)``."""
    if not is_number(value):
        return False
    return True if isinstance(value, int) else math.isfinite(value)


def is_integer(value) -> bool:
    """``Number.isInteger(value)``."""
    if not is_number(value):
        return False
    return True if isinstance(value, int) else (math.isfinite(value) and value.is_integer())


def is_safe_integer(value) -> bool:
    """``Number.isSafeInteger(value)``."""
    return is_integer(value) and abs(value) <= MAX_SAFE_INTEGER


def js_number(value) -> str:
    """``String(number)`` for a JavaScript number (shortest round-trip digits)."""
    if isinstance(value, int) and not isinstance(value, bool):
        if abs(value) <= 2 ** 53:
            return str(value)
        value = float(value)
    value = float(value)
    if value != value:
        return "NaN"
    if value == math.inf:
        return "Infinity"
    if value == -math.inf:
        return "-Infinity"
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    mantissa, _, exponent = repr(abs(value)).partition("e")
    whole, _, fraction = mantissa.partition(".")
    digits = whole + fraction
    point = len(whole) + (int(exponent) if exponent else 0)
    stripped = digits.lstrip("0")
    point -= len(digits) - len(stripped)
    digits = stripped.rstrip("0")
    k, n = len(digits), point
    if k <= n <= 21:
        text = digits + "0" * (n - k)
    elif 0 < n <= 21:
        text = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        text = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        text = digits[0] + ("." + digits[1:] if k > 1 else "") + "e" + ("+" if e >= 0 else "-") + str(abs(e))
    return sign + text


def js_str(value) -> str:
    """Template-literal conversion (``${value}``) for the scalar values used in text reports."""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if value is UNDEFINED:
        return "undefined"
    if is_number(value):
        return js_number(value)
    return str(value)


def js_round(value) -> int:
    """``Math.round``: halves round toward positive infinity."""
    return math.floor(value + 0.5)


# ---------------------------------------------------------------------------
# Strings and ordering

_SURROGATE = re.compile("[\ud800-\udfff]")
_ASTRAL = re.compile("[\U00010000-\U0010ffff]")


def _utf16(text: str) -> bytes:
    return text.encode("utf-16-be", "surrogatepass")


def utf16_length(text: str) -> int:
    """``string.length`` (UTF-16 code units)."""
    if text.isascii():
        return len(text)
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def js_sorted(items, key=None) -> list:
    """Stable sort by UTF-16 code-unit order of ``key(item)`` (default: the item itself)."""
    items = list(items)
    keys = [item if key is None else key(item) for item in items]
    if any(not k.isascii() and _ASTRAL.search(k) for k in keys):
        keys = [_utf16(k) for k in keys]
    order = sorted(range(len(items)), key=keys.__getitem__)
    return [items[i] for i in order]


_INDEX_KEY = re.compile(r"0|[1-9][0-9]*")


def js_keys(obj: dict) -> list:
    """``Object.keys`` order: canonical array-index keys ascending, then insertion order."""
    index = [k for k in obj if _INDEX_KEY.fullmatch(k) and int(k) < 4294967295]
    if not index:
        return list(obj)
    rest = [k for k in obj if not (_INDEX_KEY.fullmatch(k) and int(k) < 4294967295)]
    return sorted(index, key=int) + rest


def quote(text: str) -> str:
    """``JSON.stringify(string)``: escapes like ECMAScript, including lone surrogates."""
    out = encode_basestring(text)
    if not text.isascii() and _SURROGATE.search(out):
        out = _SURROGATE.sub(lambda m: "\\u%04x" % ord(m.group()), out)
    return out


# ---------------------------------------------------------------------------
# Output

def stringify(value, indent: int | None = None) -> str:
    """``JSON.stringify(value)`` or ``JSON.stringify(value, null, indent)``."""
    parts: list[str] = []
    step = " " * indent if indent else ""
    _emit(value, parts, step, "")
    return "".join(parts)


def _emit(value, parts: list, step: str, current: str) -> None:
    if value is None:
        parts.append("null")
    elif value is True:
        parts.append("true")
    elif value is False:
        parts.append("false")
    elif isinstance(value, str):
        parts.append(quote(value))
    elif isinstance(value, (int, float)):
        parts.append(js_number(value) if is_finite(value) else "null")
    elif isinstance(value, dict):
        items = [(k, v) for k, v in value.items() if v is not UNDEFINED]
        if not items:
            parts.append("{}")
            return
        if not step:
            parts.append("{")
            for i, (k, v) in enumerate(items):
                if i:
                    parts.append(",")
                parts.append(quote(k))
                parts.append(":")
                _emit(v, parts, step, current)
            parts.append("}")
            return
        inner = current + step
        parts.append("{\n")
        for i, (k, v) in enumerate(items):
            if i:
                parts.append(",\n")
            parts.append(inner)
            parts.append(quote(k))
            parts.append(": ")
            _emit(v, parts, step, inner)
        parts.append("\n" + current + "}")
    elif isinstance(value, (list, tuple)):
        if not value:
            parts.append("[]")
            return
        if not step:
            parts.append("[")
            for i, v in enumerate(value):
                if i:
                    parts.append(",")
                _emit(None if v is UNDEFINED else v, parts, step, current)
            parts.append("]")
            return
        inner = current + step
        parts.append("[\n")
        for i, v in enumerate(value):
            if i:
                parts.append(",\n")
            parts.append(inner)
            _emit(None if v is UNDEFINED else v, parts, step, inner)
        parts.append("\n" + current + "]")
    else:
        raise TypeError(f"cannot serialize {type(value).__name__}")


def canonical(value) -> str:
    """Key-sorted compact JSON used for fingerprints and canonical ordering."""
    parts: list[str] = []
    _canonical(value, parts)
    return "".join(parts)


def _canonical(value, parts: list) -> None:
    if isinstance(value, list):
        parts.append("[")
        for i, v in enumerate(value):
            if i:
                parts.append(",")
            _canonical(v, parts)
        parts.append("]")
    elif isinstance(value, dict):
        parts.append("{")
        for i, k in enumerate(js_sorted(value)):
            if i:
                parts.append(",")
            parts.append(quote(k))
            parts.append(":")
            _canonical(value[k], parts)
        parts.append("}")
    elif value is UNDEFINED:
        parts.append("undefined")
    else:
        _emit(value, parts, "", "")


# ---------------------------------------------------------------------------
# Strict input

_WS = re.compile(r"[\t\r\n ]*")
_STRING_BODY = re.compile(r'[^"\\]*')
_LITERAL = re.compile(r"true|false|null|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")


def _number(token: str):
    parsed = float(token)
    if math.isfinite(parsed) and parsed.is_integer() and abs(parsed) <= MAX_SAFE_INTEGER:
        return int(parsed)
    return parsed


def parse_json(text: str, label: str = "JSON"):
    """Parse strict JSON, rejecting duplicate keys, nesting over 256 and non-finite numbers."""
    return _Parser(text, label).parse()


class _Parser:
    def __init__(self, text: str, label: str):
        self.text, self.label, self.i, self.n = text, label, 0, len(text)

    def fail(self, message: str):
        # JavaScript string offsets count UTF-16 code units.
        prefix = self.text[:self.i]
        offset = self.i if prefix.isascii() else self.i + len(_ASTRAL.findall(prefix))
        raise InputError(f"{self.label}: {message} at offset {offset}")

    def char(self) -> str:
        return self.text[self.i] if self.i < self.n else ""

    def ws(self) -> None:
        self.i = _WS.match(self.text, self.i).end()

    def parse(self):
        result = self.value(0)
        self.ws()
        if self.i != self.n:
            self.fail("unexpected trailing input")
        return result

    def string(self) -> str:
        text, start = self.text, self.i
        self.i += 1
        while self.i < self.n:
            self.i = _STRING_BODY.match(text, self.i).end()
            if self.i >= self.n:
                break
            if text[self.i] == "\\":
                self.i += 2
                continue
            self.i += 1
            try:
                return json.loads(text[start:self.i])
            except ValueError:
                self.fail("invalid string")
        self.fail("unterminated string")

    def take(self) -> str:
        c = self.char()
        self.i += 1
        return c

    def value(self, depth: int):
        if depth > 256:
            self.fail("nesting exceeds 256")
        self.ws()
        c = self.char()
        if c == '"':
            return self.string()
        if c == "{":
            self.i += 1
            self.ws()
            result: dict = {}
            if self.char() == "}":
                self.i += 1
                return result
            while True:
                self.ws()
                if self.char() != '"':
                    self.fail("expected object key")
                key = self.string()
                if key in result:
                    self.fail(f"duplicate object key {quote(key)}")
                self.ws()
                if self.take() != ":":
                    self.fail("expected colon")
                result[key] = self.value(depth + 1)
                self.ws()
                if self.char() == "}":
                    self.i += 1
                    return result
                if self.take() != ",":
                    self.fail("expected comma or closing brace")
        if c == "[":
            self.i += 1
            self.ws()
            items: list = []
            if self.char() == "]":
                self.i += 1
                return items
            while True:
                items.append(self.value(depth + 1))
                self.ws()
                if self.char() == "]":
                    self.i += 1
                    return items
                if self.take() != ",":
                    self.fail("expected comma or closing bracket")
        match = _LITERAL.match(self.text, self.i)
        if not match:
            self.fail("expected JSON value")
        token = match.group()
        self.i = match.end()
        if token == "true":
            return True
        if token == "false":
            return False
        if token == "null":
            return None
        parsed = _number(token)
        if not is_finite(parsed):
            self.fail("nonfinite number")
        return parsed
