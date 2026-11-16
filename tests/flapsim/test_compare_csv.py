# SPDX-License-Identifier: Apache-2.0
import copy

import pytest

import otelwrench.flapsim as api
from otelwrench.flapsim import parse_json, to_json

from .support import assert_jitter_csv, cfg, cfgd, csv_rows, example, matrix, series, sort_pairs


def test_compare_csv_independently_exports_jitter_tradeoffs_with_grid_compatible_schema():
    inputs = [example("jitter-query-range.json"), example("baseline.json"), example("candidate.json")]
    before = copy.deepcopy(inputs)
    text = api.compare_csv(*inputs)
    assert_jitter_csv(text)
    grid = api.grid_csv(inputs[0], inputs[1], {"for": ["60s"], "keep_firing_for": ["120s"], "maxEvaluations": 40})
    assert text == grid, "same settings have identical CSV schema and rows"
    assert api.compare_csv(*inputs) == text
    assert inputs == before


def test_compare_csv_retains_signed_millisecond_deltas_and_both_directions_of_null_delay():
    data = matrix(series([[1.001, "2"], [1.061, "2"], [1.121, "0"]]))
    base = cfgd({"evaluationInterval": "60ms", "for": "60ms"})
    candidate = {**base, "for": "0ms"}
    for r in csv_rows(api.compare_csv(data, base, candidate))[2:]:
        assert r["firstFiring"] == "1.001"
        assert r["firstFiringDelaySeconds"] == "-0.06"
        assert r["deltaFiringDurationSeconds"] == "0.06"
        assert r["firingDurationSeconds"] == "0.12"
        assert r["baselineForMs"] == "60"
        assert r["forMs"] == "0"
        assert r["windowStart"] == "1.001"
        assert r["windowEnd"] == "1.121"
    for r in csv_rows(api.compare_csv(data, candidate, base))[2:]:
        assert r["firstFiringDelaySeconds"] == "0.06"
        assert r["deltaFiringDurationSeconds"] == "-0.06"
    for configs in ([base, {**base, "for": "121ms"}], [{**base, "for": "121ms"}, base]):
        assert all(r["firstFiringDelaySeconds"] == "null" for r in csv_rows(api.compare_csv(data, *configs)))


def test_compare_csv_preserves_max_safe_integer_timer_identities_and_empty_capture_aggregates():
    mx = "9007199254740991ms"
    base = cfgd({"for": mx, "keep_firing_for": "1ms", "start": 0, "end": 0})
    candidate = {**base, "for": "1ms", "keep_firing_for": mx}
    rows = csv_rows(api.compare_csv(matrix(), base, candidate))
    assert [[r["forMs"], r["keepFiringForMs"]] for r in rows] == [["9007199254740991", "1"], ["1", "9007199254740991"]]
    assert all(r["baselineForMs"] == "9007199254740991" and r["baselineKeepFiringForMs"] == "1" and r["scope"] == "aggregate"
               and r["seriesCount"] == "0" and r["seriesId"] == "null" and r["firstFiringDelaySeconds"] == "null" for r in rows)


def test_compare_csv_canonical_labels_quote_exactly_open_and_pending_remain_distinct():
    labels = parse_json('{"__proto__":"=SUM(1,2)","__name__":"測定","instance":"a,\\"b\\"\\r\\nc","10":"@x","2":"+x"}')
    data = matrix(series([2], labels), series([2], {}))
    before = copy.deepcopy(data)
    base, candidate = cfg(), cfgd({"for": "1ms"})
    text = api.compare_csv(data, base, candidate)
    rows = csv_rows(text)
    pairs = sort_pairs(labels)
    expected_ids = sorted([to_json(pairs), "[]"], key=lambda s: s.encode("utf-16-be", "surrogatepass"))
    assert [r["seriesId"] for r in rows if r["scope"] == "series"] == [*expected_ids, *expected_ids]
    assert parse_json(next(i for i in expected_ids if i != "[]")) == pairs
    aggregates = [r for r in rows if r["scope"] == "aggregate"]
    assert [[r["firingEpisodes"], r["resolutions"], r["openAtEnd"], r["pendingAtEnd"]] for r in aggregates] == \
        [["2", "0", "2", "0"], ["0", "0", "0", "2"]]
    assert aggregates[1]["deltaOpenAtEnd"] == "-2"
    assert aggregates[1]["deltaPendingAtEnd"] == "2"
    assert aggregates[1]["deltaTransitionCount"] == "0"
    assert all(r["openAtEnd"] == "0" and r["pendingAtEnd"] == "1" and r["deltaOpenAtEnd"] == "-1" and r["deltaPendingAtEnd"] == "1"
               for r in rows if r["scope"] == "series" and r["role"] == "candidate")
    assert data == before
    data["data"]["result"].reverse()
    assert api.compare_csv(data, base, candidate) == text


def test_compare_csv_carries_each_explicit_threshold_policy_range_source_with_identical_grid():
    data = matrix(series([2, 0]))
    base = cfg()
    candidate = cfg(threshold={"op": "<=", "value": 0}, missingData="error", start=0, end=60)
    rows = csv_rows(api.compare_csv(data, base, candidate))
    assert [[r["thresholdJson"], r["missingData"], r["rangeSource"]] for r in rows] == [
        *[['{"op":">","value":1}', "false", "sample-extrema"]] * 2,
        *[['{"op":"<=","value":0}', "error", "config"]] * 2]
    assert rows[2]["openAtEnd"] == "1"
    assert rows[2]["firstFiringDelaySeconds"] == "60"


def test_compare_csv_preserves_diagnostic_counts_and_shares_compare_validation_and_bounds():
    data = matrix(series([[0, "NaN"], [120, "2"]]))
    base = cfg(threshold={"op": "!=", "value": 0})
    rows = csv_rows(api.compare_csv(data, base, base))
    assert all(r["missingSamples"] == "1" and r["nanSamples"] == "1" for r in rows)
    for configs in ([{**base, "missingData": "error"}, base], [base, {**base, "missingData": "error"}]):
        with pytest.raises(Exception, match="missing sample at 60"):
            api.compare_csv(data, *configs)
    with pytest.raises(Exception, match="identical evaluation grids and windows"):
        api.compare_csv(matrix(series([2])), cfg(start=0, end=60), cfg(start=0, end=120))
    with pytest.raises(Exception, match="identical evaluation grids and windows"):
        api.compare_csv(matrix(series([2])), cfg(), cfg(evaluationInterval="30s"))
    with pytest.raises(Exception, match="1000000 series-evaluations"):
        api.compare_csv(matrix(), cfg(start=0, end=1000000, evaluationInterval="1s"), cfg())
    with pytest.raises(Exception, match="nonnegative duration"):
        api.compare_csv(matrix(series([2])), cfg(), cfgd({"for": "-1s"}))
