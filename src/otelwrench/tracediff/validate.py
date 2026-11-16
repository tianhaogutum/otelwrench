# SPDX-License-Identifier: Apache-2.0
"""Field validators shared by the OTLP and Jaeger readers.

``UNDEFINED`` marks an absent property; ``None`` is an explicit JSON ``null``.
The two differ wherever the JavaScript original tested ``!== undefined``.
"""
from __future__ import annotations

import re

from .jsontext import UNDEFINED, InputError, is_finite, is_integer, is_safe_integer, js_keys, js_str

_DIGITS = re.compile(r"[0-9]+")
_SIGNED_DIGITS = re.compile(r"-?[0-9]+")
_HEX = re.compile(r"[0-9a-fA-F]+")
_ZEROS = re.compile(r"0+")
_INT_PREFIX = re.compile(r"^-?0*")
BASE64 = re.compile(r"(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?")
# String.prototype.trim() whitespace (WhiteSpace + LineTerminator), not str.isspace().
_JS_WHITESPACE = "\t\n\x0b\x0c\r \xa0            " \
                 "    　﻿"
UINT64_MAX = (1 << 64) - 1
UINT32_MAX = 4294967295


def nullish(value, default):
    """``value ?? default``."""
    return default if value is None or value is UNDEFINED else value


def check(condition, path: str, message: str) -> None:
    if not condition:
        raise InputError(f"{path}: {message}")


def object_(value, path: str, allowed=None):
    check(isinstance(value, dict), path, "expected object")
    if allowed:
        for key in js_keys(value):
            check(key in allowed, f"{path}.{key}", "unsupported field")
    return value


def array(value, path: str):
    check(isinstance(value, list), path, "expected array")
    return value


def string(value, path: str, nonempty: bool = False):
    check(isinstance(value, str) and (not nonempty or len(value.strip(_JS_WHITESPACE)) > 0), path,
          "expected nonempty string" if nonempty else "expected string")
    return value


def uint(value, path: str, maximum: int = UINT64_MAX) -> int:
    check((isinstance(value, str) and _DIGITS.fullmatch(value) is not None)
          or (is_safe_integer(value) and value >= 0), path,
          "expected unsigned integer (large values must be decimal strings)")
    check(len(js_str(value).lstrip("0")) <= len(str(maximum)), path, "integer out of range")
    result = int(value)
    check(result <= maximum, path, "integer out of range")
    return result


def hex_id(value, path: str, widths: list[int], optional: bool = False):
    if optional and (value is UNDEFINED or value == ""):
        return None
    check(isinstance(value, str) and _HEX.fullmatch(value) is not None and len(value) in widths
          and _ZEROS.fullmatch(value) is None, path,
          f"expected nonzero hex ID of length {' or '.join(str(w) for w in widths)}")
    return value.lower().rjust(max(widths), "0")


def enumeration(value, path: str, values: list[str]) -> str:
    check(is_integer(value) and 0 <= value < len(values), path, "unsupported integer enum")
    return values[int(value)]


def optional_string(obj: dict, key: str, path: str) -> None:
    if key in obj:
        string(obj[key], f"{path}.{key}")


def optional_uint(obj: dict, key: str, path: str, maximum: int = UINT64_MAX) -> None:
    if key in obj:
        uint(obj[key], f"{path}.{key}", maximum)


def int64(value, path: str, expected: str, out_of_range: str) -> None:
    check((isinstance(value, str) and _SIGNED_DIGITS.fullmatch(value) is not None) or is_safe_integer(value),
          path, expected)
    check(len(_INT_PREFIX.sub("", js_str(value), count=1)) <= 19, path, out_of_range)
    parsed = int(value)
    check(-(1 << 63) <= parsed < (1 << 63), path, out_of_range)


def attributes(value, path: str) -> list:
    seen: set = set()
    result = []
    for i, attr in enumerate(array(value, path)):
        p = f"{path}[{i}]"
        object_(attr, p, ["key", "value"])
        string(attr.get("key", UNDEFINED), f"{p}.key", True)
        check(attr["key"] not in seen, p, f"duplicate attribute {attr['key']}")
        seen.add(attr["key"])
        _any_value(attr.get("value", UNDEFINED), f"{p}.value")
        result.append(attr)
    return result


def _any_value(value, path: str, depth: int = 0) -> None:
    check(depth <= 64, path, "attribute nesting exceeds 64")
    object_(value, path, ["stringValue", "boolValue", "intValue", "doubleValue", "bytesValue", "arrayValue",
                          "kvlistValue"])
    keys = js_keys(value)
    check(len(keys) <= 1, path, "expected at most one AnyValue variant")
    if not keys:
        return  # Empty AnyValue is the OTLP null value.
    k = keys[0]
    v = value[k]
    if k == "stringValue":
        string(v, path)
    if k == "boolValue":
        check(isinstance(v, bool), path, "expected boolean")
    if k == "intValue":
        int64(v, path, "expected int64", "int64 out of range")
    if k == "doubleValue":
        check(is_finite(v), path, "expected finite number")
    if k == "bytesValue":
        check(isinstance(v, str) and BASE64.fullmatch(v) is not None, path, "expected padded base64")
    if k in ("arrayValue", "kvlistValue"):
        object_(v, path, ["values"])
        seen: set = set()
        for i, item in enumerate(array(nullish(v.get("values", UNDEFINED), []), path)):
            if k == "arrayValue":
                _any_value(item, f"{path}[{i}]", depth + 1)
            else:
                object_(item, path, ["key", "value"])
                string(item.get("key", UNDEFINED), path)
                check(item["key"] not in seen, path, f"duplicate kvlist key {item['key']}")
                seen.add(item["key"])
                _any_value(item.get("value", UNDEFINED), path, depth + 1)
