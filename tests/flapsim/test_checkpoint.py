# SPDX-License-Identifier: Apache-2.0
import copy
import math

import pytest

import otelwrench.flapsim as api
from otelwrench.flapsim import parse_json, replay

from .support import cfg, cfgd, matrix, replay_with, roundtrip, series, slice_matrix

RULE = cfgd({"for": "120s", "keep_firing_for": "120s"})
CAPTURE = matrix(series([2, 2, 2, 0, 0, 2, 0, 0, 0, 2, 0]))


def run_window(start, end, checkpoint=None, data=None, config=None):
    data = CAPTURE if data is None else data
    config = RULE if config is None else config
    return replay_with(api, slice_matrix(data, start, end), {**config, "start": start, "end": end}, checkpoint)


def test_pending_checkpoint_fires_at_120_instead_of_restarting_the_for_timer():
    first = run_window(0, 60)
    second = run_window(120, 240, first["checkpoint"])
    assert second["series"][0]["timeline"][0]["state"] == "firing"
    assert [*first["series"][0]["timeline"], *second["series"][0]["timeline"]] == run_window(0, 240)["series"][0]["timeline"]
    assert first["checkpoint"]["series"][0]["pendingSince"] == 0


def test_every_split_matches_the_single_window_timeline_and_transitions_exactly():
    whole = run_window(0, 600)
    for end in range(0, 600, 60):
        first = run_window(0, end)
        second = run_window(end + 60, 600, roundtrip(first["checkpoint"]))
        for field in ("timeline", "transitions"):
            assert [*first["series"][0][field], *second["series"][0][field]] == whole["series"][0][field], f"{field} at {end}"
        assert second["checkpoint"] == whole["checkpoint"]


def test_canonical_label_and_series_order_is_irrelevant_all_labels_participate():
    special = parse_json('{"__proto__":"x","__name__":"m","☃":"a=b,\\"c"}')
    data = matrix(series([2, 2, 2, 2], special), series([2, 2, 0, 0], {"z": "2", "a": "1"}))
    first = run_window(0, 60, None, data)
    nxt = slice_matrix(data, 120, 180)
    nxt["data"]["result"].reverse()
    nxt["data"]["result"][0]["metric"] = {"a": "1", "z": "2"}
    first["checkpoint"]["series"].reverse()
    resumed = replay(nxt, {**RULE, "start": 120, "end": 180}, first["checkpoint"])
    whole = run_window(0, 180, None, data)
    for i in range(len(whole["series"])):
        assert resumed["series"][i]["timeline"] == whole["series"][i]["timeline"][2:]
    nxt["data"]["result"][1]["metric"] = {**nxt["data"]["result"][1]["metric"], "__name__": "different"}
    with pytest.raises(Exception, match="exact label sets"):
        replay(nxt, {**RULE, "start": 120, "end": 180}, first["checkpoint"])


def test_chained_millisecond_checkpoints_preserve_timers_holds_and_missing_as_false():
    data = matrix(series([[1.001, "2"], [1.061, "2"], [1.121, "2"], [1.241, "0"], [1.301, "0"]]))
    c = cfgd({"evaluationInterval": "60ms", "for": "61ms", "keep_firing_for": "61ms"})
    whole = run_window(1.001, 1.301, None, data, c)
    first = run_window(1.001, 1.121, None, data, c)
    second = run_window(1.181, 1.241, first["checkpoint"], data, c)
    third = run_window(1.301, 1.301, second["checkpoint"], data, c)
    assert [p for r in (first, second, third) for p in r["series"][0]["timeline"]] == whole["series"][0]["timeline"]
    assert third["checkpoint"] == whole["checkpoint"]
    assert second["series"][0]["episodes"] == [{"start": 1.121, "end": None, "durationSeconds": 0.06, "censored": True, "continued": True}]
    assert third["series"][0]["episodes"] == [{"start": 1.121, "end": 1.301, "durationSeconds": 0, "censored": False, "continued": True}]
    assert third["summary"]["firstFiring"] == 1.121


