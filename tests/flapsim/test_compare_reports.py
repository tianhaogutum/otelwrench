# SPDX-License-Identifier: Apache-2.0
import copy
import os

import pytest

import otelwrench.flapsim as api
from otelwrench.flapsim import parse_json, to_json

from .support import (
    AGGREGATE_DELTA, LABEL_DELTAS, baseline_config, bad, candidate_config, cfg, cfgd, cli, matrix, report_matrix,
    reverse_keys, series, split_reports, success, write_json, write_raw,
)


def pair():
    return [api.replay(report_matrix(), baseline_config()), api.replay(report_matrix(), candidate_config())]


def without_identity(d):
    return {k: v for k, v in d.items() if k not in ("id", "labels")}


def test_saved_joined_reports_reproduce_matrix_compare_and_independent_episodes_and_deltas():
    data = report_matrix()
    reports = [api.join(split_reports(api, data, c)) for c in (baseline_config(), candidate_config())]
    before = copy.deepcopy(reports)
    r = api.compare_reports(*reports)
    assert r == {**api.compare(data, baseline_config(), candidate_config()), "command": "compare-reports"}
    assert r["delta"] == AGGREGATE_DELTA
    assert [without_identity(d) for d in r["seriesDeltas"]] == LABEL_DELTAS
    assert [s["episodes"] for s in r["baseline"]["series"]] == [
        [{"start": 60, "end": 180, "durationSeconds": 120, "censored": False},
         {"start": 240, "end": 360, "durationSeconds": 120, "censored": False}],
        [{"start": 60, "end": 120, "durationSeconds": 60, "censored": False}]]
    assert [s["episodes"] for s in r["candidate"]["series"]] == [
        [{"start": 120, "end": None, "durationSeconds": 240, "censored": True}], []]
    assert [r["baseline"]["summary"]["firstFiring"], r["baseline"]["summary"]["firingDurationSeconds"],
            r["candidate"]["summary"]["firstFiring"], r["candidate"]["summary"]["firingDurationSeconds"]] == [60, 300, 120, 240]
    assert reports == before


def test_canonical_output_accepts_object_series_ordering_differences_and_keeps_duplicate_diagnostics():
    data = {**report_matrix(), "warnings": ["w", "w"], "infos": ["i"]}
    a = api.join(split_reports(api, data, baseline_config()))
    b = api.replay({**data, "warnings": ["candidate"], "infos": ["x", "x"]}, candidate_config())
    expected = api.compare_reports(a, b)
    scrambled = reverse_keys(b)
    scrambled["series"].reverse()
    scrambled["checkpoint"]["series"].reverse()
    before = copy.deepcopy(scrambled)
    assert to_json(api.compare_reports(reverse_keys(a), scrambled)) == to_json(expected)
    assert expected["baseline"]["sourceWarnings"] == ["w"] * 6
    assert expected["baseline"]["sourceInfos"] == ["i", "i", "i"]
    assert expected["candidate"]["sourceWarnings"] == ["candidate"]
    assert expected["candidate"]["sourceInfos"] == ["x", "x"]
    assert scrambled == before


def test_valid_threshold_operator_timer_and_policy_differences_inferred_vs_explicit_bounds():
    data = matrix(series([[0, "NaN"], [60, "+Inf"], [120, "1"], [180, "-0"]]))
    a = api.replay(data, cfg())
    b = api.replay(data, cfgd({"threshold": {"op": "!=", "value": 0}, "for": "60s", "keep_firing_for": "60s",
                               "missingData": "error", "start": 0, "end": 180}))
    r = api.compare_reports(a, b)
    assert r["baseline"]["window"]["rangeSource"] == "sample-extrema"
    assert r["candidate"]["window"]["rangeSource"] == "config"
    assert r["candidate"] == b
    assert r["delta"]["firstFiringDelaySeconds"] == 0


def test_millisecond_deltas_special_labels_missing_samples_pending_end_and_never_fired_baseline():
    data = matrix(series([[1.001, "2.0"], [1.061, "2"], [1.181, "2"]],
                         parse_json('{"__proto__":"x","__name__":"m","☃":"a=b,\\"c"}')))
    c = cfg(evaluationInterval="60ms", start=1.001, end=1.181)
    a = api.replay(data, c)
    b = api.replay(data, {**c, "for": "60ms"})
    r = api.compare_reports(a, b)
    assert r["delta"]["firstFiringDelaySeconds"] == 0.06
    assert r["delta"]["firingDurationSeconds"] == -0.06
    assert r["candidate"]["summary"]["pendingAtEnd"] == 1
    assert r["candidate"]["summary"]["missingSamples"] == 1
    assert api.compare_reports(b, a)["delta"]["firstFiringDelaySeconds"] == -0.06
    never = api.replay(data, {**c, "threshold": {"op": ">", "value": 10}})
    assert api.compare_reports(never, a)["delta"]["firstFiringDelaySeconds"] is None
    assert api.compare_reports(a, never)["seriesDeltas"][0]["firstFiringDelaySeconds"] is None


