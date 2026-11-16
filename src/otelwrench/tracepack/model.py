# SPDX-License-Identifier: Apache-2.0
"""Internal span model, validation primitives, loss collection and normalization."""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any, NoReturn

from .errors import TracepackError
from .jsonfmt import (
    MAX_SAFE_INTEGER,
    MIN_SAFE_INTEGER,
    UNDEFINED,
    DecimalToken,
    JSBigInt,
    big_int,
    js_keys,
    js_quote,
    stringify,
    u16,
)

U64 = (1 << 64) - 1
I64 = (1 << 63) - 1

_DECIMAL_STRING = re.compile(r"-?(0|[1-9][0-9]*)")
_HEX = re.compile(r"[0-9a-fA-F]+")
_ZEROS = re.compile(r"0+")


def get(obj: Mapping[str, Any], key: str) -> Any:
    """Property read that returns :data:`UNDEFINED` for an absent key."""
    return obj[key] if key in obj else UNDEFINED


def field(path: str, key: str) -> str:
    return f"{path}[{js_quote(key)}]"


def fail(path: str, message: str) -> NoReturn:
    raise TracepackError(f"{path}: {message}")


def object_(x: Any, path: str) -> Any:
    if not isinstance(x, Mapping):
        fail(path, "expected object")
    return x


def array(x: Any, path: str) -> list[Any]:
    if not isinstance(x, list):
        fail(path, "expected array")
    return x


def string(x: Any, path: str) -> str:
    if not isinstance(x, str):
        fail(path, "expected string")
    return x


def boolean(x: Any, path: str) -> bool:
    if x is not True and x is not False:
        fail(path, "expected boolean")
    return x


def _safe(n: int) -> bool:
    return MIN_SAFE_INTEGER <= n <= MAX_SAFE_INTEGER


def integer(x: Any, path: str, *, min: int = 0, max: int = U64, nanos: bool = False) -> int:  # noqa: A002
    """Exact integer from a safe JSON number, exact numeric token or decimal string."""
    if isinstance(x, DecimalToken):
        if x.integer is None:
            fail(path, "expected an exact integer numeric token")
        if nanos and not _safe(x.integer):
            fail(path, "unsafe numeric nanos; use a quoted decimal string")
        n = x.integer
    elif type(x) is JSBigInt:
        if nanos:
            fail(path, "unsafe numeric nanos; use a quoted decimal string")
        n = int(x)
    elif type(x) is int:
        if not _safe(x):
            fail(path, "expected safe integer or decimal string")
        n = x
    elif type(x) is float:
        if not (math.isfinite(x) and x.is_integer() and _safe(int(x))):
            fail(path, "expected safe integer or decimal string")
        n = int(x)
    elif isinstance(x, str) and _DECIMAL_STRING.fullmatch(x):
        n = big_int(x)
    else:
        fail(path, "expected integer or decimal string")
    if n < min or n > max:
        fail(path, f"integer outside range {min}..{max}")
    return n


def id_(x: Any, path: str, trace: bool = False, short_trace: bool = False) -> str:
    string(x, path)
    lengths = ([16, 32] if short_trace else [32]) if trace else [16]
    if len(x) not in lengths or not _HEX.fullmatch(x) or _ZEROS.fullmatch(x):
        fail(path, f"expected nonzero {' or '.join(map(str, lengths))}-digit hex ID")
    return x.lower().rjust(32 if trace else 16, "0")


class LossCollector:
    def __init__(self) -> None:
        self._losses: dict[str, dict[str, str]] = {}

    def add(self, path: str, reason: str) -> None:
        self._losses[f"{path}\0{reason}"] = {"path": path, "reason": reason}

    def unknown(self, x: Mapping[str, Any], keys: list[str], path: str) -> None:
        for key in js_keys(x):
            if key not in keys:
                self.add(field(path, key), "unsupported field dropped")

    def list(self) -> list[dict[str, str]]:
        return sorted(({"path": v["path"], "reason": v["reason"]} for v in self._losses.values()),
                      key=lambda loss: (u16(loss["path"]), u16(loss["reason"])))


def typed(type_: str, value: Any, path: str) -> dict[str, Any]:
    return {"type": type_, "value": value, "path": path}


def sorted_attrs(attrs: Mapping[str, Any]) -> list[tuple[str, Any]]:
    return sorted(attrs.items(), key=lambda item: u16(item[0]))


def put(attrs: dict[str, Any], key: Any, value: Any, path: str) -> None:
    string(key, f"{path}.key")
    if key in attrs:
        fail(path, f"duplicate attribute key {js_quote(key)}")
    attrs[key] = value


def base_span(**span: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"parentId": "", "kind": 0, "status": {"code": 0, "message": ""},
                           "resource": {}, "attrs": {}, "events": []}
    out.update(span)
    return out


def _attr_triples(attrs: Mapping[str, Any]) -> list[list[Any]]:
    return [[k, v["type"], v["value"]] for k, v in sorted_attrs(attrs)]


def _semantic(s: dict[str, Any]) -> str:
    return stringify({
        "traceId": s["traceId"], "spanId": s["spanId"], "parentId": s["parentId"], "name": s["name"],
        "start": s["start"], "end": s["end"], "kind": s["kind"], "status": s["status"],
        "resource": _attr_triples(s["resource"]), "attrs": _attr_triples(s["attrs"]),
        "events": [[e["time"], e["name"], _attr_triples(e["attrs"])] for e in s["events"]],
    })


def normalize(spans: list[dict[str, Any]], loss: LossCollector) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for s in spans:
        if s["end"] < s["start"]:
            fail(s["paths"]["end"], "end before start")
        if s["end"] > U64:
            fail(s["paths"]["end"], "end exceeds uint64 nanoseconds")
        if s["parentId"] == s["spanId"]:
            fail(s["path"], "span cannot be its own parent")
        s["events"].sort(key=lambda e: (e["time"], u16(e["name"]), u16(stringify(_attr_triples(e["attrs"])))))
        key = f"{s['traceId']}/{s['spanId']}"
        if key in seen:
            if _semantic(seen[key]) != _semantic(s):
                fail(s["path"], f"conflicting duplicate span {key}")
            loss.add(s["path"], "identical duplicate span collapsed")
        else:
            seen[key] = s
    # Missing parents are legal for partial fixtures; cycles among known spans are not.
    done: set[str] = set()
    for s in seen.values():
        visiting: dict[str, None] = {}
        current: dict[str, Any] | None = s
        while current is not None and f"{current['traceId']}/{current['spanId']}" not in done:
            key = f"{current['traceId']}/{current['spanId']}"
            if key in visiting:
                fail(current["path"], "parent cycle")
            visiting[key] = None
            current = seen.get(f"{current['traceId']}/{current['parentId']}")
        done.update(visiting)
    return sorted(seen.values(), key=lambda s: (u16(s["traceId"]), s["start"], u16(s["spanId"])))
