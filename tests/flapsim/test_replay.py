# SPDX-License-Identifier: Apache-2.0
import math

import pytest

from otelwrench.flapsim import replay, to_json

from .support import cfg, cfgd, example, matrix, series, states
from otelwrench.flapsim.jsonparse import parse_json


def test_real_matrix_example_pending_at_60_firing_at_120_false_starts_hold():
    result = replay(example("query-range.json"), example("alert.json"))
    assert [x["state"] for x in result["series"][0]["timeline"]] == \
        ["inactive", "pending", "firing", "firing", "firing", "firing", "firing"]
    assert [x["holdSince"] for x in result["series"][0]["timeline"]] == [None, None, None, 180, None, None, 360]
    assert result["summary"]["firstFiring"] == 120
    assert result["summary"]["firingDurationSeconds"] == 240
    assert result["summary"]["resolutions"] == 0
    assert result["summary"]["openAtEnd"] == 1


def test_zero_for_and_hold_immediate_firing_and_resolution_no_invented_final_duration():
    r = replay(matrix(series([2, 0, 2, 0, 2])), cfg())
    assert states(r) == ["firing", "inactive", "firing", "inactive", "firing"]
    assert r["summary"]["firstFiring"] == 0
    assert r["summary"]["firingEpisodes"] == 3
    assert r["summary"]["resolutions"] == 2
    assert r["summary"]["firingDurationSeconds"] == 120
    assert r["series"][0]["episodes"][-1] == {"start": 240, "end": None, "durationSeconds": 0, "censored": True}


def test_pending_false_resets_start_and_hold_never_retains_pending():
    r = replay(matrix(series([2, 0, 2, 2, 2, 0])), cfgd({"for": "120s", "keep_firing_for": "600s"}))
    assert states(r) == ["pending", "inactive", "pending", "pending", "firing", "firing"]
    assert [x["pendingSince"] for x in r["series"][0]["timeline"]] == [0, None, 120, 120, 120, 120]
    assert r["summary"]["firstFiring"] == 240


def test_exact_hold_boundary_timer_starts_on_first_false_resolves_at_duration():
    r = replay(matrix(series([2, 0, 0, 0, 0])), cfg(keep_firing_for="120s"))
    assert states(r) == ["firing", "firing", "firing", "inactive", "inactive"]
    assert [[x["timestamp"], x["to"]] for x in r["series"][0]["transitions"]] == [[0, "firing"], [180, "inactive"]]
    assert r["summary"]["firingDurationSeconds"] == 180
    assert r["summary"]["heldEvaluations"] == 2


def test_true_resumes_exactly_on_hold_boundary_and_clears_timer_without_new_for():
    r = replay(matrix(series([2, 2, 0, 0, 2, 0, 0, 0])), cfgd({"for": "60s", "keep_firing_for": "120s"}))
    assert states(r) == ["pending", "firing", "firing", "firing", "firing", "firing", "firing", "inactive"]
    assert [x["holdSince"] for x in r["series"][0]["timeline"]] == [None, None, 120, 120, None, 300, 300, None]
    assert r["summary"]["firingEpisodes"] == 1
    assert r["summary"]["resolutions"] == 1
    assert r["summary"]["firingDurationSeconds"] == 360


def test_for_and_hold_not_divisible_by_interval_take_effect_at_next_evaluation():
    r = replay(matrix(series([2, 2, 2, 0, 0, 0])), cfgd({"for": "61s", "keep_firing_for": "61s"}))
    assert states(r) == ["pending", "pending", "firing", "firing", "firing", "inactive"]
    assert r["summary"]["firstFiring"] == 120


def test_millisecond_grid_and_decimal_durations_use_exact_duration_arithmetic():
    r = replay(matrix(series([[0, "2"], [0.06, "2"], [0.12, "2"], [0.18, "0"], [0.24, "0"]])),
               cfgd({"evaluationInterval": "60ms", "for": "0.001m", "keep_firing_for": "0.001m"}))
    assert states(r) == ["pending", "firing", "firing", "firing", "inactive"]
    assert r["summary"]["firingDurationSeconds"] == 0.18


def test_multiple_series_have_separate_state_and_canonical_deterministic_label_identities():
    a = series([2, 2, 0], {"zone": "west", "instance": "a"})
    b = series([0, 2, 2], {"instance": "b", "zone": "east"})
    r = replay(matrix(a, b), cfgd({"for": "60s"}))
    reordered = replay(matrix({**b, "metric": {"zone": "east", "instance": "b"}},
                              {**a, "metric": {"instance": "a", "zone": "west"}}), cfgd({"for": "60s"}))
    assert to_json(r) == to_json(reordered)
    assert [x["summary"]["firstFiring"] for x in r["series"]] == [60, 120]
    assert r["summary"]["firingDurationSeconds"] == 60
    assert r["summary"]["firingEpisodes"] == 2
    assert r["summary"]["resolutions"] == 1


