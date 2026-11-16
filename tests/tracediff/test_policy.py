# SPDX-License-Identifier: Apache-2.0
import math

import pytest

from otelwrench.tracediff import InputError, compare, validate_policy

from .support import MAX_SAFE, otlp, otlp_span


def capture(ms, n=20):
    return otlp([otlp_span(trace=i + 1, ms=ms) for i in range(n)])


def test_minimum_sample_count_is_required_on_both_sides_inclusive_at_the_minimum():
    assert compare(capture(100, 19), capture(150))["regression"] is False
    assert compare(capture(100), capture(150, 19))["regression"] is False
    assert compare(capture(100, 19), capture(150))["comparisons"][0]["reason"] == "insufficient-samples"
    assert compare(capture(100), capture(150))["regression"] is True


def test_positive_zero_baseline_increase_is_explicitly_infinite_and_still_needs_absolute_budget():
    r = compare(capture(0), capture(50))
    assert r["regression"] is True
    assert r["comparisons"][0]["relativeP95Percent"] is None and r["comparisons"][0]["relativeState"] == "infinite"
    assert compare(capture(0), capture(10))["regression"] is False
    assert compare(capture(0), capture(50, 1))["regression"] is False


def test_zero_to_zero_and_improvements_never_fail():
    r = compare(capture(0), capture(0))
    assert r["regression"] is False and r["comparisons"][0]["relativeP95Percent"] == 0
    assert r["comparisons"][0]["relativeState"] == "zero-to-zero"
    assert compare(capture(100), capture(0), {"absoluteP95Ms": 0, "relativeP95Percent": 0})["regression"] is False


@pytest.mark.parametrize("values,median,p95", [([7], 7, 7), ([1, 9], 1, 9), (list(range(1, 22)), 11, 20)])
def test_nearest_rank_is_defined_for_one_two_and_twenty_one_samples(values, median, p95):
    a = otlp([otlp_span(span=i + 1, ms=ms) for i, ms in enumerate(values)])
    r = compare(a, a)
    assert r["baseline"]["groups"][0]["durationMs"]["median"] == median
    assert r["baseline"]["groups"][0]["durationMs"]["p95"] == p95


def test_nanosecond_timestamp_subtraction_is_exact_at_modern_epoch_magnitudes():
    s = otlp_span()
    s["endTimeUnixNano"] = str(int(s["startTimeUnixNano"]) + 1)
    a = otlp([s])
    assert compare(a, a)["baseline"]["groups"][0]["durationMs"]["p95"] == 0.000001


def test_decimal_threshold_equality_does_not_fail_at_nanosecond_resolution():
    a, b = capture(0.000001), capture(0.000004)
    policy = {"absoluteP95Ms": 0.000003, "relativeP95Percent": 0, "minSamples": 1}
    assert compare(a, b, policy)["regression"] is False
    assert compare(a, b, {**policy, "absoluteP95Ms": 0.0000029})["regression"] is True
    assert compare(a, b, {"absoluteP95Ms": 1e-300, "relativeP95Percent": 1e308, "minSamples": 1})["regression"] is False


@pytest.mark.parametrize("config", [
    {"absoluteP95Ms": math.nan}, {"absoluteP95Ms": math.inf}, {"relativeP95Percent": -1}, {"relativeP95Percent": "20"},
    {"minSamples": 0}, {"minSamples": 1.5}, {"minSamples": MAX_SAFE + 1}, {"failOnStructure": "true"}, {"unknown": 1},
    None, []])
def test_invalid_policy_rejected(config):
    with pytest.raises(InputError, match="config"):
        validate_policy(config)
