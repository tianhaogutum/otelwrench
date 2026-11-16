# SPDX-License-Identifier: Apache-2.0
import json
import math
import re
import sys

import pytest

from otelwrench.tracediff import InputError, compare, render_text, validate_policy
from otelwrench.tracediff.jsontext import UNDEFINED, stringify

from .support import clone, jaeger, jaeger_span, otlp, otlp_span, run, write_json

MIN_VALUE = 5e-324
MAX_VALUE = sys.float_info.max


def capture(count, errors, format="otlp", name="checkout"):
    spans = [{"trace": i + 1, "error": i < errors, "name": name} for i in range(count)]
    return otlp([otlp_span(**s) for s in spans]) if format == "otlp" else jaeger([jaeger_span(**s) for s in spans])


def policy(points, percent=50, min_samples=20):
    return {"errorFractionGate": {"maxIncreasePercent": percent, "maxIncreasePercentagePoints": points,
                                  "minSamples": min_samples}}


def entry(report):
    return report["observationGates"]["errorFraction"][0]


def test_zero_to_positive_stays_infinite_but_passes_within_an_absolute_allowance():
    b, c = capture(200, 0), capture(200, 1, "jaeger")
    relative_only = compare(b, c, {"errorFractionGate": {"maxIncreasePercent": 50}})
    assert entry(relative_only)["failed"] is True and entry(relative_only)["reason"] == "zero-baseline-increase"
    for key in ("changePercentagePoints", "absoluteExceeded", "relativeExceeded"):
        assert key not in entry(relative_only)
    for points, reason, failed in ((1, "within-budget", False), (0.5, "at-threshold", False),
                                   (0.49, "zero-baseline-increase", True), (0, "zero-baseline-increase", True)):
        report = compare(b, c, policy(points))
        e = entry(report)
        assert report["gate"]["failed"] is failed and report["regression"] is failed
        assert report["gate"]["errorFractionGateFailures"] == int(failed)
        assert e["changePercentagePoints"] == 0.5 and e["changePercent"] is None
        assert e["relativeState"] == "infinite"
        assert e["absoluteExceeded"] is (points < 0.5) and e["relativeExceeded"] is True
        assert e["reason"] == reason and e["failed"] is failed
        assert entry(json.loads(stringify(report))) == e


@pytest.mark.parametrize("points,percent,absolute_exceeded,relative_exceeded,reason,failed", [
    (4.99, 49.99, True, True, "threshold-exceeded", True),
    (5, 49.99, False, True, "at-threshold", False),
    (4.99, 50, True, False, "at-threshold", False),
    (5, 50, False, False, "at-threshold", False),
    (5, 51, False, False, "within-budget", False),
    (6, 49.99, False, True, "within-budget", False),
    (4.99, 51, True, False, "within-budget", False),
])
def test_both_relative_and_percentage_point_budgets_must_be_strictly_exceeded(
        points, percent, absolute_exceeded, relative_exceeded, reason, failed):
    # 2/20 -> 6/40 = 10% -> 15%: +5 points and +50% relative.
    report = compare(capture(20, 2), capture(40, 6), policy(points, percent))
    e = entry(report)
    assert e["changePercentagePoints"] == 5 and e["changePercent"] == 50
    assert e["absoluteExceeded"] is absolute_exceeded and e["relativeExceeded"] is relative_exceeded
    assert e["reason"] == reason and e["failed"] is failed and report["gate"]["failed"] is failed


@pytest.mark.parametrize("base_n,base_errors,next_n,next_errors,points,failed", [
    (100, 3, 100, 4, 1, False), (100, 3, 100, 4, 0.9999999999999999, True),
    (32, 1, 32, 2, 3.125, False), (32, 1, 32, 2, 3.1249999999999996, True),
    (20, 0, 20, 1, 1e-7, True), (20, 0, 20, 1, 1e21, False),
    (20, 0, 20, 1, MIN_VALUE, True), (20, 0, 20, 1, MAX_VALUE, False),
])
def test_percentage_point_decisions_use_exact_ratios_at_decimal_and_exponent_boundaries(
        base_n, base_errors, next_n, next_errors, points, failed):
    e = entry(compare(capture(base_n, base_errors), capture(next_n, next_errors), policy(points, 0)))
    assert e["failed"] is failed and e["absoluteExceeded"] is failed


@pytest.mark.parametrize("base_n,base_errors,next_n,next_errors", [
    (20, 0, 20, 0), (20, 0, 20, 1), (20, 1, 20, 0), (20, 2, 40, 4), (20, 2, 40, 6), (20, 2, 20, 4)])
def test_zero_absolute_allowance_preserves_relative_only_decisions_including_improvements(
        base_n, base_errors, next_n, next_errors):
    b, c = capture(base_n, base_errors), capture(next_n, next_errors)
    old = compare(b, c, {"errorFractionGate": {"maxIncreasePercent": 50}})
    combined = compare(b, c, policy(0))
    assert combined["gate"] == old["gate"]
    e = dict(entry(combined))
    assert e["changePercentagePoints"] == (next_errors * base_n - base_errors * next_n) / (base_n * next_n) * 100
    assert e["absoluteExceeded"] is (next_errors * base_n > base_errors * next_n)
    for key in ("changePercentagePoints", "absoluteExceeded", "relativeExceeded"):
        del e[key]
    assert e == entry(old)