def test_labels_are_preserved_even_for_proto_unicode_and_separator_characters():
    metric = parse_json('{"__proto__":"x","__name__":"m","☃":"a=b,\\"c"}')
    r = replay(matrix(series([2], metric)), cfg())
    assert r["series"][0]["labels"] == metric
    assert type(r["series"][0]["labels"]) is dict


def test_gaps_reset_pending_and_run_hold_on_explicit_grid_error_policy_refuses_gaps():
    data = matrix(series([[60, "2"], [180, "2"]]))
    c = cfgd({"for": "60s", "start": 0, "end": 240})
    r = replay(data, c)
    assert states(r) == ["inactive", "pending", "inactive", "pending", "inactive"]
    assert r["summary"]["missingSamples"] == 3
    assert r["summary"]["firstFiring"] is None
    with pytest.raises(Exception, match="missing sample at 0"):
        replay(data, {**c, "missingData": "error"})
    hold = replay(matrix(series([[0, "2"], [240, "2"]])), cfg(keep_firing_for="120s"))
    assert states(hold) == ["firing", "firing", "firing", "inactive", "firing"]


def test_series_introduced_late_or_ending_early_uses_the_same_shared_range():
    r = replay(matrix(series([[0, "2"], [60, "2"]], {"id": "early"}),
                      series([[120, "2"], [180, "0"]], {"id": "late"})), cfg())
    assert [s["summary"]["missingSamples"] for s in r["series"]] == [2, 2]
    assert [len(s["timeline"]) for s in r["series"]] == [4, 4]
    assert r["summary"]["resolutions"] == 2


def test_nan_is_ieee_nan_not_absence_infinity_comparisons_are_supported():
    data = matrix(series(["NaN", "+Inf", "-Inf", "Inf", "2e0", "-0"]))
    r = replay(data, cfg(missingData="error"))
    assert states(r) == ["inactive", "firing", "inactive", "firing", "firing", "inactive"]
    assert r["summary"]["nanSamples"] == 1
    assert r["summary"]["missingSamples"] == 0
    assert r["series"][0]["timeline"][0]["value"] == "NaN"
    neq = replay(data, cfg(threshold={"op": "!=", "value": 1}))
    assert neq["series"][0]["timeline"][0]["condition"] is True


@pytest.mark.parametrize("op,expected", [(">", False), (">=", True), ("<", False), ("<=", True), ("==", True), ("!=", False)])
def test_comparison_includes_correct_threshold_equality_behavior(op, expected):
    assert replay(matrix(series([1])), cfg(threshold={"op": op, "value": 1}))["series"][0]["timeline"][0]["condition"] is expected


def test_empty_matrix_and_empty_series_are_explicit_range_never_fabricates_unknown_labels():
    r = replay(matrix(), cfg(start=0, end=120))
    assert r["summary"]["series"] == 0
    assert r["summary"]["firstFiring"] is None
    assert r["window"]["evaluations"] == 3
    empty = replay(matrix(series([])), cfg(start=0, end=120))
    assert states(empty) == ["inactive", "inactive", "inactive"]
    assert empty["summary"]["missingSamples"] == 3


def test_api_warnings_and_infos_are_retained_not_silently_discarded():
    data = {**matrix(series([0])), "warnings": ["partial data"], "infos": ["annotation"]}
    r = replay(data, cfg())
    assert r["sourceWarnings"] == ["partial data"]
    assert r["sourceInfos"] == ["annotation"]


def test_capture_boundary_censors_both_firing_and_pending_state_without_fake_resolutions():
    r = replay(matrix(series([2, 2])), cfgd({"for": "60s"}))
    assert r["summary"]["openAtEnd"] == 1
    assert r["summary"]["firingDurationSeconds"] == 0
    assert r["summary"]["resolutions"] == 0
    pending = replay(matrix(series([2, 2])), cfgd({"for": "120s"}))
    assert pending["summary"]["pendingAtEnd"] == 1
    assert pending["summary"]["firingEpisodes"] == 0


def test_timestamps_such_as_1_001s_are_valid_ms_despite_binary_float_multiplication_error():
    data = matrix(series([[1.001, "2"], [1.061, "2"], [1.121, "0"]]))
    r = replay(data, cfgd({"evaluationInterval": "60ms", "for": "60ms"}))
    assert r["summary"]["firstFiring"] == 1.061
    assert r["summary"]["firingDurationSeconds"] == 0.06
    epoch = replay(matrix(series([[1700000000.001, "2"], [1700000000.061, "0"]])), cfg(evaluationInterval="60ms"))
    assert epoch["summary"]["firingDurationSeconds"] == 0.06


def test_millisecond_aggregate_exposure_avoids_floating_point_drift():
    r = replay(matrix(series([[0, "2"], [0.1, "0"], [0.2, "0"]], {"id": "a"}),
                      series([[0, "2"], [0.1, "2"], [0.2, "0"]], {"id": "b"})), cfg(evaluationInterval="100ms"))
    assert r["summary"]["firingDurationSeconds"] == 0.3
    assert not math.isclose(0.1 + 0.2, 0.3, rel_tol=0, abs_tol=0)
