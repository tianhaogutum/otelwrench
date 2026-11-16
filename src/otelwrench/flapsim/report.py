# SPDX-License-Identifier: Apache-2.0
"""Saved replay-report description and verification by regenerated replay."""
from __future__ import annotations

import math
from typing import Any, Callable

from ._js import deep_strict_equal, is_safe_integer, js_eq
from .matrix import label_identity
from .validation import config, fail, keys, obj, string_order_key, timestamp

MAX_REPORT_EVALUATIONS = 1_000_000
REPORT_FIELDS = ["schemaVersion", "command", "config", "window", "assumptions",
                 "sourceWarnings", "sourceInfos", "summary", "series", "checkpoint"]
CHECKPOINT_FIELDS = ["schemaVersion", "kind", "config", "originStart", "evaluatedThrough", "series"]
RULE_FIELDS = ["evaluationInterval", "threshold", "for", "keep_firing_for", "missingData"]

# Sentinel for "no checkpoint argument" (JavaScript ``undefined``), distinct
# from an explicit ``None`` (``null``), which is rejected as a malformed checkpoint.
ABSENT: Any = type("Absent", (), {"__repr__": lambda self: "ABSENT"})()


def equal(actual: Any, expected: Any, where: str) -> None:
    if not deep_strict_equal(actual, expected):
        fail(f"{where}: inconsistent with replayed timeline/checkpoint chain")


def ordered_series(entries: Any, where: str) -> list:
    if not isinstance(entries, list):
        fail(f"{where}: expected an array")
    seen: set[str] = set()
    result = []
    for i, s in enumerate(entries):
        # Only inspect identity here; full schema/state equality is checked after replay.
        obj(s, f"{where}[{i}]")
        sid = label_identity(s.get("labels"), f"{where}[{i}].labels")["id"]
        if sid in seen:
            fail(f"{where}: duplicate canonical label set")
        seen.add(sid)
        result.append({"id": sid, "entry": s})
    result.sort(key=lambda x: string_order_key(x["id"]))
    return result


def describe_report(raw: Any, where: str, require_explicit_bounds: bool = False) -> dict:
    keys(raw, REPORT_FIELDS, REPORT_FIELDS, where)
    if not js_eq(raw["schemaVersion"], 1) or not js_eq(raw["command"], "replay"):
        fail(f"{where}: expected a schemaVersion 1 replay report")
    cfg_fields = ["evaluationIntervalSeconds", "threshold", "forSeconds", "keepFiringForSeconds", "missingData"]
    keys(raw["config"], cfg_fields, cfg_fields, f"{where}.config")
    window_fields = ["start", "end", "evaluations", "rangeSource"]
    window = raw["window"]
    keys(window, window_fields, window_fields, f"{where}.window")
    range_source = window["rangeSource"]
    if (not isinstance(range_source, str) or range_source not in ("config", "sample-extrema")
            or (require_explicit_bounds and range_source != "config")):
        fail(f"{where}: resumed windows require explicit config bounds")
    start = timestamp(window["start"], f"{where}.window.start")
    end = timestamp(window["end"], f"{where}.window.end")
    checkpoint = raw["checkpoint"]
    keys(checkpoint, CHECKPOINT_FIELDS, CHECKPOINT_FIELDS, f"{where}.checkpoint")
    keys(checkpoint["config"], RULE_FIELDS, RULE_FIELDS, f"{where}.checkpoint.config")
    # Seconds can round away a millisecond at large safe-integer timer values.
    # Reconstruct from exact checkpoint durations; verify_report still checks BOTH
    # the normalized report config and checkpoint against the regenerated replay.
    raw_config = {**checkpoint["config"], "start": window["start"], "end": window["end"]}
    cfg = config(raw_config)
    equal(raw["config"], {"evaluationIntervalSeconds": cfg["intervalMs"] / 1000,
                          "threshold": {"op": cfg["op"], "value": cfg["threshold"]},
                          "forSeconds": cfg["forMs"] / 1000,
                          "keepFiringForSeconds": cfg["holdMs"] / 1000,
                          "missingData": cfg["missingData"]}, f"{where}.config")
    span = end - start
    if not is_safe_integer(span) or math.fmod(span, cfg["intervalMs"]) != 0:
        fail(f"{where}: incompatible evaluation grid")
    steps = span / cfg["intervalMs"] + 1
    if not js_eq(window["evaluations"], steps):
        fail(f"{where}: incorrect window.evaluations")
    series = ordered_series(raw["series"], f"{where}.series")
    if not js_eq(checkpoint["evaluatedThrough"], window["end"]):
        fail(f"{where}: checkpoint must end at window.end")
    return {"raw": raw, "rawConfig": raw_config, "cfg": cfg, "start": start, "end": end,
            "steps": steps, "series": series, "where": where}


# Reconstruct only raw samples; every saved derived field is checked against
# a regenerated report. Object and complete-series ordering are not identity.
def verify_report(w: dict, replay: Callable, checkpoint: Any = ABSENT) -> dict:
    result = []
    for item in w["series"]:
        s = item["entry"]
        timeline = s.get("timeline")
        if not isinstance(timeline, list) or len(timeline) != w["steps"]:
            fail(f"{w['where']}: timeline must contain every evaluation exactly once")
        values = []
        for i, point in enumerate(timeline):
            fields = ["timestamp", "value", "sampleKind", "condition", "state", "pendingSince", "holdSince", "reason"]
            keys(point, fields, fields, f"{w['where']}.timeline[{i}]")
            if timestamp(point["timestamp"], f"{w['where']}.timeline[{i}].timestamp") != w["start"] + i * w["cfg"]["intervalMs"]:
                fail(f"{w['where']}: timeline timestamps must follow the complete evaluation grid")
            if point["value"] is not None:
                values.append([point["timestamp"], point["value"]])
        result.append({"metric": s["labels"], "values": values})
    raw = w["raw"]
    matrix = {"status": "success", "data": {"resultType": "matrix", "result": result},
              "warnings": raw["sourceWarnings"], "infos": raw["sourceInfos"]}
    raw_config = dict(w["rawConfig"])
    if raw["window"]["rangeSource"] == "sample-extrema":
        raw_config.pop("start", None)
        raw_config.pop("end", None)
    report = replay(matrix, raw_config, checkpoint)
    checkpoint_series = [s["entry"] for s in ordered_series(raw["checkpoint"]["series"], f"{w['where']}.checkpoint.series")]
    canonical = {**raw, "series": [s["entry"] for s in w["series"]],
                 "checkpoint": {**raw["checkpoint"], "series": checkpoint_series}}
    equal(canonical, report, w["where"])
    return {"report": report, "matrix": matrix}
