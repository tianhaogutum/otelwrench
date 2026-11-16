# SPDX-License-Identifier: Apache-2.0
"""Synthetic corpus-mutation experiment behind ``examples/tracediff/experiment-results.json``.

Independent oracle: it does not use the parser, normalizer, comparator or test
generators to establish expected values. It reads the committed wire-format
records directly and checks the CLI's JSON report against them.
"""
from __future__ import annotations

import json

from .support import EXAMPLES, run

KINDS = ["unspecified", "internal", "server", "client", "producer", "consumer"]


def _read(name: str):
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


def _key(service, operation, kind) -> str:
    return json.dumps([service, operation, kind], ensure_ascii=False, separators=(",", ":"))


def _wire_groups(data) -> dict:
    groups: dict = {}

    def add(service, operation, kind, ns, error):
        g = groups.setdefault(_key(service, operation, kind), {"durations": [], "errors": 0})
        g["durations"].append(ns)
        g["errors"] += int(error)

    if data.get("resourceSpans"):
        for rs in data["resourceSpans"]:
            service = next(a for a in rs["resource"]["attributes"] if a["key"] == "service.name")["value"]["stringValue"]
            for ss in rs["scopeSpans"]:
                for s in ss["spans"]:
                    add(service, s["name"], KINDS[s["kind"]], int(s["endTimeUnixNano"]) - int(s["startTimeUnixNano"]),
                        s["status"]["code"] == 2)
    else:
        for trace in data["data"]:
            for s in trace["spans"]:
                tag = {t["key"]: t["value"] for t in s["tags"]}
                add(trace["processes"][s["processID"]]["serviceName"], s["operationName"], tag["span.kind"],
                    int(s["duration"]) * 1000, tag["error"] is True)
    return groups


def _verify_stats(summary: dict, expected: dict) -> None:
    assert len(summary["groups"]) == len(expected)
    for actual in summary["groups"]:
        g = actual["group"]
        oracle = expected[_key(g["service"], g["operation"], g["kind"])]
        values = sorted(oracle["durations"])
        n = len(values)
        assert actual["count"] == n and actual["errorCount"] == oracle["errors"]
        assert actual["errorFraction"] == oracle["errors"] / n

        def rank(percent: int) -> int:
            # Integer ceiling division avoids sharing the implementation's rank expression.
            return (n * percent + 99) // 100 - 1

        assert actual["durationMs"] == {"min": float(values[0]) / 1e6, "median": float(values[rank(50)]) / 1e6,
                                        "p95": float(values[rank(95)]) / 1e6, "max": float(values[-1]) / 1e6}


def _run(candidate: str):
    result = run([EXAMPLES / "baseline.json", EXAMPLES / candidate, "--config", EXAMPLES / "budget.json",
                  "--format", "json"])
    assert result.stderr == ""
    report = json.loads(result.stdout)
    _verify_stats(report["baseline"], _wire_groups(_read("baseline.json")))
    _verify_stats(report["candidate"], _wire_groups(_read(candidate)))
    return result, report


def run_experiment() -> dict:
    cases = []
    checkout_group = {"service": "shop", "operation": "checkout", "kind": "server"}
    for name in ("renamed.json", "compatible.json"):
        result, report = _run(name)
        assert result.status == 0 and report["regression"] is False
        assert report["diff"] == {"operations": {"added": [], "removed": []},
                                  "edges": {"added": [], "removed": [], "countChanged": []}}
        assert report["baseline"]["edges"] == report["candidate"]["edges"]
        assert report["baseline"]["edges"] == [{"parent": checkout_group,
                                                "child": {"service": "database", "operation": "select",
                                                          "kind": "client"}, "count": 20}]
        assert report["baseline"]["spanCount"] == 40 and report["candidate"]["spanCount"] == 40
        raw_json_changed = json.dumps(_read("baseline.json")) != json.dumps(_read(name))
        assert raw_json_changed is True
        cases.append({"candidate": name, "rawJsonChanged": raw_json_changed, "normalizedStructuralChanges": 0,
                      "latencyRegressions": 0, "exitCode": result.status})
    result, report = _run("regression.json")
    assert result.status == 1 and report["regression"] is True and report["gate"]["latencyRegressions"] == 1
    assert report["candidate"]["spanCount"] == 60 and report["candidate"]["traceCount"] == 20
    checkout = next(c for c in report["comparisons"] if c["group"]["operation"] == "checkout")
    assert (checkout["baselineP95Ms"], checkout["candidateP95Ms"]) == (100, 150)
    assert (checkout["deltaP95Ms"], checkout["relativeP95Percent"], checkout["regression"]) == (50, 50, True)
    audit = {"service": "shop", "operation": "audit", "kind": "internal"}
    assert report["diff"]["operations"]["added"] == [audit]
    assert report["diff"]["operations"]["removed"] == []
    assert report["diff"]["edges"]["added"] == [{"parent": checkout_group, "child": audit}]
    assert report["diff"]["edges"]["removed"] == [] and report["diff"]["edges"]["countChanged"] == []
    assert report["gate"]["structuralChanges"] == 2
    cases.append({"candidate": "regression.json", "rawJsonChanged": True, "normalizedStructuralChanges": 2,
                  "latencyRegressions": 1,
                  "checkoutP95Ms": {"baseline": 100, "candidate": 150, "delta": 50, "relativePercent": 50},
                  "exitCode": result.status})
    return {"experiment": "synthetic-corpus-mutation-v1", "traceCount": 20, "baselineSpanCount": 40,
            "policy": _read("budget.json"),
            "baselineMethod": "JSON.stringify equality (detects ID/order/format noise)",
            "method": "CLI output checked against direct wire-format counts and independently ranked integer durations",
            "cases": cases, "verified": True,
            "performanceClaim": "None: this measures comparison behavior, not runtime speed or production latency "
                                "improvement."}


if __name__ == "__main__":
    print(json.dumps(run_experiment(), indent=2, ensure_ascii=False))
