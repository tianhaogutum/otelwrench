# SPDX-License-Identifier: Apache-2.0
"""Input validation shared by replay, checkpoints, reports and grids."""
from __future__ import annotations

import math
import re
from typing import Any, NoReturn

from ._js import (
    is_finite_number,
    is_number,
    is_safe_integer,
    js_keys,
    math_round,
    stringify,
    to_double,
    utf16_key,
)


class FlapsimError(ValueError):
    """A rejected input or option; the message is the user-visible diagnostic."""


def fail(message: str) -> NoReturn:
    raise FlapsimError(message)


def obj(value: Any, where: str) -> None:
    if not isinstance(value, dict):
        fail(f"{where}: expected an object")


def keys(value: Any, allowed: list, required: list, where: str) -> None:
    obj(value, where)
    for key in js_keys(value):
        if key not in allowed:
            fail(f"{where}: unsupported field {stringify(key)}")
    for key in required:
        if key not in value:
            fail(f"{where}: missing field {key}")


def timestamp(value: Any, where: str | None = None) -> float:
    ms = math_round(to_double(value) * 1000) if is_number(value) else math.nan
    # Round-trip detects sub-ms inputs without rejecting e.g. 1.001 * 1000's
    # harmless floating-point multiplication error. Never snap an off-grid input.
    if not is_safe_integer(ms) or ms / 1000 != to_double(value):
        fail(f"{where}: expected numeric Unix seconds with exact millisecond precision")
    return ms


_DURATION = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:ms|s|m|h|d)")
_DURATION_PARTS = re.compile(r"(.*?)(ms|s|m|h|d)", re.S)
_UNITS = {"ms": 1, "s": 1000, "m": 60000, "h": 3600000, "d": 86400000}


# Deliberately narrower than Prometheus duration syntax: one unit, exact ms.
def duration(value: Any, where: str | None = None, positive: bool = False) -> float:
    if not isinstance(value, str) or len(value) > 64 or not _DURATION.fullmatch(value):
        fail(f"{where}: expected a nonnegative duration such as 0s, 500ms, 1.5m (one unit)")
    amount, unit = _DURATION_PARTS.fullmatch(value).groups()
    whole, _, fraction = amount.partition(".")
    scale = 10 ** len(fraction)
    numerator = int(whole + fraction) * _UNITS[unit]
    milliseconds = float(numerator // scale)
    if numerator % scale != 0 or not is_safe_integer(milliseconds) or (positive and milliseconds == 0):
        fail(f"{where}: duration must be {'positive' if positive else 'nonnegative'} and fit safe integer milliseconds")
    return milliseconds


COMPARE_OPS = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


def config(raw: Any) -> dict:
    required = ["evaluationInterval", "threshold", "for", "keep_firing_for", "missingData"]
    keys(raw, [*required, "start", "end"], required, "config")
    keys(raw["threshold"], ["op", "value"], ["op", "value"], "config.threshold")
    op = raw["threshold"]["op"]
    if not isinstance(op, str) or op not in COMPARE_OPS:
        fail("config.threshold.op: supported operators are >, >=, <, <=, ==, !=; no regex or PromQL")
    if not is_finite_number(raw["threshold"]["value"]):
        fail("config.threshold.value: expected a finite JSON number")
    if not isinstance(raw["missingData"], str) or raw["missingData"] not in ("false", "error"):
        fail('config.missingData: expected the string "false" or "error"')
    interval_ms = duration(raw["evaluationInterval"], "config.evaluationInterval", True)
    for_ms = duration(raw["for"], "config.for")
    hold_ms = duration(raw["keep_firing_for"], "config.keep_firing_for")
    if ("start" in raw) != ("end" in raw):
        fail("config: start and end must be provided together")
    start_ms = timestamp(raw["start"], "config.start") if "start" in raw else None
    end_ms = timestamp(raw["end"], "config.end") if "end" in raw else None
    if start_ms is not None and end_ms < start_ms:
        fail("config: end must be >= start")
    return {"intervalMs": interval_ms, "forMs": for_ms, "holdMs": hold_ms, "startMs": start_ms,
            "endMs": end_ms, "op": op, "threshold": raw["threshold"]["value"],
            "missingData": raw["missingData"]}


_NUMERIC = re.compile(r"[+-]?(?:(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)")


def sample_value(value: Any, where: str) -> float:
    if not isinstance(value, str):
        fail(f"{where}: sample value must be a Prometheus numeric string")
    if value == "NaN":
        return math.nan
    if value in ("+Inf", "Inf"):
        return math.inf
    if value == "-Inf":
        return -math.inf
    if not _NUMERIC.fullmatch(value) or not math.isfinite(float(value)):
        fail(f"{where}: unsupported numeric value {stringify(value)} (stale tokens are not API float samples)")
    return float(value)


def string_order_key(value: str) -> bytes:
    """Ascending JavaScript string order (UTF-16 code units)."""
    return utf16_key(value)
