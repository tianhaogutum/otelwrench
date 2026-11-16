# SPDX-License-Identifier: Apache-2.0
import json
import math
import re

import pytest

from otelwrench.tracediff import InputError, compare, render_text, validate_policy
from otelwrench.tracediff.jsontext import UNDEFINED, stringify

from .support import MAX_SAFE, clone, jaeger, jaeger_span, otlp, otlp_span, run, write_json


def capture(n, format="otlp", name="checkout", errors=0, ms=100):
    spans = [{"trace": i + 1, "name": name, "error": i < errors, "ms": ms} for i in range(n)]
    return otlp([otlp_span(**s) for s in spans]) if format == "otlp" else jaeger([jaeger_span(**s) for s in spans])


def policy(count, percent=0, min_samples=20):
    return {"countGate": {"maxChangePercent": percent, "maxChangeCount": count, "minSamples": min_samples}}


def entry(r):
    return r["observationGates"]["count"][0]


@pytest.mark.parametrize("b,c,failed", [(20, 21, False), (21, 20, False), (20, 22, True), (22, 20, True)])
def test_absolute_count_allowance_passes_equality_and_fails_larger_magnitudes_in_either_direction(b, c, failed):
    r = compare(capture(b), capture(c, format="jaeger"), policy(1))
    e = entry(r)
    assert r["gate"]["failed"] is failed and r["regression"] is failed
    assert r["gate"]["countGateFailures"] == int(failed)
    assert r["gate"]["latencyRegressions"] == 0 and r["gate"]["structuralChanges"] == 0
    assert e["changeCount"] == c - b and e["changePercent"] == (c - b) / b * 100
    assert e["relativeState"] == "finite" and e["relativeExceeded"] is True
    assert e["absoluteExceeded"] is failed and e["eligible"] is True and e["failed"] is failed
    assert e["reason"] == ("threshold-exceeded" if failed else "at-threshold")
    assert entry(json.loads(stringify(r))) == e


@pytest.mark.parametrize("b,c,count,percent,absolute,relative,reason,failed", [
    (20, 22, 1, 10, True, False, "at-threshold", False),
    (22, 20, 1, 10, True, False, "within-budget", False),
    (20, 22, 1, 9.5, True, True, "threshold-exceeded", True),
    (22, 20, 1, 9.5, True, False, "within-budget", False),
    (20, 22, 2, 10, False, False, "at-threshold", False),
    (20, 22, 2, 11, False, False, "within-budget", False),
    (20, 22, 3, 9, False, True, "within-budget", False),
    (20, 22, 3, 10, False, False, "within-budget", False),
    (32, 31, 0, 3.125, True, False, "at-threshold", False),
    (32, 31, 0, 3.1249999999999996, True, True, "threshold-exceeded", True),
    (20, 21, 0, 1e-7, True, True, "threshold-exceeded", True),
    (20, 21, MAX_SAFE, 0, False, True, "within-budget", False),
])
def test_both_budgets_must_be_exceeded_count_magnitude_is_symmetric_but_relative_denominator_stays_baseline(
        b, c, count, percent, absolute, relative, reason, failed):
    e = entry(compare(capture(b), capture(c), policy(count, percent)))
    assert e["changeCount"] == c - b and e["changePercent"] == (c - b) / b * 100
    assert e["absoluteExceeded"] is absolute and e["relativeExceeded"] is relative
    assert e["reason"] == reason and e["failed"] is failed


@pytest.mark.parametrize("b,c,percent", [(20, 20, 0), (20, 21, 0), (21, 20, 0), (20, 22, 10), (22, 20, 10)])
def test_omission_preserves_relative_only_shape_and_zero_allowance_preserves_its_decisions(b, c, percent):
    baseline, candidate = capture(b), capture(c)
    old = compare(baseline, candidate, {"countGate": {"maxChangePercent": percent}})
    assert old["policy"]["countGate"] == {"minSamples": 20, "maxChangePercent": percent}
    assert sorted(entry(old)) == sorted(["group", "baselineCount", "candidateCount", "baselineValue", "candidateValue",
                                         "changePercent", "relativeState", "eligible", "failed", "reason"])
    assert not re.search(r"maxChangeCount|absoluteExceeded|relativeExceeded", render_text(old))
    combined = compare(baseline, candidate, policy(0, percent))
    e = dict(entry(combined))
    assert combined["gate"] == old["gate"]
    for key in ("changeCount", "absoluteExceeded", "relativeExceeded"):
        del e[key]
    assert e == entry(old)


def test_samples_and_presence_take_precedence_while_measured_paired_evidence_remains_visible():
    for b, c in ((19, 22), (22, 19)):
        e = entry(compare(capture(b), capture(c), policy(1)))
        assert e["reason"] == "insufficient-samples" and e["eligible"] is False and e["failed"] is False
        assert e["changeCount"] == c - b and e["absoluteExceeded"] is True and e["relativeExceeded"] is True
        assert entry(compare(capture(b), capture(c), policy(1, 0, 19)))["failed"] is True
    r = compare(capture(20), capture(22, name="other"), policy(0, 0, 1))
    assert r["gate"]["failed"] is False
    assert [e["reason"] for e in r["observationGates"]["count"]] == ["removed-group", "new-group"]
    for e in r["observationGates"]["count"]:
        assert e["changeCount"] is None and e["changePercent"] is None and e["relativeState"] == "unpaired"
        assert e["absoluteExceeded"] is False and e["relativeExceeded"] is False
        assert e["eligible"] is False and e["failed"] is False
    assert "delta=unpaired absoluteExceeded=false relativeExceeded=false" in render_text(r)
    assert compare(capture(20), capture(22, name="other"), {**policy(0), "failOnStructure": True})["gate"]["failed"]


