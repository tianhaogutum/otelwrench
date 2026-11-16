# SPDX-License-Identifier: Apache-2.0
"""Prometheus ``query_range`` success-matrix parsing and canonical label identity."""
from __future__ import annotations

import math
from typing import Any

from ._js import js_string, js_truthy, stringify
from .validation import fail, keys, obj, sample_value, string_order_key, timestamp


def _template_json(value: Any, present: bool = True) -> str:
    """``${JSON.stringify(value)}`` inside a template literal."""
    return stringify(value) if present else "undefined"


# Shared exact identity for captured series and checkpoint entries.
def label_identity(metric: Any, where: str) -> dict:
    obj(metric, where)
    label_keys = sorted(metric.keys(), key=string_order_key)
    for key in label_keys:
        if len(key) == 0 or not isinstance(metric[key], str):
            fail(f"{where}: label names must be nonempty and values must be strings")
    labels = {k: metric[k] for k in label_keys}
    # An ordered array of pairs also preserves ordering for integer-like label names.
    identity = stringify([[k, metric[k]] for k in label_keys])
    return {"id": identity, "labels": labels}


def parse_matrix(raw: Any) -> dict:
    obj(raw, "API response")
    if raw.get("status") != "success" or not isinstance(raw.get("status"), str):
        error = raw.get("error")
        suffix = f": {js_string(error)}" if js_truthy(error) else ""
        got = _template_json(raw.get("status"), "status" in raw)
        fail(f"API response: expected status success, got {got}{suffix}")
    keys(raw, ["status", "data", "warnings", "infos"], ["status", "data"], "API response")
    keys(raw["data"], ["resultType", "result"], ["resultType", "result"], "API data")
    if raw["data"]["resultType"] != "matrix" or not isinstance(raw["data"]["resultType"], str):
        fail(f"API data: unsupported resultType {stringify(raw['data']['resultType'])}; expected matrix")
    if not isinstance(raw["data"]["result"], list):
        fail("API data.result: expected an array")
    for field in ("warnings", "infos"):
        if field in raw and (not isinstance(raw[field], list) or any(not isinstance(x, str) for x in raw[field])):
            fail(f"API response.{field}: expected an array of strings")
    min_ms = math.inf
    max_ms = -math.inf
    seen: set[str] = set()
    series = []
    for i, item in enumerate(raw["data"]["result"]):
        where = f"API data.result[{i}]"
        keys(item, ["metric", "values"], ["metric", "values"], where)
        identity = label_identity(item["metric"], f"{where}.metric")
        sid, labels = identity["id"], identity["labels"]
        if sid in seen:
            fail(f"{where}: duplicate canonical label set {sid}")
        seen.add(sid)
        if not isinstance(item["values"], list):
            fail(f"{where}.values: expected an array")
        previous = -math.inf
        samples: dict[float, dict] = {}
        for j, pair in enumerate(item["values"]):
            if not isinstance(pair, list) or len(pair) != 2:
                fail(f'{where}.values[{j}]: expected [timestamp, "value"]')
            t = timestamp(pair[0], f"{where}.values[{j}][0]")
            if t <= previous:
                fail(f"{where}.values[{j}]: unsorted or duplicate timestamps")
            previous = t
            value = sample_value(pair[1], f"{where}.values[{j}][1]")
            samples[t] = {"value": value, "raw": pair[1]}
            min_ms = min(min_ms, t)
            max_ms = max(max_ms, t)
        series.append({"id": sid, "labels": labels, "samples": samples})
    series.sort(key=lambda s: string_order_key(s["id"]))
    return {"series": series, "minMs": min_ms, "maxMs": max_ms,
            "warnings": raw["warnings"] if raw.get("warnings") is not None else [],
            "infos": raw["infos"] if raw.get("infos") is not None else []}