def test_combined_budget_charges_both_sides_accepts_exact_cap_and_rejects_before_replay():
    a = api.replay(matrix(), cfg(start=0, end=499999 * 60))
    assert api.compare_reports(a, copy.deepcopy(a))["baseline"]["window"]["evaluations"] == 500000
    b = api.replay(matrix(), cfg(start=0, end=500000 * 60))
    with pytest.raises(Exception, match="combined timeline exceeds 1000000"):
        api.compare_reports(b, b)
    full = api.replay(matrix(series([0])), cfg(start=0, end=0))
    full["window"]["end"] = 500000 * 60
    full["window"]["evaluations"] = 500001
    full["checkpoint"]["evaluatedThrough"] = full["window"]["end"]
    with pytest.raises(Exception, match="combined timeline exceeds 1000000"):
        api.compare_reports(full, full)


def make_unsafe():
    a = api.replay(matrix(series([2]), series([2], {"instance": "b"})), cfg())
    a["config"]["evaluationIntervalSeconds"] = 4503599627370.495
    a["checkpoint"]["config"]["evaluationInterval"] = "4503599627370495ms"
    a["window"]["end"] = 9007199254740.99
    a["window"]["evaluations"] = 3
    a["checkpoint"]["evaluatedThrough"] = a["window"]["end"]
    return a


def test_reject_unsafe_aggregate_exposure_before_replay():
    a = make_unsafe()
    with pytest.raises(Exception, match="aggregate duration exceeds safe integer"):
        api.compare_reports(a, a)


def _missing_vs_present(a, b):
    data = report_matrix()
    del data["data"]["result"][0]["values"][1]
    return [a, api.replay(data, candidate_config())]


def _missing_vs_nan(a, b):
    data = report_matrix()
    data["data"]["result"][0]["values"][1][1] = "NaN"
    nan = api.replay(data, candidate_config())
    del data["data"]["result"][0]["values"][1]
    return [api.replay(data, baseline_config()), nan]


def _do(fn):
    def apply(a, b):
        fn(a, b)
    return apply


def _altered(field):
    def apply(a, b):
        if field == "assumptions":
            b["assumptions"] = []
        elif field == "checkpoint":
            b["checkpoint"]["series"][0]["state"] = "inactive"
        elif field == "summary":
            b["summary"]["resolutions"] += 1
        elif field in ("id", "transitions", "episodes"):
            b["series"][0][field] = "fake" if field == "id" else []
        else:
            b["series"][0]["timeline"][1][field] = "invalid"
    return apply


def _window_change(a, b):
    b["window"]["start"] = 60
    b["window"]["evaluations"] -= 1


def _interval_change(a, b):
    b["config"]["evaluationIntervalSeconds"] = 30
    b["checkpoint"]["config"]["evaluationInterval"] = "30000ms"
    b["window"]["evaluations"] = 13


