# SPDX-License-Identifier: Apache-2.0
import copy

import pytest

import otelwrench.flapsim as api
from otelwrench.flapsim import parse_json

from .support import cfg, cfgd, matrix, replay_with, roundtrip, series, slice_matrix


def windows(data, config, bounds):
    checkpoint = None
    reports = []
    for start, end in bounds:
        report = replay_with(api, slice_matrix(data, start, end), {**config, "start": start, "end": end}, checkpoint)
        checkpoint = report["checkpoint"]
        reports.append(roundtrip(report))
    return reports


def test_join_counts_the_boundary_interval_once_and_merges_a_continued_open_episode():
    data = matrix(series([2, 2, 2, 2]))
    reports = windows(data, cfg(), [[0, 60], [120, 180]])
    assert sum(r["summary"]["firingDurationSeconds"] for r in reports) == 120
    joined = api.join(reports)
    assert joined["summary"]["firingDurationSeconds"] == 180
    assert joined["series"][0]["episodes"] == [{"start": 0, "end": None, "durationSeconds": 180, "censored": True}]
    assert joined == api.replay(data, cfg(start=0, end=180))


def test_join_retains_closed_history_and_resolution_at_the_first_resumed_evaluation():
    data = matrix(series([2, 0, 2, 0, 0, 2]))
    joined = api.join(windows(data, cfg(), [[0, 0], [60, 120], [180, 300]]))
    assert joined["series"][0]["episodes"] == [
        {"start": 0, "end": 60, "durationSeconds": 60, "censored": False},
        {"start": 120, "end": 180, "durationSeconds": 60, "censored": False},
        {"start": 300, "end": None, "durationSeconds": 0, "censored": True},
    ]
    assert joined["summary"]["firstFiring"] == 0
    assert joined["summary"]["resolutions"] == 2
    assert joined == api.replay(data, cfg(start=0, end=300))


def test_every_split_reproduces_nonmultiple_timers_hold_resets_missing_nan_and_pending():
    data = matrix(series([[1.001, "2"], [1.061, "2"], [1.121, "2"], [1.241, "2"],
                          [1.301, "NaN"], [1.361, "0"], [1.421, "0"], [1.481, "2"]]))
    config = cfgd({"evaluationInterval": "60ms", "for": "61ms", "keep_firing_for": "61ms"})
    whole = api.replay(data, {**config, "start": 1.001, "end": 1.481})
    for i in range(8):
        end = (1001 + i * 60) / 1000
        start = (1061 + i * 60) / 1000
        assert api.join(windows(data, config, [[1.001, end], [start, 1.481]])) == whole, f"split at {end}"
    joined = api.join(windows(data, config, [[1.001, 1.121], [1.181, 1.361], [1.421, 1.481]]))
    assert joined["summary"]["firingDurationSeconds"] == 0.3
    assert joined["summary"]["pendingAtEnd"] == 1
    assert joined["summary"]["missingSamples"] == 1
    assert joined["summary"]["nanSamples"] == 1
    assert joined["series"][0]["episodes"] == [{"start": 1.121, "end": 1.421, "durationSeconds": 0.3, "censored": False}]


def test_canonical_series_label_ordering_duplicate_diagnostics_and_input_immutability():
    labels = parse_json('{"__proto__":"x","__name__":"m","☃":"a=b,\\"c"}')
    data = {**matrix(series([2, 2, 0, 2], labels), series([0, 2, 2, 0], {"instance": "z"})),
            "warnings": ["synthetic warning"], "infos": ["synthetic info"]}
    reports = windows(data, cfg(), [[0, 60], [120, 180]])
    reports[1]["series"].reverse()
    reports[1]["checkpoint"]["series"].reverse()
    for s in reports[1]["series"]:
        s["labels"] = dict(reversed(list(s["labels"].items())))
    before = copy.deepcopy(reports)
    joined = api.join(reports)
    whole = api.replay(data, cfg(start=0, end=180))
    assert joined["series"] == whole["series"]
    assert joined["summary"] == whole["summary"]
    assert joined["summary"]["firingDurationSeconds"] == 240
    assert joined["sourceWarnings"] == ["synthetic warning", "synthetic warning"]
    assert joined["sourceInfos"] == ["synthetic info", "synthetic info"]
    assert reports == before


