# SPDX-License-Identifier: Apache-2.0
import copy

import pytest

import otelwrench.flapsim as api
from otelwrench.flapsim import parse_json, to_json

from .support import cfg, cfgd, csv_rows, example, matrix, series, sort_pairs

INPUT = example("grid-query-range.json")
BASELINE = example("grid-baseline.json")
PARAMETERS = example("timer-grid.json")
COLUMNS = ("schemaVersion,role,candidateIndex,scope,seriesId,baselineForMs,baselineKeepFiringForMs,forMs,keepFiringForMs,"
           "evaluationIntervalMs,thresholdJson,missingData,windowStart,windowEnd,rangeSource,seriesCount,firstFiring,"
           "firingDurationSeconds,firingEpisodes,resolutions,openAtEnd,pendingAtEnd,transitionCount,missingSamples,nanSamples,"
           "heldEvaluations,firstFiringDelaySeconds,deltaFiringDurationSeconds,deltaFiringEpisodes,deltaResolutions,"
           "deltaOpenAtEnd,deltaPendingAtEnd,deltaTransitionCount")


def test_csv_grid_exports_explicit_baseline_and_all_hand_calculated_tradeoffs():
    before = copy.deepcopy([INPUT, BASELINE, PARAMETERS])
    text = api.grid_csv(INPUT, BASELINE, PARAMETERS)
    assert text.startswith(f"{COLUMNS}\r\n")
    rows = csv_rows(text)
    assert len(rows) == 10
    assert [[r["role"], r["candidateIndex"], r["scope"]] for r in rows] == [
        ["baseline", "null", "aggregate"], ["baseline", "null", "series"],
        *[x for i in range(4) for x in (["candidate", str(i), "aggregate"], ["candidate", str(i), "series"])]]
    expected = [
        ["0", "0", "240", "null", "1", "0", "5", "3", "2"],
        ["0", "0", "240", "0", "1", "0", "5", "3", "2"],
        ["0", "120000", "420", "0", "1", "0", "1", "1", "0"],
        ["60000", "0", "120", "60", "0", "1", "7", "2", "2"],
        ["60000", "120000", "360", "60", "1", "0", "2", "1", "0"],
    ]
    for i, r in enumerate(rows):
        assert [r["forMs"], r["keepFiringForMs"], r["firingDurationSeconds"], r["firstFiringDelaySeconds"],
                r["openAtEnd"], r["pendingAtEnd"], r["transitionCount"], r["firingEpisodes"], r["resolutions"]] == expected[i // 2]
        assert r["baselineForMs"] == "0"
        assert r["baselineKeepFiringForMs"] == "0"
        assert r["schemaVersion"] == "1"
        assert r["evaluationIntervalMs"] == "60000"
        assert parse_json(r["thresholdJson"]) == {"op": ">", "value": 1}
        assert r["missingData"] == "false"
        assert r["windowStart"] == "0"
        assert r["windowEnd"] == "420"
        assert r["rangeSource"] == "config"
        assert r["seriesCount"] == "1"
        assert r["missingSamples"] == "1"
        assert r["nanSamples"] == "0"
        assert r["seriesId"] == ("null" if i % 2 == 0 else to_json([["__name__", "synthetic_load"], ["instance", "a"]]))
        if r["role"] == "baseline":
            assert all(v == "null" for k, v in r.items() if k.startswith("delta"))
    assert [[r["deltaFiringDurationSeconds"], r["deltaFiringEpisodes"], r["deltaResolutions"], r["deltaOpenAtEnd"],
             r["deltaPendingAtEnd"], r["deltaTransitionCount"]] for r in rows if r["scope"] == "aggregate"][1:] == [
        ["0", "0", "0", "0", "0", "0"], ["180", "-2", "-2", "0", "0", "-4"],
        ["-120", "-1", "0", "-1", "1", "2"], ["120", "-2", "-2", "0", "0", "-3"]]
    assert api.grid_csv(INPUT, BASELINE, PARAMETERS) == text
    assert [INPUT, BASELINE, PARAMETERS] == before


def test_csv_preserves_signed_millisecond_tradeoffs_user_timer_order_and_null_never_fired_delays():
    data = matrix(series([[1.001, "2"], [1.061, "2"], [1.121, "0"]], {"id": "b"}),
                  series([[1.001, "0"], [1.061, "2"], [1.121, "2"]], {"id": "a"}))
    base = cfgd({"evaluationInterval": "60ms", "for": "60ms"})
    params = {"for": ["61ms", "0ms"], "keep_firing_for": ["61ms", "0ms"], "maxEvaluations": 30}
    rows = csv_rows(api.grid_csv(data, base, params))
    aggregates = [r for r in rows if r["scope"] == "aggregate"]
    assert [[r["forMs"], r["keepFiringForMs"]] for r in aggregates] == [["60", "0"], ["61", "61"], ["61", "0"], ["0", "61"], ["0", "0"]]
    assert [r["firstFiringDelaySeconds"] for r in aggregates] == ["null", "null", "null", "-0.06", "-0.06"]
    assert aggregates[1]["firstFiring"] == "null"
    assert aggregates[1]["pendingAtEnd"] == "1"
    assert aggregates[4]["firingDurationSeconds"] == "0.18"
    assert aggregates[4]["deltaFiringDurationSeconds"] == "0.12"
    assert [[r["firstFiringDelaySeconds"], r["deltaFiringDurationSeconds"]] for r in rows
            if r["candidateIndex"] == "3" and r["scope"] == "series"] == [["-0.06", "0.06"], ["-0.06", "0.06"]]
    assert all(r["baselineForMs"] == "60" and r["baselineKeepFiringForMs"] == "0" for r in rows)
    data["data"]["result"].reverse()
    assert csv_rows(api.grid_csv(data, base, params)) == rows
    never_baseline = csv_rows(api.grid_csv(data, {**base, "for": "1s"}, params))
    assert all(r["firstFiringDelaySeconds"] == "null" for r in never_baseline)


def test_csv_exact_timer_pairs_do_not_round_through_floating_point_seconds():
    mx = "9007199254740991ms"
    rows = csv_rows(api.grid_csv(matrix(), cfgd({"for": mx, "keep_firing_for": mx, "start": 0, "end": 0}),
                                 {"for": [mx, "1ms"], "keep_firing_for": ["1ms", mx], "maxEvaluations": 5}))
    assert len(rows) == 5
    assert [[r["forMs"], r["keepFiringForMs"]] for r in rows] == [
        ["9007199254740991", "9007199254740991"],
        ["9007199254740991", "1"], ["9007199254740991", "9007199254740991"],
        ["1", "1"], ["1", "9007199254740991"]]
    assert all(r["scope"] == "aggregate" and r["seriesId"] == "null" and r["seriesCount"] == "0"
               and r["baselineForMs"] == "9007199254740991" and r["baselineKeepFiringForMs"] == "9007199254740991"
               and r["firstFiringDelaySeconds"] == "null" for r in rows)


def test_csv_canonical_labels_round_trip_quotes_commas_unicode_line_breaks_and_formula_like_strings():
    labels = parse_json('{"__proto__":"=SUM(1,2)","__name__":"測定","instance":"a,\\"b\\"\\r\\nc","10":"@x","2":"+x"}')
    data = matrix(series([2], labels), series([0], {}))
    rows = csv_rows(api.grid_csv(data, cfg(), {"for": ["0s", "1ms"], "keep_firing_for": ["0s"], "maxEvaluations": 6}))
    ids = [r["seriesId"] for r in rows if r["scope"] == "series"]
    assert len(ids) == 6
    assert all(i.startswith("[") for i in ids), "no raw formula-like label starts a CSV cell"
    pairs = sort_pairs(labels)
    assert len([i for i in ids if i == to_json(pairs)]) == 3
    assert len([i for i in ids if i == "[]"]) == 3
    assert parse_json(next(i for i in ids if i != "[]")) == pairs
    aggregate = [r for r in rows if r["scope"] == "aggregate"]
    assert [[r["openAtEnd"], r["pendingAtEnd"], r["firingDurationSeconds"]] for r in aggregate] == \
        [["1", "0", "0"], ["1", "0", "0"], ["0", "1", "0"]]


def test_csv_shares_grid_validation_including_budget_first_and_atomic_missing_data_failures():
    with pytest.raises(Exception, match="requires 40.*budget 39"):
        api.grid_csv(INPUT, {**BASELINE, "missingData": "error"}, {**PARAMETERS, "maxEvaluations": 39})
    with pytest.raises(Exception, match="missing sample at 180"):
        api.grid_csv(INPUT, {**BASELINE, "missingData": "error"}, PARAMETERS)
    with pytest.raises(Exception, match="duplicate normalized duration"):
        api.grid_csv(INPUT, BASELINE, {**PARAMETERS, "for": ["60s", "1m"]})
    with pytest.raises(Exception, match="unsupported field"):
        api.grid_csv(INPUT, {**BASELINE, "unexpected": True}, PARAMETERS)
