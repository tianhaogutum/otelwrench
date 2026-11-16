# SPDX-License-Identifier: Apache-2.0
import pytest

from otelwrench.tracediff import InputError, compare, normalize

from .support import clone, jaeger, jaeger_span, otlp, otlp_span


def test_otlp_versus_jaeger_multitrace_counts_nearest_rank_p95_and_error_fraction():
    args = [{"trace": i + 1, "ms": (i + 1) * 10, "error": i < 4} for i in range(20)]
    report = compare(otlp([otlp_span(**a) for a in args]), jaeger([jaeger_span(**a) for a in args]))
    assert report["regression"] is False
    assert report["baseline"]["groups"] == report["candidate"]["groups"]
    assert report["baseline"]["traceCount"] == 20
    assert report["baseline"]["groups"][0] == {
        "group": {"service": "shop", "operation": "checkout", "kind": "server"}, "count": 20, "errorCount": 4,
        "errorFraction": 0.2, "durationMs": {"min": 10, "median": 100, "p95": 190, "max": 200}}


def test_known_100_to_150_ms_gate_requires_both_thresholds():
    def capture(ms):
        return otlp([otlp_span(trace=i + 1, ms=ms) for i in range(20)])
    report = compare(capture(100), capture(150))
    assert report["regression"] is True
    assert report["comparisons"][0]["deltaP95Ms"] == 50 and report["comparisons"][0]["relativeP95Percent"] == 50
    assert compare(capture(100), capture(150), {"absoluteP95Ms": 50})["regression"] is False
    assert compare(capture(100), capture(150), {"relativeP95Percent": 50})["regression"] is False


def test_exact_duplicates_deduplicate_metadata_conflicts_fail():
    s = otlp_span()
    assert normalize(otlp([s, clone(s)]))["duplicateSpanCount"] == 1
    with pytest.raises(InputError, match="conflicting duplicate"):
        normalize(otlp([s, {**s, "name": "different"}]))


def test_cycles_and_reversed_time_fail():
    with pytest.raises(InputError, match="cyclic"):
        normalize(otlp([otlp_span(parent=1)]))
    with pytest.raises(InputError, match="reversed"):
        normalize(otlp([{**otlp_span(), "endTimeUnixNano": "0"}]))