def test_zero_duration_firing_resumes_without_refiring_continued_exposure_is_clipped():
    c = cfg()
    data = matrix(series([2, 2, 0]))
    first = run_window(0, 0, None, data, c)
    second = run_window(60, 120, first["checkpoint"], data, c)
    assert [[t["timestamp"], t["from"], t["to"]] for t in second["series"][0]["transitions"]] == [[120, "firing", "inactive"]]
    assert second["series"][0]["episodes"] == [{"start": 0, "end": 120, "durationSeconds": 60, "censored": False, "continued": True}]
    assert second["summary"]["firingDurationSeconds"] == 60


def _set(cp, **changes):
    cp.update(changes)
    return cp


def _entry(cp, **changes):
    cp["series"][0].update(changes)
    return cp


def _delete(cp, key):
    del cp[key]
    return cp


def _delete_entry(cp, key):
    del cp["series"][0][key]
    return cp


def _push(cp, entry):
    cp["series"].append(entry)
    return cp


def _label(cp, value):
    cp["series"][0]["labels"]["instance"] = value
    return cp


def _rule_for(cp):
    cp["config"]["for"] = "bad"
    return cp


BAD_STATES = [
    ("null", lambda cp: None, "expected an object"),
    ("unknown version", lambda cp: {**cp, "schemaVersion": 2}, "unsupported kind or schemaVersion"),
    ("wrong kind", lambda cp: {**cp, "kind": "other"}, "unsupported kind or schemaVersion"),
    ("extra field", lambda cp: {**cp, "guessed": True}, "unsupported field"),
    ("missing field", lambda cp: _delete(cp, "evaluatedThrough"), "missing field"),
    ("invalid rule", _rule_for, "nonnegative duration"),
    ("non-array series", lambda cp: {**cp, "series": {}}, "expected an array"),
    ("missing series", lambda cp: {**cp, "series": []}, "exact label sets"),
    ("extra series", lambda cp: _push(cp, {**cp["series"][0], "labels": {"instance": "b"}}), "exact label sets"),
    ("duplicate identity", lambda cp: _push(cp, cp["series"][0]), "duplicate canonical"),
    ("label mismatch", lambda cp: _label(cp, "b"), "exact label sets"),
    ("bad label", lambda cp: _label(cp, 2), "values must be strings"),
    ("invalid state", lambda cp: _entry(cp, state="unknown"), "inconsistent alert state"),
    ("inactive with timer", lambda cp: _entry(cp, state="inactive"), "inconsistent alert state"),
    ("pending without timer", lambda cp: _entry(cp, pendingSince=None), "inconsistent alert state"),
    ("pending with hold", lambda cp: _entry(cp, holdSince=60), "inconsistent alert state"),
    ("pending with episode", lambda cp: _entry(cp, episodeStart=60), "inconsistent alert state"),
    ("expired pending", lambda cp: _entry(_set(cp, originStart=-60), pendingSince=-60), "inconsistent alert state"),
    ("firing prematurely", lambda cp: _entry(cp, state="firing", episodeStart=60), "inconsistent alert state"),
    ("timer before capture", lambda cp: _entry(cp, pendingSince=-60), "timer outside known capture"),
    ("future timer", lambda cp: _entry(cp, pendingSince=120), "timer outside known capture"),
    ("off-grid timer", lambda cp: _entry(cp, pendingSince=1), "incompatible grid"),
    ("sub-ms timer", lambda cp: _entry(cp, pendingSince=0.0001), "millisecond precision"),
    ("string timer", lambda cp: _entry(cp, pendingSince="0"), "numeric Unix seconds"),
    ("nonfinite timer", lambda cp: _entry(cp, pendingSince=math.nan), "numeric Unix seconds"),
    ("missing timer", lambda cp: _delete_entry(cp, "holdSince"), "missing field"),
    ("origin off grid", lambda cp: {**cp, "originStart": 1}, "incompatible origin/evaluation grid"),
    ("origin after end", lambda cp: {**cp, "originStart": 120}, "incompatible origin/evaluation grid"),
    ("unsafe origin span", lambda cp: {**cp, "originStart": -9007199254740.99}, "incompatible origin/evaluation grid"),
]