def test_inferred_first_window_and_previously_joined_prefixes_are_accepted():
    data = matrix(series([2, 2, 0, 2]))
    reports = windows(data, cfg(), [[0, 0], [60, 120], [180, 180]])
    reports[0] = api.replay(matrix(series([2])), cfg())
    joined = api.join(reports)
    assert joined["window"]["rangeSource"] == "config"
    assert joined == api.join([api.join(reports[:2]), reports[2]])
    assert joined == api.replay(data, cfg(start=0, end=180))


def test_empty_captures_and_exact_combined_evaluation_cap_counting_empty_matrices_as_one_series():
    reports = windows(matrix(), cfg(), [[0, 60 * 999998], [60 * 999999, 60 * 999999]])
    joined = api.join(reports)
    assert joined["window"]["evaluations"] == 1000000
    assert joined["series"] == []
    reports[1]["window"]["end"] += 60
    reports[1]["window"]["evaluations"] += 1
    reports[1]["checkpoint"]["evaluatedThrough"] += 60
    with pytest.raises(Exception, match="combined timeline exceeds 1000000"):
        api.join(reports)


def test_join_accepts_the_maximum_report_count_and_rejects_larger_arrays_before_replay():
    reports = windows(matrix(), cfg(), [[i * 60, i * 60] for i in range(256)])
    assert api.join(reports)["window"]["evaluations"] == 256
    with pytest.raises(Exception, match="2 to 256"):
        api.join([*reports, reports[0]])


def valid_reports():
    return windows(matrix(series([2, 2, 0, 2])), cfg(), [[0, 60], [120, 180]])


def _m(fn):
    """Mutate in place, return the reports (JS `r => { ...; return r; }`)."""
    def apply(r):
        fn(r)
        return r
    return apply


def _set(path, value):
    def fn(r):
        target = r
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value(target[path[-1]]) if callable(value) else value
    return _m(fn)


def _multi(*mutations):
    def fn(r):
        for mutation in mutations:
            mutation(r)
        return r
    return fn


def _inc(n=1):
    return lambda v: v + n


BAD_REPORTS = [
    ("null", lambda r: None, "2 to 256"),
    ("one report", lambda r: r[:1], "2 to 256"),
    ("missing origin", lambda r: [*r[1:], r[1]], "originStart"),
    ("reversed", lambda r: r[::-1], "originStart|contiguous"),
    ("duplicate window", lambda r: [r[0], r[0]], "contiguous"),
    ("gap", _multi(_set([1, "window", "start"], 180), _set([1, "window", "evaluations"], 1)), "checkpoint must end|contiguous"),
    ("overlap", _multi(_set([1, "window", "start"], 60), _set([1, "window", "evaluations"], _inc())), "contiguous"),
    ("shifted grid", _multi(_set([1, "window", "start"], _inc()), _set([1, "window", "end"], _inc()),
                            _set([1, "checkpoint", "evaluatedThrough"], _inc())), "contiguous"),
    ("rule mismatch", _multi(_set([1, "config", "forSeconds"], 60), _set([1, "checkpoint", "config", "for"], "60000ms")), "settings must match"),
    ("threshold mismatch", _multi(_set([1, "config", "threshold", "value"], 0),
                                  _set([1, "checkpoint", "config", "threshold", "value"], 0)), "settings must match"),
    ("policy mismatch", _multi(_set([1, "config", "missingData"], "error"),
                               _set([1, "checkpoint", "config", "missingData"], "error")), "settings must match"),
    ("interval mismatch", _multi(_set([1, "config", "evaluationIntervalSeconds"], 30),
                                 _set([1, "checkpoint", "config", "evaluationInterval"], "30000ms"),
                                 _set([1, "window", "evaluations"], 3)), "settings must match"),
    ("changed labels", _set([1, "series", 0, "labels", "__name__"], "other"), "exact label sets"),
    ("omitted series", _set([1, "series"], []), "exact label sets"),
    ("duplicate series", _m(lambda r: r[1]["series"].append(r[1]["series"][0])), "duplicate canonical"),
    ("different origin", _set([1, "checkpoint", "originStart"], -60), "originStart must match"),
    ("unsupported version", _set([0, "schemaVersion"], 2), "schemaVersion 1 replay"),
    ("wrong command", _set([0, "command"], "grid"), "schemaVersion 1 replay"),
    ("missing checkpoint", _m(lambda r: r[0].pop("checkpoint")), "missing field"),
    ("extra field", _set([0, "extra"], True), "unsupported field"),
    ("incorrect evaluation count", _set([0, "window", "evaluations"], _inc()), "incorrect window.evaluations"),
    ("sub-ms bound", _set([0, "window", "start"], 0.0001), "millisecond precision"),
    ("short timeline", _m(lambda r: r[1]["series"][0]["timeline"].pop()), "every evaluation"),
    ("duplicate timestamp", _set([0, "series", 0, "timeline", 1, "timestamp"], 0), "complete evaluation grid"),
    ("bad numeric string", _set([0, "series", 0, "timeline", 0, "value"], "stale"), "unsupported numeric value"),
    ("wrong sample kind", _set([0, "series", 0, "timeline", 0, "sampleKind"], "missing"), "inconsistent"),
    ("wrong condition", _set([0, "series", 0, "timeline", 0, "condition"], False), "inconsistent"),
    ("wrong state", _set([1, "series", 0, "timeline", 0, "state"], "firing"), "inconsistent"),
    ("wrong timer", _set([0, "series", 0, "timeline", 1, "pendingSince"], 60), "inconsistent"),
    ("wrong reason", _set([0, "series", 0, "timeline", 0, "reason"], "condition_true"), "inconsistent"),
    ("wrong final checkpoint", _set([0, "checkpoint", "series", 0, "episodeStart"], 60), "inconsistent"),
    ("wrong checkpoint version", _set([0, "checkpoint", "schemaVersion"], 2), "inconsistent"),
    ("wrong transitions", _set([0, "series", 0, "transitions"], []), "inconsistent"),
    ("wrong episodes", _set([1, "series", 0, "episodes"], []), "inconsistent"),
    ("wrong local summary", _set([1, "series", 0, "summary", "resolutions"], _inc()), "inconsistent"),
    ("wrong aggregate summary", _set([1, "summary", "firingDurationSeconds"], _inc()), "inconsistent"),
    ("malformed diagnostics", _set([1, "sourceWarnings"], [1]), "array of strings"),
    ("inferred resumed bounds", _set([1, "window", "rangeSource"], "sample-extrema"), "explicit config bounds"),
]