def test_latency_count_and_error_gates_retain_independent_minima_and_decisions():
    r = compare(capture(20), capture(21, errors=1, ms=150), {**policy(1), "errorFractionGate": {"maxIncreasePercent": 0}})
    assert r["gate"]["countGateFailures"] == 0 and r["gate"]["errorFractionGateFailures"] == 1
    assert r["gate"]["latencyRegressions"] == 1 and r["gate"]["failed"] is True
    count_only = compare(capture(19), capture(21, errors=1, ms=150), {
        **policy(1, 0, 19), "minSamples": 22, "errorFractionGate": {"maxIncreasePercent": 0, "minSamples": 20}})
    assert count_only["gate"]["countGateFailures"] == 1 and count_only["gate"]["errorFractionGateFailures"] == 0
    assert count_only["gate"]["latencyRegressions"] == 0 and count_only["gate"]["failed"] is True


@pytest.mark.parametrize("b,c,delta", [(20, 21, 1), (21, 20, -1)])
def test_count_text_exposes_signed_delta_budget_and_both_decision_flags(b, c, delta):
    text = render_text(compare(capture(b), capture(c), policy(1)))
    assert "tracediff: PASS" in text
    assert "count gate: maxChangePercent=0% AND maxChangeCount=1 spans; minSamples=20" in text
    assert re.search(rf"count at-threshold .*delta={delta} spans absoluteExceeded=false relativeExceeded=true", text)


@pytest.mark.parametrize("count", [0, 1, MAX_SAFE])
def test_count_allowance_normalizes_without_mutation_accepting_zero_and_largest_safe_integer(count):
    data = {"countGate": {"maxChangePercent": 0, "maxChangeCount": count}}
    before = clone(data)
    assert validate_policy(data)["countGate"] == {**data["countGate"], "minSamples": 20}
    assert data == before


INVALID_COUNTS = [None, -1, 0.5, math.nan, math.inf, -math.inf, MAX_SAFE + 1, "1", True, {}, [], UNDEFINED]


@pytest.mark.parametrize("count", INVALID_COUNTS, ids=repr)
def test_reject_invalid_absolute_count_allowance(count):
    with pytest.raises(InputError, match=r"config\.countGate\.maxChangeCount"):
        validate_policy(policy(count))


@pytest.mark.parametrize("config", [
    {"countGate": {"maxChangeCount": 1}}, {**policy(1), "maxChangeCount": 1},
    {"errorFractionGate": {"maxIncreasePercent": 0, "maxChangeCount": 1}},
    {"countGate": {**policy(1)["countGate"], "extra": 1}}])
def test_count_allowance_requires_relative_budget_and_rejects_misplaced_and_unknown_keys(config):
    with pytest.raises(InputError, match=r"config\."):
        validate_policy(config)


def test_cli_count_allowance_json_text_exits_0_1_and_invalid_json_policies_exit_2(tmp_path):
    baseline, candidate, config = tmp_path / "b.json", tmp_path / "c.json", tmp_path / "p.json"

    def cli(budget, b=20, c=21, fmt="json"):
        write_json(baseline, capture(b))
        write_json(candidate, capture(c, format="jaeger"))
        if isinstance(budget, str):
            config.write_text(budget)
        else:
            write_json(config, budget)
        return run([baseline, candidate, "--config", config, "--format", fmt])

    for b, c, status in ((20, 21, 0), (21, 20, 0), (20, 22, 1), (22, 20, 1)):
        r = cli(policy(1), b, c)
        assert r.status == status and r.stderr == ""
        report = json.loads(r.stdout)
        assert entry(report)["changeCount"] == c - b and entry(report)["failed"] is (status == 1)
    text = cli(policy(1), 21, 20, "text")
    assert text.status == 0 and text.stderr == ""
    assert "delta=-1 spans absoluteExceeded=false relativeExceeded=true" in text.stdout
    for budget in (policy(None), policy(-1), policy(0.5), policy("1"), policy(MAX_SAFE + 1),
                   '{"countGate":{"maxChangePercent":0,"maxChangeCount":1e999}}',
                   '{"countGate":{"maxChangePercent":0,"maxChangeCount":1,"maxChangeCount":2}}',
                   {"countGate": {"maxChangeCount": 1}}, {**policy(1), "maxChangeCount": 1},
                   {"errorFractionGate": {"maxIncreasePercent": 0, "maxChangeCount": 1}},
                   {"countGate": {**policy(1)["countGate"], "extra": 1}}):
        r = cli(budget)
        assert r.status == 2 and r.stdout == ""
        assert json.loads(r.stderr)["error"]["code"] == "INPUT_ERROR"
