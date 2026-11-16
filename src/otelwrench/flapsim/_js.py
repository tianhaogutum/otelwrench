# SPDX-License-Identifier: Apache-2.0
"""ECMAScript value semantics needed for byte-identical output.

The tool was first written in JavaScript and its saved reports, CSV files and
identity digests are defined in terms of ``JSON.stringify``/``String(number)``.
These helpers reproduce exactly the parts of that behavior the tool relies on:

* Number-to-string conversion (shortest round-trip digits, ``1e21``/``1e-7``
  exponent thresholds, ``-0`` printed as ``0``).
* ``JSON.stringify`` string escaping (lone surrogates escaped, no ASCII
  escaping of other characters) and object key order (array-index keys first,
  ascending, then insertion order).
* ``===`` and ``util.isDeepStrictEqual`` (booleans are never numbers, ``-0`` is
  distinct from ``0`` in deep equality, ``NaN`` equals ``NaN``).
* String ordering by UTF-16 code units, ``Math.round`` and ``toFixed``.
"""
from __future__ import annotations

import json
import math
import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

MAX_SAFE_INTEGER = 9007199254740991
_INDEX_RE = re.compile(r"0|[1-9][0-9]*")
_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_finite_number(value: Any) -> bool:
    if not is_number(value):
        return False
    if isinstance(value, int):
        return abs(value) < 2 ** 1024
    return math.isfinite(value)


def is_safe_integer(value: Any) -> bool:
    if not is_number(value):
        return False
    if isinstance(value, int):
        return abs(value) <= MAX_SAFE_INTEGER
    return math.isfinite(value) and value.is_integer() and abs(value) <= MAX_SAFE_INTEGER


def to_double(value: Any) -> float:
    """Convert a JSON number to the IEEE-754 double JavaScript would hold."""
    try:
        return float(value)
    except OverflowError:
        return math.inf if value > 0 else -math.inf


def math_round(value: float) -> float:
    """``Math.round``: nearest integer, ties toward +Infinity."""
    if not math.isfinite(value) or value == 0:
        return value
    floor = math.floor(value)
    result = float(floor + 1) if value - floor >= 0.5 else float(floor)
    if result == 0 and value < 0:
        return -0.0
    return result


def js_eq(a: Any, b: Any) -> bool:
    """``a === b`` for JSON-like values (identity for objects and arrays)."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if is_number(a) or is_number(b):
        return is_number(a) and is_number(b) and a == b
    if isinstance(a, str) or isinstance(b, str):
        return isinstance(a, str) and isinstance(b, str) and a == b
    if a is None or b is None:
        return a is None and b is None
    return a is b


def _object_is_number(a: Any, b: Any) -> bool:
    if isinstance(a, float) and math.isnan(a):
        return isinstance(b, float) and math.isnan(b)
    if a != b:
        return False
    if a == 0:
        return math.copysign(1.0, a) == math.copysign(1.0, b)
    return True


def deep_strict_equal(a: Any, b: Any) -> bool:
    """``util.isDeepStrictEqual`` for plain JSON-like data."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if is_number(a) or is_number(b):
        return is_number(a) and is_number(b) and _object_is_number(a, b)
    if isinstance(a, str) or isinstance(b, str):
        return isinstance(a, str) and isinstance(b, str) and a == b
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, (list, tuple)) or isinstance(b, (list, tuple)):
        if not (isinstance(a, (list, tuple)) and isinstance(b, (list, tuple))) or len(a) != len(b):
            return False
        return all(deep_strict_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) or isinstance(b, dict):
        if not (isinstance(a, dict) and isinstance(b, dict)) or len(a) != len(b):
            return False
        for key, value in a.items():
            if key not in b or not deep_strict_equal(value, b[key]):
                return False
        return True
    return a == b


def _is_array_index(key: str) -> bool:
    return bool(_INDEX_RE.fullmatch(key)) and int(key) < 4294967295


def js_keys(obj: dict) -> list:
    """``Object.keys`` order: integer-like keys ascending, then insertion order."""
    keys = list(obj.keys())
    index = [k for k in keys if isinstance(k, str) and _is_array_index(k)]
    if not index:
        return keys
    index.sort(key=int)
    return index + [k for k in keys if not (isinstance(k, str) and _is_array_index(k))]


def utf16_key(value: str) -> bytes:
    """Sort key matching JavaScript ``<`` on strings (UTF-16 code units)."""
    return value.encode("utf-16-be", "surrogatepass")


def is_well_formed(value: str) -> bool:
    """``String.prototype.isWellFormed``: no unpaired UTF-16 surrogates."""
    if not _SURROGATE_RE.search(value):
        return True
    i, n = 0, len(value)
    while i < n:
        code = ord(value[i])
        if 0xD800 <= code <= 0xDBFF and i + 1 < n and 0xDC00 <= ord(value[i + 1]) <= 0xDFFF:
            i += 2
            continue
        if 0xD800 <= code <= 0xDFFF:
            return False
        i += 1
    return True


