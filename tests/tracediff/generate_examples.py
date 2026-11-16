# SPDX-License-Identifier: Apache-2.0
"""Deterministic generator for ``examples/tracediff``: fixed IDs/timestamps, no randomness.

The tests check that every committed example equals what this generator builds.
Run ``python -m tests.tracediff.generate_examples`` from a repository checkout to rewrite them.
"""
from __future__ import annotations

from .support import EXAMPLES, jaeger, jaeger_span, otlp, otlp_span


def create_examples() -> dict:
    def otlp_corpus(renamed: bool) -> dict:
        roots, children = [], []
        for i in range(20):
            trace, span = i + (1001 if renamed else 1), 101 if renamed else 1
            start = 1700000000000000000 + i * 1000000000
            roots.append(otlp_span(trace=trace, span=span, ms=100, error=i < 2, start=start))
            children.append(otlp_span(trace=trace, span=span + 1, parent=span, name="select", kind=3, ms=40,
                                      start=start + 10000000))
        capture = otlp(roots[::-1] if renamed else roots, "shop")
        capture["resourceSpans"].extend(otlp(children[::-1] if renamed else children, "database")["resourceSpans"])
        if renamed:
            capture["resourceSpans"].reverse()
        return capture

    def jaeger_corpus(mutated: bool, observations: bool = False) -> dict:
        spans = []
        for i in range(30 if observations else 20):
            trace = i + 2001
            root = jaeger_span(trace=trace, span=201, ms=150 if mutated else 100, error=i < (6 if observations else 2))
            child = jaeger_span(trace=trace, span=202, parent=201, name="select", kind="client", ms=40, processID="p2")
            spans.extend([child, root])
            if mutated:
                spans.append(jaeger_span(trace=trace, span=203, parent=201, name="audit", kind="internal", ms=5))
        capture = jaeger(spans[::-1], "shop")
        for trace in capture["data"]:
            trace["processes"]["p2"] = {"serviceName": "database", "tags": []}
        return capture

    witness_baseline = otlp([otlp_span()], "shop")
    witness_baseline["resourceSpans"].extend(
        otlp([otlp_span(span=2, parent=1, name="query", kind=3, ms=10)], "database")["resourceSpans"])
    witness_candidate = jaeger([
        jaeger_span(trace=9, span=13, parent=11, name="query", kind="client", ms=10, processID="p2"),
        jaeger_span(trace=9, span=12, parent=11, name="query", kind="client", ms=10, processID="p2"),
        jaeger_span(trace=9, span=11),
    ], "shop")
    witness_candidate["data"][0]["processes"]["p2"] = {"serviceName": "database", "tags": []}
    return {
        "witness-query-path.json": [
            {"service": "shop", "operation": "checkout", "kind": "server"},
            {"service": "database", "operation": "query", "kind": "client"},
        ],
        "witness-baseline.json": witness_baseline,
        "witness-candidate.json": witness_candidate,
        "baseline.json": otlp_corpus(False),
        "renamed.json": otlp_corpus(True),
        "compatible.json": jaeger_corpus(False),
        "regression.json": jaeger_corpus(True),
        "observation-candidate.json": jaeger_corpus(False, True),
        "observation-budget.json": {"countGate": {"maxChangePercent": 20, "minSamples": 20},
                                    "errorFractionGate": {"maxIncreasePercent": 50, "minSamples": 20}},
        "error-fraction-budget.json": {"errorFractionGate": {"maxIncreasePercent": 50,
                                                             "maxIncreasePercentagePoints": 10, "minSamples": 20}},
        "count-absolute-budget.json": {"countGate": {"maxChangePercent": 0, "maxChangeCount": 10, "minSamples": 20}},
        "budget.json": {"absoluteP95Ms": 20, "relativeP95Percent": 20, "minSamples": 20, "failOnStructure": False},
    }


if __name__ == "__main__":
    from otelwrench.tracediff.jsontext import stringify

    examples = create_examples()
    for name, data in examples.items():
        (EXAMPLES / name).write_text(stringify(data, 2) + "\n", encoding="utf-8")
    print(f"Generated {len(examples)} deterministic synthetic example files.")