@pytest.mark.parametrize("name,mutate,error", BAD_STATES, ids=[x[0] for x in BAD_STATES])
def test_reject_malformed_checkpoint(name, mutate, error):
    cp = copy.deepcopy(run_window(0, 60)["checkpoint"])
    with pytest.raises(Exception, match=error):
        replay(slice_matrix(CAPTURE, 120, 180), {**RULE, "start": 120, "end": 180}, mutate(cp))


def test_firing_checkpoints_reject_impossible_firing_hold_boundaries():
    cp = run_window(0, 240)["checkpoint"]
    for edit in ({"pendingSince": None}, {"episodeStart": None},
                 {"episodeStart": 180},  # for would already have elapsed at 120
                 {"holdSince": 120}):  # firing began on a true sample
        bad = copy.deepcopy(cp)
        bad["series"][0].update(edit)
        with pytest.raises(Exception, match="inconsistent alert state"):
            run_window(300, 360, bad)
    expired = copy.deepcopy(cp)
    expired["evaluatedThrough"] = 300
    with pytest.raises(Exception, match="inconsistent alert state"):
        run_window(360, 420, expired)


def test_gaps_overlaps_shifted_grids_and_inferred_windows_are_rejected():
    cp = run_window(0, 60)["checkpoint"]
    for start, end in ([60, 120], [180, 240], [121, 181]):
        with pytest.raises(Exception, match="must be contiguous"):
            replay(matrix(series([[start, "2"], [end, "2"]])), {**RULE, "start": start, "end": end}, cp)
    with pytest.raises(Exception, match="requires explicit start and end"):
        replay(slice_matrix(CAPTURE, 120, 180), RULE, cp)


CHANGES = [{"evaluationInterval": "30s"}, {"for": "60s"}, {"keep_firing_for": "60s"},
           {"threshold": {"op": ">=", "value": 1}}, {"threshold": {"op": ">", "value": 2}}, {"missingData": "error"}]


@pytest.mark.parametrize("change", CHANGES)
def test_reject_changed_settings(change):
    cp = run_window(0, 60)["checkpoint"]
    with pytest.raises(Exception, match="alert settings must match"):
        run_window(120, 180, cp, CAPTURE, {**RULE, **change})


def test_resume_keeps_missing_data_error_and_cannot_discover_absent_label_sets():
    c = {**RULE, "missingData": "error"}
    cp = run_window(0, 60, None, CAPTURE, c)["checkpoint"]
    with pytest.raises(Exception, match="missing sample at 120"):
        replay(matrix(series([])), {**c, "start": 120, "end": 180}, cp)
    empty = replay(matrix(), {**RULE, "start": 0, "end": 60})
    assert replay(matrix(), {**RULE, "start": 120, "end": 180}, empty["checkpoint"])["summary"]["series"] == 0
    with pytest.raises(Exception, match="exact label sets"):
        replay(matrix(), {**c, "start": 120, "end": 180}, cp)


def test_without_a_checkpoint_the_start_remains_assumed_inactive():
    resumed = run_window(120, 180, run_window(0, 60)["checkpoint"])
    cold = run_window(120, 180)
    assert resumed["series"][0]["timeline"][0]["state"] == "firing"
    assert cold["series"][0]["timeline"][0]["state"] == "pending"
    assert "pre-capture alert state is unknown" in cold["assumptions"][0]
    assert cold["checkpoint"]["originStart"] == 120
