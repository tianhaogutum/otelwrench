# SPDX-License-Identifier: Apache-2.0
import json
import math
import re

import pytest

from otelwrench.tracediff import InputError, compare, render_text, validate_policy
from otelwrench.tracediff.jsontext import stringify

from .support import MAX_SAFE, jaeger, jaeger_span, otlp, otlp_span


def capture(n, errors=0, ms=100, name="checkout", format="otlp"):
    args = [{"trace": i + 1, "error": i < errors, "ms": ms, "name": name} for i in range(n)]
    return otlp([otlp_span(**a) for a in args]) if format == "otlp" else jaeger([jaeger_span(**a) for a in args])


CONFIG = {"countGate": {"maxChangePercent": 20}, "errorFractionGate": {"maxIncreasePercent": 50}}


def test_opt_in_observation_gates_fail_independently_of_unchanged_latency_across_otlp_jaeger():
    r = compare(capture(20, 2), capture(30, 6, 100, "checkout", "jaeger"), CONFIG)
    assert r["gate"]["failed"] is True and r["gate"]["latencyRegressions"] == 0
    assert r["comparisons"][0]["reason"] == "within-budget"
    assert r["gate"]["countGateFailures"] == 1 and r["gate"]["errorFractionGateFailures"] == 1
    assert r["observationGates"]["count"][0]["changePercent"] == 50
    assert r["observationGates"]["errorFraction"][0]["changePercent"] == 100
    assert r["observationGates"]["count"][0]["reason"] == "threshold-exceeded"
    assert r["observationGates"]["errorFraction"][0]["reason"] == "threshold-exceeded"
    assert re.search(r"observational.*causal", render_text(r), re.I)
    assert re.search(r"count.*threshold-exceeded", render_text(r))


def test_absent_gates_preserve_the_complete_latency_only_report_and_policy_shape():
    r = compare(capture(20, 0), capture(40, 40))
    assert r["gate"]["failed"] is False and "observationGates" not in r
    assert r["policy"] == {"absoluteP95Ms": 20, "relativeP95Percent": 20, "minSamples": 20, "failOnStructure": False}
    assert r["gate"] == {"failed": False, "latencyRegressions": 0, "structuralChanges": 0, "insufficientSampleGroups": 0}


@pytest.mark.parametrize("base,candidate,threshold,reason,failed", [
    (20, 24, 20, "at-threshold", False), (24, 20, 16, "threshold-exceeded", True),
    (20, 30, 50, "at-threshold", False), (20, 30, 49.9999, "threshold-exceeded", True),
    (32, 31, 3.125, "at-threshold", False), (32, 31, 3.124999999, "threshold-exceeded", True),
    (20, 20, 0, "at-threshold", False), (20, 21, 10, "within-budget", False)])
def test_strict_exact_count_thresholds_gate_increases_and_decreases_including_decimal_boundaries(
        base, candidate, threshold, reason, failed):
    r = compare(capture(base), capture(candidate), {"countGate": {"maxChangePercent": threshold}})
    assert r["observationGates"]["count"][0]["reason"] == reason
    assert r["gate"]["failed"] is failed


def test_error_fraction_uses_rates_with_unequal_sample_counts_not_raw_error_counts():
    equal = compare(capture(20, 2), capture(40, 4), {"errorFractionGate": {"maxIncreasePercent": 0}})
    assert equal["observationGates"]["errorFraction"][0]["reason"] == "at-threshold"
    assert equal["gate"]["failed"] is False
    boundary = compare(capture(20, 2), capture(40, 6), {"errorFractionGate": {"maxIncreasePercent": 50}})
    assert boundary["observationGates"]["errorFraction"][0]["reason"] == "at-threshold"
    assert boundary["observationGates"]["errorFraction"][0]["changePercent"] == 50
    assert boundary["gate"]["failed"] is False
    fail = compare(capture(20, 2), capture(40, 6), {"errorFractionGate": {"maxIncreasePercent": 49.99999999}})
    assert fail["gate"]["failed"] is True
    assert fail["observationGates"]["errorFraction"][0]["reason"] == "threshold-exceeded"
    assert compare(capture(20, 2), capture(20, 0), CONFIG)["gate"]["failed"] is False


