# SPDX-License-Identifier: Apache-2.0
import copy
import math
import re
import sys

import pytest

import otelwrench.flapsim as api
from otelwrench.flapsim import parse_json

from .support import (
    assert_joined_rule_identities, cfgd, cli, joined_set, matrix, reverse_keys, rule_oracle, series, write_json, freeze,
)


def summary(reports):
    return api.compare_report_set(reports[0], reports[1:], {"detail": "summary"})


def settings():
    return cfgd({"evaluationInterval": "60ms", "for": "120ms", "keep_firing_for": "180ms",
                 "threshold": {"op": ">=", "value": 1.5}, "start": 0, "end": 0.18})


def capture():
    return matrix(series([[0, "0"], [0.06, "2"], [0.12, "2"], [0.18, "0"]]))


def expected():
    return rule_oracle('["alert-flapsim-rule-v1","60","120","180",">=",1.5,"false"]')


def project(config=None, data=None):
    r = api.replay(capture() if data is None else data, settings() if config is None else config)
    return summary([r, r])


def test_per_report_versioned_rule_identity_matches_independent_literal_sha256_oracles():
    reports = joined_set(api)
    before = copy.deepcopy(reports)
    frozen = freeze(reports)
    result = summary(frozen)
    assert_joined_rule_identities(result)
    assert frozen == before
    assert project()["baseline"]["ruleIdentity"] == expected()
    assert re.fullmatch("[a-f0-9]{64}", result["baseline"]["ruleIdentity"]["digest"])
    assert result["baseline"]["ruleIdentity"]["encoding"] != result["captureIdentity"]["encoding"]
    for options in (None, {}, {"detail": "full"}):
        full = (api.compare_report_set(frozen[0], frozen[1:]) if options is None
                else api.compare_report_set(frozen[0], frozen[1:], options))
        for r in [full["baseline"], *[c["report"] for c in full["candidates"]]]:
            assert "ruleIdentity" not in r


def test_equivalent_duration_numeric_spelling_key_order_captures_and_diagnostics_preserve_identity():
    equivalent = {"end": 0.18, "start": 0, "missingData": "false", "keep_firing_for": "0.003m", "for": "0.12s",
                  "threshold": parse_json('{"value":15e-1,"op":">="}'), "evaluationInterval": "0.001m"}
    assert project(equivalent)["baseline"]["ruleIdentity"] == expected()
    reports = [api.replay(capture(), settings()), api.replay(capture(), equivalent)]
    reports[0]["sourceWarnings"] = ["w", "w"]
    reports[1]["sourceInfos"] = ["i", "i"]
    assert summary(reverse_keys(reports))["baseline"]["ruleIdentity"] == expected()
    changed = matrix(series([[0, "NaN"], [0.12, "-0"], [0.18, "2.0"]], {"other": "é"}))
    altered = project(settings(), changed)
    assert altered["baseline"]["ruleIdentity"] == expected()
    assert altered["captureIdentity"] != project()["captureIdentity"]
    moved = project({**settings(), "start": 0.06, "end": 0.24}, matrix(series([[0.06, "4"], [0.24, "0"]])))
    assert moved["baseline"]["ruleIdentity"] == expected()
    inferred = settings()
    del inferred["start"], inferred["end"]
    assert project(inferred)["baseline"]["ruleIdentity"] == expected()
    reordered = joined_set(api)
    for r in reordered:
        r["series"].reverse()
        r["checkpoint"]["series"].reverse()
    assert_joined_rule_identities(summary(reordered))
    assert summary(reports[::-1])["baseline"]["ruleIdentity"] == expected()


