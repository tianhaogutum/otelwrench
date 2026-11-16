# SPDX-License-Identifier: Apache-2.0
import copy
import math
import os
import sys
import tempfile

import pytest

import otelwrench.flapsim as api
from otelwrench.flapsim import parse_json

from .support import (
    bad, cfg, cfgd, cli, csv_rows, fixture_text, forbid_warnings, freeze, joined_set, matrix, ok, roundtrip, series,
    sha, summary_csv_set, write_json, write_raw,
)

FLAGS = ["--detail", "summary", "--format", "csv"]


def csv(reports):
    return api.compare_report_set_summary_csv(reports[0], reports[1:])


def literal():
    return fixture_text("report-set-summary-v1.csv")


def run(*args):
    return cli("compare-report-set", *args)


def files(tmp_path, reports=None, prefix=""):
    reports = summary_csv_set(api) if reports is None else reports
    paths = []
    for i, r in enumerate(reports):
        p = tmp_path / f"{prefix}{i}.json"
        write_json(p, r)
        paths.append(p)
    return paths


def test_summary_csv_literal_joined_multi_label_multi_candidate_byte_oracle_and_sha256_identities():
    reports = summary_csv_set(api)
    before = copy.deepcopy(reports)
    frozen = freeze(reports)
    text = csv(frozen)
    assert text == literal()
    assert frozen == before
    rows = csv_rows(text)
    # Independent literal bytes, never hashes of production output or helpers.
    capture = ('["alert-flapsim-capture-v1","1001","1361","60","7",[[[["instance","a,\\"\\n"]],["0","2","2","0","2","2","0"]],'
               '[[["instance","b\\r\\n"]],["0","2","0","0","0","0","0"]]]]')
    rules = ['["alert-flapsim-rule-v1","60","60","0",">",1,"false"]',
             '["alert-flapsim-rule-v1","60","0","120",">=",2,"false"]',
             '["alert-flapsim-rule-v1","60","120","60",">",3,"false"]']
    assert len(rows) == 9
    for i, r in enumerate(rows):
        assert r["captureDigest"] == sha(capture)
        assert r["ruleDigest"] == sha(rules[i // 3])
        assert r["schemaVersion"] == "1"
        assert r["csvSchema"] == "alert-flapsim-report-set-summary-v1"
        assert r["captureSchemaVersion"] == "1"
        assert r["captureAlgorithm"] == "sha256"
        assert r["captureEncoding"] == "alert-flapsim-capture-v1"
        assert r["ruleSchemaVersion"] == "1"
        assert r["ruleAlgorithm"] == "sha256"
        assert r["ruleEncoding"] == "alert-flapsim-rule-v1"
    assert [[r["firstFiringDelaySeconds"], r["deltaCensoredEpisodes"]] for r in rows[:3]] == [["null", "null"]] * 3
    reordered = copy.deepcopy(before)
    for r in reordered:
        r["series"].reverse()
        r["checkpoint"]["series"].reverse()
    assert csv(reordered) == text
    reversed_rows = csv_rows(csv([before[0], before[2], before[1]]))
    assert reversed_rows[3]["ruleDigest"] == rows[6]["ruleDigest"]
    assert reversed_rows[6]["candidateIndex"] == "1"


def test_summary_csv_cli_explicit_opt_in_in_either_option_order_preserves_files_and_prior_bytes(tmp_path):
    paths = files(tmp_path)
    raw = [p.read_bytes() for p in paths]
    assert ok(run(*paths, *FLAGS)) == literal()
    assert ok(run(*paths, "--format", "csv", "--detail", "summary")) == literal()
    assert [p.read_bytes() for p in paths] == raw
    reports = joined_set(api)
    original_paths = files(tmp_path, reports, prefix="joined-")
    base, *cs = reports
    full = ok(run(*original_paths))
    assert sha(full) == "e15ba058c4513dc349cfe4c26c70f09f7fca3007446b2165d9ba9efddcad5487"
    assert ok(run(*original_paths, "--detail", "full")) == full
    assert sha(ok(run(*original_paths, "--detail", "summary"))) == "2b8cdc2ce3e64ff47276a7cb3274b970cbd6a03d0f8a9d0210b5caa04770510c"
    full_csv = ok(run(*original_paths, "--format", "csv"))
    assert sha(full_csv) == "082ab68ccb5e150f3235693cd196f24e6fac46ffcc4f796a4b943c3ebce59e3a"
    assert ok(run(*original_paths, "--detail", "full", "--format", "csv")) == full_csv
    assert api.compare_report_set_csv(base, cs) == full_csv


def test_summary_csv_pending_censor_null_missing_nan_and_per_report_capture_context():
    data = matrix(series([[0, "NaN"], [120, "2"]]))
    reports = [api.replay(data, c) for c in (cfg(), cfgd({"start": 0, "end": 120, "for": "60s"}))]
    reports[0]["sourceWarnings"] = ['w,\n"', 'w,\n"']
    reports[1]["sourceInfos"] = ["i\r\n"]
    rows = csv_rows(csv(reports))
    assert [[r["firstFiring"], r["openAtEnd"], r["pendingAtEnd"], r["censoredEpisodes"], r["missingSamples"], r["nanSamples"]]
            for r in rows] == [["120", "1", "0", "1", "1", "1"], ["120", "1", "0", "1", "1", "1"],
                               ["null", "0", "1", "0", "1", "1"], ["null", "0", "1", "0", "1", "1"]]
    assert [[r["firstFiringDelaySeconds"], r["deltaPendingAtEnd"], r["deltaCensoredEpisodes"]] for r in rows[2:]] == [["null", "1", "-1"]] * 2
    assert rows[0]["rangeSource"] == "sample-extrema"
    assert rows[2]["rangeSource"] == "config"
    assert rows[0]["originStart"] == "0"
    assert rows[0]["evaluatedThrough"] == "120"
    assert rows[0]["evaluations"] == "3"
    assert parse_json(rows[0]["sourceWarningsJson"]) == reports[0]["sourceWarnings"]
    assert parse_json(rows[2]["sourceInfosJson"]) == reports[1]["sourceInfos"]
    assert parse_json(rows[0]["assumptionsJson"]) == reports[0]["assumptions"]
    assert rows[0]["captureDigest"] == rows[2]["captureDigest"]


def test_summary_csv_exact_ms_signed_deltas_finite_number_policy_and_negative_zero_semantics():
    mx = "9007199254740991ms"

    def empty(**change):
        return api.replay(matrix(), {**cfgd({"start": 0, "end": 0, "evaluationInterval": mx, "for": mx, "keep_firing_for": mx}), **change})

    a = empty()
    b = empty(**{"for": "9007199254740990ms", "keep_firing_for": "9007199254740990ms"})
    assert a["config"]["forSeconds"] == b["config"]["forSeconds"]
    exact = csv_rows(csv([a, b]))
    assert exact[0]["evaluationIntervalMs"] == "9007199254740991"
    assert exact[0]["forMs"] == "9007199254740991"
    assert exact[1]["forMs"] == "9007199254740990"
    assert exact[1]["keepFiringForMs"] == "9007199254740990"
    assert exact[0]["ruleDigest"] != exact[1]["ruleDigest"]
    assert exact[0]["seriesCount"] == "0"
    assert exact[0]["seriesId"] == "null"
    for value, token in [(-0.0, "0"), (0, "0"), (1e21, "1e+21"), (1e-7, "1e-7"), (5e-324, "5e-324"),
                         (sys.float_info.max, "1.7976931348623157e+308")]:
        r = empty(threshold={"op": "!=", "value": value}, missingData="error")
        row = csv_rows(csv([r, roundtrip(r)]))[0]
        assert row["thresholdJson"] == '{"op":"!=","value":' + token + "}"
        assert row["missingData"] == "error"
        assert row["ruleDigest"] == sha('["alert-flapsim-rule-v1","9007199254740991","9007199254740991","9007199254740991","!=",'
                                        + token + ',"error"]')
    for value in (math.inf, -math.inf, math.nan, "1", None):
        r = copy.deepcopy(b)
        r["checkpoint"]["config"]["threshold"]["value"] = value
        r["config"]["threshold"]["value"] = value
        with pytest.raises(Exception, match="finite JSON number"):
            csv([a, b, r])
    ms = 4503599627370480
    conf = cfg(evaluationInterval=f"{ms}ms", start=-0.01, end=4503599627370.47)
    data = matrix(*[series([[conf["start"], "2"], [conf["end"], "2"]], {"instance": i}) for i in ("b", "a")])
    runs = [api.replay(data, c) for c in (conf, {**conf, "for": "1ms"})]
    signed = csv_rows(csv(runs))
    from otelwrench.flapsim._js import js_string
    assert signed[3]["deltaFiringDurationSeconds"] == js_string(-2 * ms / 1000)
    assert signed[3]["firstFiringDelaySeconds"] == js_string(ms / 1000)
    assert csv_rows(csv(runs[::-1]))[3]["firstFiringDelaySeconds"] == js_string(-ms / 1000)


def empty_steps(steps):
    return api.replay(matrix(), cfg(start=0, end=(steps - 1) * 60))


def make_unsafe():
    unsafe = api.replay(matrix(series([2]), series([2], {"instance": "b"})), cfg())
    unsafe["config"]["evaluationIntervalSeconds"] = 4503599627370.495
    unsafe["checkpoint"]["config"]["evaluationInterval"] = "4503599627370495ms"
    unsafe["window"]["end"] = 9007199254740.99
    unsafe["window"]["evaluations"] = 3
    unsafe["checkpoint"]["evaluatedThrough"] = unsafe["window"]["end"]
    return unsafe


def test_summary_csv_whole_set_preflight_count_costs_and_safe_aggregate_ms_before_replay(tmp_path):
    a = empty_steps(1)
    for candidates in (None, {}, [], [a] * 256, [None]):
        with pytest.raises(Exception, match="1 to 255|expected an object"):
            api.compare_report_set_summary_csv(a, candidates)
    many = csv_rows(api.compare_report_set_summary_csv(a, [a] * 255))
    assert len(many) == 256
    assert many[-1]["candidateIndex"] == "254"
    assert many[0]["reports"] == "256"
    assert many[0]["requiredEvaluations"] == "256"
    cap, over = empty_steps(250000), empty_steps(250001)
    assert csv_rows(csv([cap, cap, cap, cap]))[0]["requiredEvaluations"] == "1000000"
    paths = files(tmp_path, [over])
    bad(run(*([paths[0]] * 4), *FLAGS), "combined timeline exceeds 1000000")
    guarded = forbid_warnings(over, "replay before preflight")
    with pytest.raises(Exception, match="combined timeline exceeds 1000000"):
        csv([guarded] * 4)
    nonempty = api.replay(matrix(series([0])), cfg(start=0, end=0))
    nonempty["window"]["end"] = 250000 * 60
    nonempty["window"]["evaluations"] = 250001
    nonempty["checkpoint"]["evaluatedThrough"] = nonempty["window"]["end"]
    with pytest.raises(Exception, match="combined timeline exceeds 1000000"):
        csv([nonempty] * 4)
    unsafe = make_unsafe()
    bad_paths = files(tmp_path, [unsafe, unsafe], prefix="unsafe-")
    bad(run(*bad_paths, *FLAGS), "aggregate duration exceeds safe integer")
    guarded = forbid_warnings(unsafe, "replay before preflight")
    with pytest.raises(Exception, match="aggregate duration exceeds safe integer"):
        csv([guarded, guarded])


def _window(r):
    r["window"]["start"] = 1.061
    r["window"]["evaluations"] -= 1


MUTATIONS = [
    (lambda r: r.update(schemaVersion=2), "schemaVersion 1 replay"),
    (lambda r: r["checkpoint"].update(originStart=0.941), "missing origin history"),
    (_window, "identical evaluation grids"),
    (lambda r: r["series"][0]["labels"].update(instance="other"), "exact label sets"),
    (lambda r: r["series"][0]["timeline"][1].update(value="2.0"), "identical raw samples"),
    (lambda r: r["summary"].update(resolutions=r["summary"]["resolutions"] + 1), "inconsistent"),
    (lambda r: r["series"][0]["summary"].update(firstFiring=0), "inconsistent"),
    (lambda r: r["series"][0]["timeline"][1].update(state="invalid"), "inconsistent"),
    (lambda r: r["series"][0]["episodes"].append({"invalid": True}), "inconsistent"),
    (lambda r: r["series"][0]["transitions"].append({"invalid": True}), "inconsistent"),
    (lambda r: r["series"][0]["timeline"].pop(), "every evaluation"),
    (lambda r: r["checkpoint"]["config"].update({"for": "1ms"}), "inconsistent"),
    (lambda r: r["config"].update(forSeconds=0.001), "inconsistent"),
    (lambda r: r.update(sourceWarnings=[1]), "array of strings"),
    (lambda r: r.update(ruleIdentity={"digest": "forged"}), "unsupported field"),
    (lambda r: r.update(captureIdentity={"digest": "forged"}), "unsupported field"),
]


def test_summary_csv_every_position_corrupt_compact_forged_report_fails_atomically(tmp_path):
    paths = files(tmp_path)
    for mutate, pattern in MUTATIONS:
        for position in range(3):
            reports = summary_csv_set(api)
            mutate(reports[position])
            before = copy.deepcopy(reports)
            with pytest.raises(Exception, match=pattern):
                csv(reports)
            assert reports == before
            for p, r in zip(paths, reports):
                write_json(p, r)
            raw = [p.read_bytes() for p in paths]
            bad(run(*paths, *FLAGS), pattern)
            assert [p.read_bytes() for p in paths] == raw
    reports = summary_csv_set(api)
    compact = api.compare_report_set(reports[0], reports[1:], {"detail": "summary"})
    for invalid in (compact, compact["baseline"], compact["candidates"][0]["report"],
                    {**compact["baseline"], "command": "replay"}, literal()):
        group = [reports[0], reports[1], invalid]
        before = copy.deepcopy(group)
        with pytest.raises(Exception, match="unsupported field|missing field|schemaVersion 1 replay|expected an object"):
            csv(group)
        assert group == before
        for p, r in zip(paths, group):
            write_json(p, r)
        bad(run(*paths, *FLAGS), "unsupported field|missing field|schemaVersion 1 replay|expected an object")


def test_summary_csv_cli_retains_strict_count_format_detail_file_bounds_and_late_file_failures(tmp_path):
    for suffix, pattern in [
        (["--detail"], "--detail: expected full or summary"), (["--detail", "SUMMARY"], "--detail: expected full or summary"),
        (["--detail", "summary", "--detail", "full"], "duplicate option --detail"),
        (["--format", "csv", "--format", "json"], "duplicate option --format"),
        (["--detail", "summary", "--format"], "--format: expected json or csv"),
        (["--detail", "summary", "--format", "tsv"], "--format: expected json or csv"),
        ([*FLAGS, "extra.json"], "unsupported argument"),
    ]:
        bad(run("/absent-base", "/absent-candidate", *suffix), pattern)
    for args in ([], ["/absent"], ["/absent"] * 257):
        bad(run(*args, *FLAGS), "2 to 256")
    bad(run("/absent-base", *FLAGS, "/absent-candidate"), "2 to 256")
    paths = files(tmp_path)
    original = [p.read_bytes() for p in paths]
    for raw, pattern in [("{broken", "invalid JSON"), ('{"x":1,"\\u0078":2}', "duplicate JSON object key"),
                         (b"\xff", "encoded data was not valid"), ("[" * 101 + "0" + "]" * 101, "nesting")]:
        write_raw(paths[2], raw)
        bad(run(*paths, *FLAGS), pattern)
        assert paths[2].read_bytes() == (raw if isinstance(raw, bytes) else raw.encode())
    write_raw(paths[2], original[2])
    bad(run(*paths[:2], tempfile.gettempdir(), *FLAGS), "regular local file")
    bad(run(*paths[:2], "/absent", *FLAGS), "ENOENT")
    os.truncate(paths[2], 64 * 1024 * 1024 + 1)
    bad(run(*paths, *FLAGS), "exceeds 64 MiB")
    assert [p.read_bytes() for p in paths[:2]] == original[:2]