def test_presence_and_sample_checks_take_precedence_other_gates_remain_independent():
    low = entry(compare(capture(19, 0), capture(20, 1), policy(0)))
    assert low["eligible"] is False and low["failed"] is False and low["reason"] == "insufficient-samples"
    assert low["changePercentagePoints"] == 5
    assert low["absoluteExceeded"] is True and low["relativeExceeded"] is True
    candidate_low = entry(compare(capture(20, 0), capture(19, 1), policy(0)))
    assert candidate_low["reason"] == "insufficient-samples" and candidate_low["failed"] is False
    unpaired = compare(capture(20, 0), capture(20, 1, "otlp", "other"), policy(0))
    assert unpaired["gate"]["failed"] is False
    for e in unpaired["observationGates"]["errorFraction"]:
        assert e["changePercentagePoints"] is None
        assert e["absoluteExceeded"] is False and e["relativeExceeded"] is False
        assert e["eligible"] is False and e["failed"] is False and e["relativeState"] == "unpaired"
    independent = compare(capture(20, 0), capture(40, 1), {**policy(100), "countGate": {"maxChangePercent": 0}})
    assert independent["gate"]["errorFractionGateFailures"] == 0
    assert independent["gate"]["countGateFailures"] == 1 and independent["gate"]["failed"] is True


def test_text_exposes_percentage_point_budget_and_change_without_hiding_infinite_relative_change():
    text = render_text(compare(capture(200, 0), capture(200, 1), policy(0.5)))
    assert "tracediff: PASS" in text
    assert "maxIncreasePercentagePoints=0.5 pp" in text
    assert re.search(r"errorFraction at-threshold .*relative=infinite.*delta=0\.5 pp", text)


def test_optional_allowance_normalizes_without_mutating_input_or_changing_relative_only_defaults():
    data = {"errorFractionGate": {"maxIncreasePercent": 50, "maxIncreasePercentagePoints": 0.5}}
    before = clone(data)
    normalized = validate_policy(data)
    assert data == before
    assert normalized["errorFractionGate"] == {**data["errorFractionGate"], "minSamples": 20}
    assert validate_policy({"errorFractionGate": {"maxIncreasePercent": 50}})["errorFractionGate"] == {
        "maxIncreasePercent": 50, "minSamples": 20}


INVALID_POINTS = [-1, math.nan, math.inf, -math.inf, None, "0.5", True, {}, [], UNDEFINED]


@pytest.mark.parametrize("points", INVALID_POINTS, ids=repr)
def test_reject_invalid_absolute_allowance(points):
    with pytest.raises(InputError, match=r"config\.errorFractionGate\.maxIncreasePercentagePoints"):
        validate_policy(policy(points))


def test_absolute_allowance_requires_relative_budget_and_belongs_only_to_error_fraction_gate():
    with pytest.raises(InputError, match="maxIncreasePercent:"):
        validate_policy({"errorFractionGate": {"maxIncreasePercentagePoints": 1}})
    with pytest.raises(InputError, match="unsupported field"):
        validate_policy({"countGate": {"maxChangePercent": 1, "maxIncreasePercentagePoints": 1}})
    with pytest.raises(InputError, match="unsupported field"):
        validate_policy({**policy(1), "maxIncreasePercentagePoints": 1})


def test_cli_zero_to_positive_contract_combined_pass_fail_relative_only_failure_and_config_errors(tmp_path):
    baseline, candidate, config = tmp_path / "baseline.json", tmp_path / "candidate.json", tmp_path / "budget.json"
    write_json(baseline, capture(200, 0))
    write_json(candidate, capture(200, 1, "jaeger"))

    def cli(budget, fmt="json"):
        write_json(config, budget)
        return run([baseline, candidate, "--config", config, "--format", fmt])

    for budget, status, reason in ((policy(0.5), 0, "at-threshold"), (policy(0.49), 1, "zero-baseline-increase"),
                                   ({"errorFractionGate": {"maxIncreasePercent": 50}}, 1, "zero-baseline-increase")):
        r = cli(budget)
        assert r.status == status and r.stderr == ""
        report = json.loads(r.stdout)
        assert report["gate"]["latencyRegressions"] == 0 and report["gate"]["structuralChanges"] == 0
        assert entry(report)["reason"] == reason and entry(report)["relativeState"] == "infinite"
        assert entry(report)["changePercent"] is None
    text = cli(policy(0.5), "text")
    assert text.status == 0 and text.stderr == ""
    assert "maxIncreasePercentagePoints=0.5 pp" in text.stdout
    assert "errorFraction at-threshold " in text.stdout and "delta=0.5 pp" in text.stdout
    for budget in (policy(-1), policy(None), policy("0.5"), {"errorFractionGate": {"maxIncreasePercentagePoints": 0.5}},
                   {"countGate": {"maxChangePercent": 50, "maxIncreasePercentagePoints": 0.5}}):
        r = cli(budget)
        assert r.status == 2 and r.stdout == ""
        error = json.loads(r.stderr)["error"]
        assert error["code"] == "INPUT_ERROR" and "config." in error["message"]