@pytest.mark.parametrize("name,mutate,pattern", BAD_REPORTS, ids=[x[0] for x in BAD_REPORTS])
def test_join_rejects(name, mutate, pattern):
    with pytest.raises(Exception, match=pattern):
        api.join(mutate(valid_reports()))


def test_join_rejects_a_second_cold_replay_even_when_its_samples_match_a_resumed_window():
    reports = valid_reports()
    reports[1] = api.replay(matrix(series([[120, "0"], [180, "2"]])), cfg(start=120, end=180))
    with pytest.raises(Exception, match="originStart must match"):
        api.join(reports)


def test_join_checks_total_span_and_aggregate_exposure_across_individually_valid_windows():
    interval = 4503599627370.495
    data = matrix(series([[0, "2"], [interval, "2"], [interval * 2, "2"]]),
                  series([[0, "2"], [interval, "2"], [interval * 2, "2"]], {"instance": "b"}))
    reports = windows(data, cfg(evaluationInterval="4503599627370495ms"),
                      [[0, 0], [interval, interval], [interval * 2, interval * 2]])
    with pytest.raises(Exception, match="aggregate duration exceeds safe integer"):
        api.join(reports)


def test_join_preserves_error_policy_inf_raw_sample_strings_and_nan_true_under_ne():
    data = matrix(series([[0, "NaN"], [60, "+Inf"], [120, "1"], [180, "-0"]]))
    config = cfg(threshold={"op": "!=", "value": 1}, missingData="error")
    reports = windows(data, config, [[0, 60], [120, 180]])
    assert api.join(reports) == api.replay(data, {**config, "start": 0, "end": 180})
    reports[1]["series"][0]["timeline"][0]["value"] = None
    with pytest.raises(Exception, match="missing sample at 120 under missingData=error"):
        api.join(reports)


# Normalized report seconds alone cannot distinguish adjacent timers this large.
def test_join_retains_exact_checkpoint_timer_identities_at_the_safe_integer_boundary():
    data = matrix(series([2, 2]))
    config = cfgd({"for": "9007199254740991ms", "keep_firing_for": "9007199254740991ms"})
    reports = windows(data, config, [[0, 0], [60, 60]])
    joined = api.join(reports)
    assert joined == api.replay(data, {**config, "start": 0, "end": 60})
    assert joined["checkpoint"]["config"]["for"] == "9007199254740991ms"
    reports[1]["checkpoint"]["config"]["for"] = "9007199254740990ms"
    with pytest.raises(Exception, match="settings must match"):
        api.join(reports)