def test_every_rule_field_changes_identity_even_with_equal_outcomes_or_colliding_seconds():
    original = project()
    for change in ({"for": "121ms"}, {"keep_firing_for": "181ms"}, {"threshold": {"op": ">=", "value": 1.5000000000000002}},
                   {"missingData": "error"}):
        nxt = project({**settings(), **change})
        assert nxt["baseline"]["ruleIdentity"] != expected()
        assert nxt["captureIdentity"] == original["captureIdentity"], "rule changes preserve shared data identity"
    ops = [">", ">=", "<", "<=", "==", "!="]
    identities = [project({**settings(), "threshold": {"op": op, "value": 1.5}})["baseline"]["ruleIdentity"]["digest"] for op in ops]
    assert len(set(identities)) == 6
    normalized_names = {"evaluationInterval": "evaluationIntervalSeconds", "for": "forSeconds", "keep_firing_for": "keepFiringForSeconds"}
    for field, normalized in normalized_names.items():
        a = {**settings(), "start": 0, "end": 0, field: "9007199254740991ms"}
        b = {**a, field: "9007199254740990ms"}
        result = summary([api.replay(matrix(), a), api.replay(matrix(), a if field == "evaluationInterval" else b)])
        left = result["baseline"]
        right = project(b, matrix())["baseline"] if field == "evaluationInterval" else result["candidates"][0]["report"]
        assert left["config"][normalized] == right["config"][normalized]
        assert left["ruleIdentity"] != right["ruleIdentity"], field + " exact-ms identity"
        if field == "evaluationInterval":
            assert result["captureIdentity"] != project(b, matrix())["captureIdentity"]


def test_threshold_canonical_number_encoding_normalizes_negative_zero_and_keeps_distinct_doubles():
    for number, token in [(0, "0"), (-0.0, "0"), (1e21, "1e+21"), (1e-7, "1e-7"), (5e-324, "5e-324"),
                          (sys.float_info.max, "1.7976931348623157e+308"), (-sys.float_info.max, "-1.7976931348623157e+308")]:
        r = project({**settings(), "threshold": {"op": ">=", "value": number}})["baseline"]
        assert r["ruleIdentity"] == rule_oracle('["alert-flapsim-rule-v1","60","120","180",">=",' + token + ',"false"]')
    zero = project({**settings(), "threshold": {"op": ">=", "value": 0}})["baseline"]["ruleIdentity"]
    minus_zero = project({**settings(), "threshold": {"op": ">=", "value": -0.0}})["baseline"]["ruleIdentity"]
    assert minus_zero == zero
    assert project({**settings(), "threshold": {"op": ">=", "value": 5e-324}})["baseline"]["ruleIdentity"] != zero
    r = api.replay(capture(), {**settings(), "threshold": {"op": ">=", "value": -0.0}})
    value = r["config"]["threshold"]["value"]
    assert value == 0 and math.copysign(1.0, value) < 0, "negative zero exercised before JSON serialization"
    from .support import roundtrip
    assert summary([r, roundtrip(r)])["baseline"]["ruleIdentity"] == zero


def test_rule_metadata_never_authorizes_compact_or_forged_input_and_cli_failures_remain_atomic(tmp_path):
    reports = joined_set(api)
    projected = summary(reports)
    paths = []
    for i, r in enumerate(reports):
        p = tmp_path / f"{i}.json"
        write_json(p, r)
        paths.append(p)

    def run():
        return cli("compare-report-set", *paths, "--detail", "summary")

    raw = [p.read_bytes() for p in paths]
    ok_result = run()
    assert ok_result.status == 0, ok_result.stderr
    assert ok_result.stderr == ""
    assert_joined_rule_identities(parse_json(ok_result.stdout))
    for forged in (projected["baseline"], {**projected["candidates"][0]["report"], "command": "replay"},
                   {**reports[2], "ruleIdentity": expected()},
                   {**reports[2], "config": {**reports[2]["config"], "ruleIdentity": expected()}}):
        for detail in ("full", "summary"):
            before = copy.deepcopy(forged)
            with pytest.raises(Exception):
                api.compare_report_set(reports[0], [reports[1], forged], {"detail": detail})
            assert forged == before
        with pytest.raises(Exception):
            api.compare_reports(reports[0], forged)
        with pytest.raises(Exception):
            api.join([reports[0], forged])
        write_json(paths[2], forged)
        bad_bytes = paths[2].read_bytes()
        failed = run()
        assert failed.status == 1
        assert failed.stdout == ""
        assert re.search("unsupported field|missing field|schemaVersion 1 replay", failed.stderr)
        assert paths[2].read_bytes() == bad_bytes
    for value in (math.inf, -math.inf, math.nan, "1", None):
        forged = copy.deepcopy(reports[2])
        forged["checkpoint"]["config"]["threshold"]["value"] = value
        forged["config"]["threshold"]["value"] = value
        with pytest.raises(Exception, match="finite JSON number"):
            summary([reports[0], reports[1], forged])
    for i in range(2):
        assert paths[i].read_bytes() == raw[i]
