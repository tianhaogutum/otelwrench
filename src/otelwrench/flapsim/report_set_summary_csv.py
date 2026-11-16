# SPDX-License-Identifier: Apache-2.0
"""Separately versioned compact report-set summary CSV (v1 unnamed, v2 named)."""
from __future__ import annotations

from typing import Any

from ._js import js_number, stringify
from .grid_csv import cell
from .validation import duration

# Independent of the fixed full CSV schema. Optional CLI names select v2; unnamed stays v1.
COLUMNS = [
    "schemaVersion", "csvSchema", "role", "candidateIndex", "scope", "seriesId",
    "captureSchemaVersion", "captureAlgorithm", "captureEncoding", "captureDigest",
    "ruleSchemaVersion", "ruleAlgorithm", "ruleEncoding", "ruleDigest",
    "evaluationIntervalMs", "forMs", "keepFiringForMs", "thresholdJson", "missingData",
    "windowStart", "windowEnd", "evaluations", "rangeSource", "originStart", "evaluatedThrough",
    "assumptionsJson", "sourceWarningsJson", "sourceInfosJson", "reports", "requiredEvaluations", "seriesCount",
    "firstFiring", "firingDurationSeconds", "firingEpisodes", "resolutions", "openAtEnd", "pendingAtEnd",
    "transitionCount", "censoredEpisodes", "missingSamples", "nanSamples", "heldEvaluations",
    "firstFiringDelaySeconds", "deltaFiringDurationSeconds", "deltaFiringEpisodes", "deltaResolutions",
    "deltaOpenAtEnd", "deltaPendingAtEnd", "deltaTransitionCount", "deltaCensoredEpisodes",
]


def _identity(i: dict) -> list:
    return [i["schemaVersion"], i["algorithm"], i["encoding"], i["digest"]]


def _get(d: Any, key: str) -> Any:
    return None if d is None else d.get(key)


# Private formatter: only a freshly verified complete-set summary is accepted
# here. The public API takes full saved replays, never an imported summary.
def format_report_set_summary_csv(result: dict, names: dict | None = None) -> str:
    named = names is not None
    header = [*COLUMNS[:4], "displayName", *COLUMNS[4:]] if named else COLUMNS
    records = [",".join(header)]

    def rows(report: dict, candidate_index: Any, aggregate_delta: Any = None, series_deltas: list | None = None) -> None:
        series_deltas = series_deltas or []
        cfg = report["exactConfig"]
        context = [
            *_identity(result["captureIdentity"]), *_identity(report["ruleIdentity"]),
            duration(cfg["evaluationInterval"]), duration(cfg["for"]), duration(cfg["keep_firing_for"]),
            stringify(cfg["threshold"]), cfg["missingData"],
            report["window"]["start"], report["window"]["end"], report["window"]["evaluations"], report["window"]["rangeSource"],
            report["origin"]["originStart"], report["origin"]["evaluatedThrough"],
            stringify(report["assumptions"]), stringify(report["sourceWarnings"]), stringify(report["sourceInfos"]),
            result["budget"]["reports"], result["budget"]["requiredEvaluations"],
        ]

        def row(s: dict, sid: Any, d: Any) -> None:
            display = []
            if named:
                display = [names["baseline"] if candidate_index is None else names["candidates"][candidate_index]]
            values = [
                2 if named else 1,
                "alert-flapsim-report-set-summary-v2" if named else "alert-flapsim-report-set-summary-v1",
                "baseline" if candidate_index is None else "candidate",
                candidate_index, *display,
                "aggregate" if sid is None else "series", sid, *context,
                s["series"] if sid is None else 1,
                s["firstFiring"], s["firingDurationSeconds"], s["firingEpisodes"], s["resolutions"],
                js_number(s["openAtEnd"]), js_number(s["pendingAtEnd"]),
                s["transitionCount"], s["censoredEpisodes"], s["missingSamples"], s["nanSamples"], s["heldEvaluations"],
                _get(d, "firstFiringDelaySeconds"), _get(d, "firingDurationSeconds"), _get(d, "firingEpisodes"),
                _get(d, "resolutions"), _get(d, "openAtEnd"), _get(d, "pendingAtEnd"),
                _get(d, "transitionCount"), _get(d, "censoredEpisodes"),
            ]
            records.append(",".join(cell(v) for v in values))

        row(report["summary"], None, aggregate_delta)
        for i, s in enumerate(report["series"]):
            row(s["summary"], s["id"], series_deltas[i] if i < len(series_deltas) else None)

    rows(result["baseline"], None)
    for c in result["candidates"]:
        rows(c["report"], c["candidateIndex"], c["delta"], c["seriesDeltas"])
    return "\r\n".join(records) + "\r\n"