def node_utf8(value: str) -> bytes:
    """Encode like Node ``Buffer.from(string, 'utf8')`` (lone surrogates -> U+FFFD)."""
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError:
        pass
    out = []
    i, n = 0, len(value)
    while i < n:
        code = ord(value[i])
        if 0xD800 <= code <= 0xDBFF and i + 1 < n and 0xDC00 <= ord(value[i + 1]) <= 0xDFFF:
            out.append(chr(0x10000 + ((code - 0xD800) << 10) + (ord(value[i + 1]) - 0xDC00)))
            i += 2
            continue
        out.append("�" if 0xD800 <= code <= 0xDFFF else value[i])
        i += 1
    return "".join(out).encode("utf-8")


def utf8_length(value: str) -> int:
    return len(node_utf8(value))


def number_to_string(value: Any) -> str:
    """``String(number)`` / ``Number.prototype.toString()``."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        if abs(value) <= MAX_SAFE_INTEGER:
            return str(value)
        value = to_double(value)
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    text = repr(abs(value))
    mantissa, _, exp_text = text.partition("e")
    exponent = int(exp_text) if exp_text else 0
    whole, _, fraction = mantissa.partition(".")
    raw = whole + fraction
    stripped = raw.lstrip("0")
    leading = len(raw) - len(stripped)
    digits = stripped.rstrip("0") or "0"
    n = len(whole) + exponent - leading
    k = len(digits)
    if k <= n <= 21:
        body = digits + "0" * (n - k)
    elif 0 < n <= 21:
        body = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        body = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        mark = "+" if e >= 0 else "-"
        body = (digits if k == 1 else digits[0] + "." + digits[1:]) + "e" + mark + str(abs(e))
    return sign + body


def to_fixed(value: float, places: int) -> str:
    """``Number.prototype.toFixed`` (ties on the exact binary value round up)."""
    if not math.isfinite(value):
        return number_to_string(value)
    if abs(value) >= 1e21:
        return number_to_string(value)
    negative = value < 0
    quantum = Decimal(1).scaleb(-places)
    text = format(Decimal(abs(value)).quantize(quantum, rounding=ROUND_HALF_UP), "f")
    return ("-" + text) if negative else text


def _quote(value: str) -> str:
    if not _SURROGATE_RE.search(value):
        return json.dumps(value, ensure_ascii=False)
    out = ['"']
    i, n = 0, len(value)
    while i < n:
        ch = value[i]
        code = ord(ch)
        if 0xD800 <= code <= 0xDBFF and i + 1 < n and 0xDC00 <= ord(value[i + 1]) <= 0xDFFF:
            out.append(chr(0x10000 + ((code - 0xD800) << 10) + (ord(value[i + 1]) - 0xDC00)))
            i += 2
            continue
        if 0xD800 <= code <= 0xDFFF:
            out.append("\\u%04x" % code)
        else:
            out.append(json.dumps(ch, ensure_ascii=False)[1:-1])
        i += 1
    out.append('"')
    return "".join(out)


def stringify(value: Any, indent: int | None = None) -> str:
    """``JSON.stringify(value, null, indent)`` for JSON-like Python data."""
    parts: list[str] = []
    _write(value, parts, indent, "")
    return "".join(parts)


def _write(value: Any, parts: list, indent: int | None, current: str) -> None:
    if value is None:
        parts.append("null")
    elif value is True:
        parts.append("true")
    elif value is False:
        parts.append("false")
    elif isinstance(value, str):
        parts.append(_quote(value))
    elif isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            parts.append("null")
        else:
            parts.append(number_to_string(value))
    elif isinstance(value, dict):
        keys = js_keys(value)
        if not keys:
            parts.append("{}")
            return
        inner = current + " " * indent if indent else current
        parts.append("{")
        for i, key in enumerate(keys):
            if i:
                parts.append(",")
            if indent:
                parts.append("\n" + inner)
            parts.append(_quote(str(key)))
            parts.append(": " if indent else ":")
            _write(value[key], parts, indent, inner)
        if indent:
            parts.append("\n" + current)
        parts.append("}")
    elif isinstance(value, (list, tuple)):
        if not value:
            parts.append("[]")
            return
        inner = current + " " * indent if indent else current
        parts.append("[")
        for i, item in enumerate(value):
            if i:
                parts.append(",")
            if indent:
                parts.append("\n" + inner)
            _write(item, parts, indent, inner)
        if indent:
            parts.append("\n" + current)
        parts.append("]")
    else:
        raise TypeError(f"cannot serialize {type(value).__name__}")


def js_string(value: Any) -> str:
    """``String(value)`` for JSON-like data."""
    if value is None:
        return "null"
    if isinstance(value, (bool, int, float)):
        return number_to_string(value)
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return ",".join("" if item is None else js_string(item) for item in value)
    if isinstance(value, dict):
        return "[object Object]"
    return str(value)


def js_truthy(value: Any) -> bool:
    if value is None or value is False:
        return False
    if is_number(value):
        return not (value == 0 or (isinstance(value, float) and math.isnan(value)))
    if isinstance(value, str):
        return value != ""
    return True


def js_number(value: Any) -> Any:
    """``Number(value)`` for the booleans and numbers summaries contain."""
    if isinstance(value, bool):
        return int(value)
    return value
