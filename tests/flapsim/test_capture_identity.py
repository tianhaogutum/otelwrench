# SPDX-License-Identifier: Apache-2.0
import copy
import re

import pytest

import otelwrench.flapsim as api
from otelwrench.flapsim import parse_json, to_json

from .support import (
    CANONICAL_CAPTURE, assert_identity, cfg, freeze, identity_config, identity_matrix, identity_oracle, identity_set,
    joined_set, matrix, reverse_keys, series, sha,
)


def summary(reports):
    return api.compare_report_set(reports[0], reports[1:], {"detail": "summary"})


def identity(reports):
    return summary(reports)["captureIdentity"]


def replay_pair(data, config):
    return [api.replay(data, config), api.replay(data, {**config, "for": "1ms"})]


def test_versioned_capture_identity_matches_independent_literal_byte_sha256_oracle():
    assert CANONICAL_CAPTURE == to_json(parse_json(CANONICAL_CAPTURE)), "canonical literal has no whitespace or alternate escapes"
    reports = identity_set(api)
    before = copy.deepcopy(reports)
    frozen = freeze(reports)
    assert_identity(summary(frozen))
    assert re.fullmatch("[a-f0-9]{64}", identity(frozen)["digest"])
    assert frozen == before
    assert "captureIdentity" not in api.compare_report_set(frozen[0], frozen[1:])
    assert "captureIdentity" not in summary(frozen)["baseline"], "capture identity is emitted once"


def test_capture_identity_excludes_settings_diagnostics_roles_multiplicity_and_ordering():
    reports = identity_set(api)
    expected = identity_oracle()
    reordered = reverse_keys(reports)
    for r in reordered:
        r["series"].reverse()
        r["checkpoint"]["series"].reverse()
    assert identity(reordered) == expected
    assert identity(reports[::-1]) == expected
    assert identity([reports[0], reports[1], reports[1], reports[0]]) == expected
    data = identity_matrix()
    data["data"]["result"].reverse()
    config = {**identity_config(), "evaluationInterval": "0.06s"}  # same exact interval
    changed = replay_pair(data, {**config, "threshold": {"op": "<=", "value": -1}, "for": "120ms", "keep_firing_for": "60ms"})
    changed[0]["sourceWarnings"] = ["changed", "changed"]
    changed[1]["sourceInfos"] = ["new context"]
    assert identity(changed) == expected
    complete = joined_set(api)
    assert identity(complete) == identity([complete[1], complete[2], complete[0]]), "valid error/false policies excluded"
    m = matrix(series([[0, "2"], [60, "0"]]))
    explicit, inferred = api.replay(m, cfg(start=0, end=60)), api.replay(m, cfg())
    assert identity([explicit, inferred]) == identity([inferred, explicit]), "range provenance excluded"


def _shift(p):
    p[0] = (round(p[0] * 1000) + 1) / 1000


def _stretch(p):
    p[0] = (0.001 * 1000 + 2 * (round(p[0] * 1000) - 1)) / 1000


def test_raw_spelling_missing_vs_nan_labels_exact_window_interval_and_empty_grid_change_identity():
    expected = identity_oracle()

    def altered(mutate):
        m = identity_matrix()
        mutate(m)
        return identity(identity_set(api, m))

    def rename(m):
        metric = m["data"]["result"][1]["metric"]
        metric["other"] = metric.pop("zone")

    for mutate in (
        lambda m: m["data"]["result"][1]["values"][0].__setitem__(1, "2"),
        lambda m: m["data"]["result"][1]["values"].insert(1, [0.061, "NaN"]),
        lambda m: m["data"]["result"][1].__setitem__("values", [v for v in m["data"]["result"][1]["values"] if v[0] != 0.121]),
        lambda m: m["data"]["result"][1]["values"][-1].__setitem__(1, "0"),
        lambda m: m["data"]["result"][1]["metric"].__setitem__("zone", 'é\n"\\'),
        rename,
    ):
        assert altered(mutate) != expected
    # Shift exact bounds by 1 ms while preserving interval and raw grid.
    m = identity_matrix()
    for s in m["data"]["result"]:
        for p in s["values"]:
            _shift(p)
    assert identity(replay_pair(m, {**identity_config(), "start": 0.002, "end": 0.182})) != expected
    stretched = identity_matrix()
    for s in stretched["data"]["result"]:
        for p in s["values"]:
            _stretch(p)
    assert identity(replay_pair(stretched, {**identity_config(), "evaluationInterval": "120ms", "end": 0.361})) != expected
    extended = identity(replay_pair(identity_matrix(), {**identity_config(), "end": 0.241}))
    assert extended != expected

    def empty(**c):
        return identity(replay_pair(matrix(), cfg(start=0, end=0, **c)))

    assert empty(evaluationInterval="9007199254740991ms") != empty(evaluationInterval="9007199254740990ms"), \
        "exact ms despite colliding normalized seconds"
    assert empty() != identity(replay_pair(matrix(series([])), cfg(start=0, end=0)))
    assert empty() != empty(end=60)
    assert empty() == {"schemaVersion": 1, "algorithm": "sha256", "encoding": "alert-flapsim-capture-v1",
                       "digest": sha('["alert-flapsim-capture-v1","0","0","60000","1",[]]')}


def test_fingerprint_cannot_authorize_compact_or_forged_input_or_bypass_late_validation():
    reports = identity_set(api)
    compact = summary(reports)
    for forged in (compact, compact["baseline"], {**compact["baseline"], "command": "replay"},
                   {**reports[1], "captureIdentity": identity_oracle()}):
        before = copy.deepcopy(forged)
        with pytest.raises(Exception, match="unsupported field|missing field|schemaVersion 1 replay"):
            summary([reports[0], reports[1], forged])
        assert forged == before

    def origin(r):
        r["checkpoint"]["originStart"] = -0.059

    for mutate in (lambda r: r["summary"].update(resolutions=r["summary"]["resolutions"] + 1),
                   lambda r: r["series"][0]["timeline"][0].update(value="2"), origin):
        forged = copy.deepcopy(reports[1])
        mutate(forged)
        group = [reports[0], reports[1], forged]
        before = copy.deepcopy(group)
        with pytest.raises(Exception):
            summary(group)
        assert group == before
