# SPDX-License-Identifier: Apache-2.0
import copy
import math
import re

import pytest

import otelwrench.flapsim as api
from otelwrench.flapsim import to_json

from .support import EXPECTED_GRID, cfg, cfgd, example, matrix, series

INPUT = example("grid-query-range.json")
BASELINE = example("grid-baseline.json")
PARAMETERS = example("timer-grid.json")


def test_grid_reproduces_every_hand_calculated_timer_pair_missing_policy_and_censored_episode():
    report = api.grid(INPUT, BASELINE, PARAMETERS)
    assert report["command"] == "grid"
    assert report["schemaVersion"] == 1
    assert report["budget"]["requestedEvaluations"] == 40
    assert len(report["candidates"]) == 4
    assert report["sourceWarnings"] == INPUT["warnings"]
    assert report["sourceInfos"] == INPUT["infos"]
    assert re.search("no statistical.*production.*universal optimum", " ".join(report["assumptions"]), re.I)
    for i, expected in enumerate(EXPECTED_GRID):
        candidate = report["candidates"][i]
        s = candidate["series"][0]
        assert candidate["config"]["forSeconds"] == expected["forSeconds"]
        assert candidate["config"]["keepFiringForSeconds"] == expected["keepFiringForSeconds"]
        assert candidate["config"]["missingData"] == "false"
        assert candidate["summary"]["transitionCount"] == expected["transitionCount"]
        assert candidate["summary"]["firingDurationSeconds"] == expected["firingDurationSeconds"]
        assert candidate["summary"]["firstFiring"] == expected["firstFiring"]
        assert candidate["delta"]["firstFiringDelaySeconds"] == expected["firstFiringDelaySeconds"]
        assert candidate["delta"]["transitionCount"] == expected["transitionCount"] - 5
        assert candidate["delta"]["firingDurationSeconds"] == expected["firingDurationSeconds"] - 240
        assert s["summary"]["pendingAtEnd"] is expected["pendingAtEnd"]
        assert s["summary"]["missingSamples"] == 1
        assert candidate["summary"]["missingSamples"] == 1
        assert s["labels"] == {"__name__": "synthetic_load", "instance": "a"}
        assert [[t["timestamp"], t["to"]] for t in s["transitions"]] == expected["transitions"]
        assert s["episodes"] == expected["episodes"]
        assert candidate["summary"]["openAtEnd"] == int(any(e["censored"] for e in expected["episodes"]))
        assert candidate["summary"]["resolutions"] == len([e for e in expected["episodes"] if not e["censored"]])
        assert s["delta"]["firstFiringDelaySeconds"] == expected["firstFiringDelaySeconds"]
    assert report["baseline"]["summary"] == report["candidates"][0]["summary"]
    assert to_json(api.grid(INPUT, BASELINE, PARAMETERS)) == to_json(report)


def test_grid_counts_baseline_work_and_rejects_over_budget_before_running_missing_data_replay():
    with pytest.raises(Exception, match="requires 40.*budget 39"):
        api.grid(INPUT, {**BASELINE, "missingData": "error"}, {**PARAMETERS, "maxEvaluations": 39})
    with pytest.raises(Exception, match="missing sample at 180"):
        api.grid(INPUT, {**BASELINE, "missingData": "error"}, PARAMETERS)


INVALID_GRIDS = [
    ("null", None, "expected an object"),
    ("unknown field", {**PARAMETERS, "missingData": "false"}, "unsupported field"),
    ("missing budget", {"for": ["0s"], "keep_firing_for": ["0s"]}, "missing field maxEvaluations"),
    ("missing axis", {"for": ["0s"], "maxEvaluations": 40}, "missing field keep_firing_for"),
    ("empty axis", {**PARAMETERS, "for": []}, "array of 1 to 256"),
    ("nonarray axis", {**PARAMETERS, "keep_firing_for": "0s"}, "array of 1 to 256"),
    ("negative duration", {**PARAMETERS, "for": ["-1s"]}, "nonnegative duration"),
    ("numeric duration", {**PARAMETERS, "for": [60]}, "nonnegative duration"),
    ("sub-ms duration", {**PARAMETERS, "for": ["0.0001s"]}, "safe integer milliseconds"),
    ("overflow duration", {**PARAMETERS, "for": ["9007199254740992ms"]}, "safe integer milliseconds"),
    ("duplicate normalized for", {**PARAMETERS, "for": ["60s", "1m"]}, "duplicate normalized duration"),
    ("duplicate normalized hold", {**PARAMETERS, "keep_firing_for": ["0s", "0ms"]}, "duplicate normalized duration"),
    ("sparse array hole", {**PARAMETERS, "for": [None]}, "nonnegative duration"),
    ("oversized axis", {**PARAMETERS, "for": [f"{i}s" for i in range(257)]}, "array of 1 to 256"),
    ("oversized product", {**PARAMETERS, "for": [f"{i}s" for i in range(129)]}, "exceeds 256 combinations"),
]
for _budget in [0, -1, 1.5, "40", None, math.nan, math.inf, 1_000_001, 9007199254740991, True]:
    INVALID_GRIDS.append((f"invalid budget {_budget!r}", {**PARAMETERS, "maxEvaluations": _budget}, "integer from 1 to 1000000"))


