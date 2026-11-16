# SPDX-License-Identifier: Apache-2.0
"""Fixed CSV schema shared by compare, compare-reports, compare-report-set and grid."""
from __future__ import annotations

import re
from typing import Any

from ._js import js_number, js_string, stringify

COLUMNS = [
    "schemaVersion", "role", "candidateIndex", "scope", "seriesId",
    "baselineForMs", "baselineKeepFiringForMs", "forMs", "keepFiringForMs",
    "evaluationIntervalMs", "thresholdJson", "missingData", "windowStart", "windowEnd", "rangeSource", "seriesCount",
    "firstFiring", "firingDurationSeconds", "firingEpisodes", "resolutions", "openAtEnd", "pendingAtEnd",
    "transitionCount", "missingSamples", "nanSamples", "heldEvaluations",
    "firstFiringDelaySeconds", "deltaFiringDurationSeconds", "deltaFiringEpisodes", "deltaResolutions",
    "deltaOpenAtEnd", "deltaPendingAtEnd", "deltaTransitionCount",
]
_QUOTE = re.compile(r'[",\r\n]')


def cell(value: Any) -> str:
    text = "null" if value is None else js_string(value)
    return '"' + text.replace('"', '""') + '"' if _QUOTE.search(text) else text


def _get(d: Any, key: str) -> Any:
    return None if d is None else d.get(key)


# Only called with freshly evaluated compact runs and exact input timer pairs.
def format_grid_csv(report: dict, interval_ms: float, baseline_timers: list, candidate_timers: list) -> str:
    records = [",".join(COLUMNS)]

    def row(run: dict, baseline: dict, timers: list, candidate_index: Any, series_index: Any = None) -> None:
        entry = run if series_index is None else run["series"][series_index]
        base = baseline if series_index is None else baseline["series"][series_index]
        s = entry["summary"]
        d = None if candidate_index is None else entry["delta"]
        window = run.get("window") or report["window"]
        values = [
            1, "baseline" if candidate_index is None else "candidate", candidate_index,
            "aggregate" if series_index is None else "series", None if series_index is None else entry["id"],
            *baseline_timers, *timers, interval_ms, stringify(run["config"]["threshold"]), run["config"]["missingData"],
            window["start"], window["end"], window["rangeSource"], s["series"] if series_index is None else 1,
            s["firstFiring"], s["firingDurationSeconds"], s["firingEpisodes"], s["resolutions"],
            js_number(s["openAtEnd"]), js_number(s["pendingAtEnd"]),
            s["transitionCount"], s["missingSamples"], s["nanSamples"], s["heldEvaluations"],
            _get(d, "firstFiringDelaySeconds"), _get(d, "firingDurationSeconds"), _get(d, "firingEpisodes"),
            _get(d, "resolutions"), _get(d, "openAtEnd"),
            None if d is None else js_number(s["pendingAtEnd"]) - js_number(base["summary"]["pendingAtEnd"]),
            _get(d, "transitionCount"),
        ]
        records.append(",".join(cell(v) for v in values))

    def rows(run: dict, timers: list, index: Any) -> None:
        row(run, report["baseline"], timers, index)
        for i in range(len(run["series"])):
            row(run, report["baseline"], timers, index, i)

    rows(report["baseline"], baseline_timers, None)
    for i, candidate in enumerate(report["candidates"]):
        rows(candidate, candidate_timers[i], i)
    return "\r\n".join(records) + "\r\n"
