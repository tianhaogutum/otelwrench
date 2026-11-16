# SPDX-License-Identifier: Apache-2.0
"""Join contiguous checkpoint-chained saved replay reports."""
from __future__ import annotations

from typing import Any, Callable

from ._js import is_safe_integer, js_eq
from .report import ABSENT, MAX_REPORT_EVALUATIONS, describe_report, equal, verify_report
from .validation import fail

MAX_REPORTS = 256


# The replay callback shares the existing state machine, timer arithmetic and
# summary generation. Saved derived fields are verified, never summed or trusted.
def join_reports(reports: Any, replay: Callable) -> dict:
    if not isinstance(reports, list) or len(reports) < 2 or len(reports) > MAX_REPORTS:
        fail(f"join: expected an ordered array of 2 to {MAX_REPORTS} replay reports")
    windows = [describe_report(raw, f"join reports[{i}]", i > 0) for i, raw in enumerate(reports)]
    first = windows[0]
    if not js_eq(first["raw"]["checkpoint"]["originStart"], first["raw"]["window"]["start"]):
        fail("join: first report must start at checkpoint originStart; missing origin history")
    evaluations = 0
    for i, w in enumerate(windows):
        evaluations += w["steps"] * max(1, len(w["series"]))
        if evaluations > MAX_REPORT_EVALUATIONS:
            fail(f"join: combined timeline exceeds {MAX_REPORT_EVALUATIONS} series-evaluations")
        if not is_safe_integer((w["end"] - first["start"]) * max(1, len(first["series"]))):
            fail("join: aggregate duration exceeds safe integer milliseconds")
        if i > 0:
            if w["start"] - windows[i - 1]["end"] != first["cfg"]["intervalMs"]:
                fail("join: reports must be contiguous and ordered; gaps and overlaps are rejected")
            for field in ("intervalMs", "forMs", "holdMs", "op", "threshold", "missingData"):
                if not js_eq(w["cfg"][field], first["cfg"][field]):
                    fail("join: alert settings must match, including evaluationInterval")
            equal([s["id"] for s in w["series"]], [s["id"] for s in first["series"]], "join exact label sets")
            if not js_eq(w["raw"]["checkpoint"]["originStart"], first["raw"]["checkpoint"]["originStart"]):
                fail("join: checkpoint originStart must match")

    combined = {"status": "success",
                "data": {"resultType": "matrix",
                         "result": [{"metric": s["entry"]["labels"], "values": []} for s in first["series"]]},
                "warnings": [], "infos": []}
    checkpoint: Any = ABSENT
    for w in windows:
        verified = verify_report(w, replay, checkpoint)
        report, matrix = verified["report"], verified["matrix"]
        for i, s in enumerate(matrix["data"]["result"]):
            combined["data"]["result"][i]["values"].extend(s["values"])
        checkpoint = report["checkpoint"]
        combined["warnings"].extend(w["raw"]["sourceWarnings"])
        combined["infos"].extend(w["raw"]["sourceInfos"])
    return replay(combined, {**first["rawConfig"], "end": windows[-1]["raw"]["window"]["end"]})