INVALID_REPORTS = [
    ("null report", lambda a, b: [a, None], "expected an object"),
    ("version", _do(lambda a, b: b.update(schemaVersion=2)), "schemaVersion 1 replay"),
    ("compact command", _do(lambda a, b: b.update(command="grid")), "schemaVersion 1 replay"),
    ("missing checkpoint", _do(lambda a, b: b.pop("checkpoint")), "missing field checkpoint"),
    ("unknown field", _do(lambda a, b: b.update(extra=1)), "unsupported field"),
    ("changed label", _do(lambda a, b: b["series"][0]["labels"].update(instance="other")), "exact label sets"),
    ("omitted series", _do(lambda a, b: b["series"].pop()), "exact label sets"),
    ("added series", _do(lambda a, b: b["series"].append({**b["series"][0], "labels": {"instance": "c"}})), "exact label sets"),
    ("duplicate series", _do(lambda a, b: b["series"].append(b["series"][0])), "duplicate canonical"),
    ("different window", _window_change, "identical evaluation grids"),
    ("different interval", _interval_change, "identical evaluation grids"),
    ("incomplete origin", _do(lambda a, b: b["checkpoint"].update(originStart=-60)), "originStart.*missing origin history"),
    ("count", _do(lambda a, b: b["window"].update(evaluations=b["window"]["evaluations"] + 1)), "incorrect window.evaluations"),
    ("short timeline", _do(lambda a, b: b["series"][0]["timeline"].pop()), "every evaluation"),
    ("altered timestamp", _do(lambda a, b: b["series"][0]["timeline"][0].update(timestamp=60)), "complete evaluation grid"),
    ("raw sample spelling", _do(lambda a, b: b["series"][0]["timeline"][1].update(value="2.0")), "identical raw samples"),
    ("missing vs present", _missing_vs_present, "identical raw samples"),
    ("missing vs NaN", _missing_vs_nan, "identical raw samples"),
    ("non-string sample", _do(lambda a, b: b["series"][0]["timeline"][1].update(value=2)), "numeric string"),
    *[(f"altered {field}", _altered(field), "inconsistent")
      for field in ["id", "assumptions", "checkpoint", "summary", "transitions", "episodes", "reason", "state",
                    "pendingSince", "holdSince", "condition", "sampleKind"]],
    ("local summary", _do(lambda a, b: b["series"][0]["summary"].update(firstFiring=0)), "inconsistent"),
    ("diagnostics", _do(lambda a, b: b.update(sourceWarnings=[1])), "array of strings"),
]


def mutated(mutate):
    reports = pair()
    result = mutate(*reports)
    return reports if result is None else result


@pytest.mark.parametrize("name,mutate,pattern", INVALID_REPORTS, ids=[x[0] for x in INVALID_REPORTS])
def test_reject_in_either_role(name, mutate, pattern):
    bad_pair = mutated(mutate)
    with pytest.raises(Exception, match=pattern):
        api.compare_reports(*bad_pair)
    with pytest.raises(Exception, match=pattern):
        api.compare_reports(*bad_pair[::-1])


def test_reject_real_resumed_suffix_even_if_both_reports_have_the_same_incomplete_history():
    r = split_reports(api, report_matrix(), candidate_config())[-1]
    with pytest.raises(Exception, match="originStart.*missing origin history"):
        api.compare_reports(r, r)


def test_cli_compares_saved_joined_reports_deterministically_and_rejects_malformed_reports(tmp_path):
    paths = [tmp_path / "baseline.json", tmp_path / "candidate.json"]
    reports = [api.join(split_reports(api, report_matrix(), c)) for c in (baseline_config(), candidate_config())]
    for p, r in zip(paths, reports):
        write_json(p, r)
    before = [p.read_bytes() for p in paths]
    result = cli("compare-reports", *paths)
    assert result.status == 0, result.stderr
    assert result.stderr == ""
    assert parse_json(result.stdout)["delta"] == AGGREGATE_DELTA
    assert [without_identity(d) for d in parse_json(result.stdout)["seriesDeltas"]] == LABEL_DELTAS
    assert result.stdout == cli("compare-reports", *paths).stdout
    assert [p.read_bytes() for p in paths] == before
    for _, mutate, pattern in INVALID_REPORTS:
        for p, v in zip(paths, mutated(mutate)):
            write_json(p, v)
        bad(cli("compare-reports", *paths), pattern)
    for raw, pattern in [("{broken", "invalid JSON"), ('{"schemaVersion":1,"schema\\u0056ersion":1}', "duplicate JSON object key"),
                         (b"\xff", "encoded data was not valid"), ("[" * 101 + "0" + "]" * 101, "nesting")]:
        write_raw(paths[0], before[0])
        write_raw(paths[1], raw)
        bad(cli("compare-reports", *paths), pattern)
    write_raw(paths[1], before[1])
    for args in [[], paths[:1], [*paths, paths[0]], ["--baseline", paths[0]]]:
        bad(cli("compare-reports", *args), "expected exactly two.*paths|unsupported option|unsupported argument")
    bad(cli("compare-reports", paths[0], tmp_path / "absent.json"), "ENOENT")
    bad(cli("compare-reports", paths[0], tmp_path), "regular local file")
    os.truncate(paths[1], 64 * 1024 * 1024 + 1)
    bad(cli("compare-reports", *paths), "exceeds 64 MiB")
    over = api.replay(matrix(), cfg(start=0, end=500000 * 60))
    for p in paths:
        write_json(p, over)
    bad(cli("compare-reports", *paths), "combined timeline exceeds 1000000")