@pytest.mark.parametrize("name,value,pattern", INVALID_GRIDS, ids=[x[0] for x in INVALID_GRIDS])
def test_grid_rejects(name, value, pattern):
    with pytest.raises(Exception, match=pattern):
        api.grid(INPUT, BASELINE, value)


def test_grid_budget_covers_all_series_and_charges_empty_matrices_without_bypassing_the_hard_cap():
    two = matrix(series([2, 0], {"id": "a"}), series([0, 2], {"id": "b"}))
    one_pair = {"for": ["0s"], "keep_firing_for": ["0s"], "maxEvaluations": 8}
    assert api.grid(two, cfg(), one_pair)["budget"]["requestedEvaluations"] == 8
    with pytest.raises(Exception, match="requires 8.*budget 7"):
        api.grid(two, cfg(), {**one_pair, "maxEvaluations": 7})
    empty = cfg(evaluationInterval="1ms", start=0, end=499.999)
    maximum = api.grid(matrix(), empty, {**one_pair, "maxEvaluations": 1_000_000})
    assert maximum["budget"]["requestedEvaluations"] == 1_000_000
    assert maximum["baseline"]["summary"]["firstFiring"] is None
    assert maximum["candidates"][0]["delta"]["firstFiringDelaySeconds"] is None
    with pytest.raises(Exception, match="requires 1000002.*budget 1000000"):
        api.grid(matrix(), {**empty, "end": 500}, {**one_pair, "maxEvaluations": 1_000_000})
    at_limit = {"for": [f"{i}ms" for i in range(256)], "keep_firing_for": ["0s"], "maxEvaluations": 257}
    assert len(api.grid(matrix(), cfg(start=0, end=0), at_limit)["candidates"]) == 256


def test_grid_retains_user_axis_order_and_reports_exact_signed_millisecond_and_per_label_tradeoffs():
    data = matrix(series([[1.001, "2"], [1.061, "2"], [1.121, "0"]], {"id": "b"}),
                  series([[1.001, "0"], [1.061, "2"], [1.121, "2"]], {"id": "a"}))
    params = {"for": ["61ms", "0ms"], "keep_firing_for": ["61ms", "0ms"], "maxEvaluations": 30}
    r = api.grid(data, cfgd({"evaluationInterval": "60ms", "for": "60ms"}), params)
    assert [[c["config"]["forSeconds"], c["config"]["keepFiringForSeconds"]] for c in r["candidates"]] == \
        [[0.061, 0.061], [0.061, 0], [0, 0.061], [0, 0]]
    assert [s["summary"]["firstFiring"] for s in r["candidates"][0]["series"]] == [None, None]
    assert r["candidates"][0]["delta"]["firstFiringDelaySeconds"] is None
    immediate = r["candidates"][3]
    assert immediate["delta"]["firstFiringDelaySeconds"] == -0.06
    assert immediate["summary"]["firingDurationSeconds"] == 0.18
    assert immediate["delta"]["firingDurationSeconds"] == 0.12
    assert [[s["labels"]["id"], s["delta"]["firstFiringDelaySeconds"], s["delta"]["firingDurationSeconds"]]
            for s in immediate["series"]] == [["a", -0.06, 0.06], ["b", -0.06, 0.06]]
    reversed_input = copy.deepcopy(data)
    reversed_input["data"]["result"].reverse()
    assert api.grid(reversed_input, cfgd({"evaluationInterval": "60ms", "for": "60ms"}), params) == r


def test_grid_preserves_ne_nan_versus_missing_as_false_and_inferred_range_behavior():
    data = matrix(series([[60, "NaN"], [180, "2"]]))
    r = api.grid(data, cfg(threshold={"op": "!=", "value": 1}), PARAMETERS)
    assert r["window"]["rangeSource"] == "sample-extrema"
    assert r["window"]["start"] == 60
    assert r["window"]["end"] == 180
    assert r["baseline"]["summary"]["nanSamples"] == 1
    assert r["baseline"]["summary"]["missingSamples"] == 1
    assert r["candidates"][0]["series"][0]["episodes"] == [
        {"start": 60, "end": 120, "durationSeconds": 60, "censored": False},
        {"start": 180, "end": None, "durationSeconds": 0, "censored": True}]
    with pytest.raises(Exception, match="missing sample at 120"):
        api.grid(data, cfg(threshold={"op": "!=", "value": 1}, missingData="error"), PARAMETERS)