def test_zero_baseline_error_fraction_is_infinite_for_a_positive_candidate_and_zero_to_zero_otherwise():
    r = compare(capture(20), capture(20, 1), CONFIG)
    e = r["observationGates"]["errorFraction"][0]
    assert e["changePercent"] is None and e["relativeState"] == "infinite"
    assert e["reason"] == "zero-baseline-increase" and e["failed"] is True
    assert json.loads(stringify(r))["observationGates"]["errorFraction"][0]["changePercent"] is None
    zero = compare(capture(20), capture(20), CONFIG)["observationGates"]["errorFraction"][0]
    assert zero["relativeState"] == "zero-to-zero" and zero["reason"] == "zero-to-zero"
    assert zero["changePercent"] == 0 and zero["failed"] is False


def test_observation_sample_minima_are_separate_from_latency_and_each_other():
    r = compare(capture(10), capture(20, 2, 150), {
        "minSamples": 30, "countGate": {"maxChangePercent": 20, "minSamples": 10},
        "errorFractionGate": {"maxIncreasePercent": 0, "minSamples": 11}})
    assert r["comparisons"][0]["reason"] == "insufficient-samples"
    assert r["observationGates"]["count"][0]["failed"] is True
    ef = r["observationGates"]["errorFraction"][0]
    assert ef["eligible"] is False and ef["reason"] == "insufficient-samples" and ef["relativeState"] == "infinite"
    assert r["gate"]["errorFractionGateFailures"] == 0
    latency = compare(capture(10), capture(20, 2, 150), {"minSamples": 1, **CONFIG})
    assert latency["gate"]["latencyRegressions"] == 1
    assert latency["observationGates"]["count"][0]["reason"] == "insufficient-samples"
    assert latency["observationGates"]["errorFraction"][0]["reason"] == "insufficient-samples"
    candidate_low = compare(capture(30, 3), capture(19, 10), CONFIG)
    assert candidate_low["observationGates"]["count"][0]["reason"] == "insufficient-samples"
    assert candidate_low["observationGates"]["errorFraction"][0]["reason"] == "insufficient-samples"


def test_new_removed_groups_are_explicitly_unpaired_separate_from_low_samples_and_structure_gating():
    a, b = capture(20, 1, 100, "before"), capture(20, 2, 100, "after")
    r = compare(a, b, CONFIG)
    assert r["gate"]["failed"] is False and len(r["comparisons"]) == 0
    for entries in r["observationGates"].values():
        assert [e["reason"] for e in entries] == ["new-group", "removed-group"]
        for e in entries:
            assert e["relativeState"] == "unpaired" and e["changePercent"] is None
            assert e["eligible"] is False and e["failed"] is False
    assert compare(a, b, {**CONFIG, "failOnStructure": True})["gate"]["failed"] is True


def test_enabled_gates_normalize_independent_defaults_without_mutating_user_config():
    assert validate_policy(CONFIG)["countGate"] == {"maxChangePercent": 20, "minSamples": 20}
    assert CONFIG["countGate"] == {"maxChangePercent": 20}
    only_count = compare(capture(20), capture(20), {"countGate": {"maxChangePercent": 0}})
    assert list(only_count["observationGates"]) == ["count"]


def _invalid_gates():
    for key in ("countGate", "errorFractionGate"):
        t = "maxChangePercent" if key == "countGate" else "maxIncreasePercent"
        for value in (None, [], {}, {t: -1}, {t: math.inf}, {t: "20"}, {t: 1, "minSamples": 0},
                      {t: 1, "minSamples": 1.5}, {t: 1, "minSamples": MAX_SAFE + 1}, {t: 1, "unexpected": True}):
            yield key, value


@pytest.mark.parametrize("key,value", list(_invalid_gates()))
def test_reject_invalid_gate(key, value):
    with pytest.raises(InputError, match="config"):
        validate_policy({key: value})
