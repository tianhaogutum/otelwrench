# SPDX-License-Identifier: Apache-2.0
import copy
import os
import tempfile

import pytest

import otelwrench.flapsim as api
from otelwrench.flapsim import parse_json, to_json

from .support import (
    assert_reports_csv, bad, baseline_config, candidate_config, cfg, cfgd, cli, csv_rows, joined_pair, matrix, ok,
    report_matrix, reverse_keys, series, sha, sort_pairs, write_json, write_raw,
)


def files(tmp_path, reports=None):
    reports = joined_pair(api) if reports is None else reports
    paths = [tmp_path / "baseline.json", tmp_path / "candidate.json"]
    for p, r in zip(paths, reports):
        write_json(p, r)
    return paths


def run(*args):
    return cli("compare-reports", *args)


def test_cli_saved_report_csv_exports_joined_two_label_oracle_and_json_compatibility(tmp_path):
    paths = files(tmp_path)
    before = [p.read_bytes() for p in paths]
    json_text = ok(run(*paths))
    assert sha(json_text) == "d690f5aa127c8e2b337cc7f7b70185d5b2a6b20294b243b84cede3c938d954f1"
    csv_text = ok(run(*paths, "--format", "csv"))
    assert_reports_csv(csv_text)
    assert ok(run(*paths, "--format", "csv")) == csv_text
    assert ok(run(*paths, "--format", "json")) == json_text
    assert [p.read_bytes() for p in paths] == before


def test_library_saved_csv_shares_matrix_csv_schema_and_joined_oracle_without_mutation():
    reports = joined_pair(api)
    before = copy.deepcopy(reports)
    text = api.compare_reports_csv(*reports)
    assert_reports_csv(text)
    assert text == api.compare_csv(report_matrix(), baseline_config(), candidate_config())
    scrambled = [reverse_keys(r) for r in reports]
    for r in scrambled:
        r["series"].reverse()
        r["checkpoint"]["series"].reverse()
    assert api.compare_reports_csv(*scrambled) == text
    assert reports == before


def test_saved_csv_exact_safe_integer_timers_and_interval_survive_seconds_normalization():
    mx = "9007199254740991ms"
    base = cfgd({"for": mx, "keep_firing_for": "1ms", "evaluationInterval": mx, "start": 0, "end": 0})
    candidate = {**base, "for": "1ms", "keep_firing_for": mx}
    reports = [api.replay(matrix(), c) for c in (base, candidate)]
    assert api.compare_reports(*reports)["baseline"] == reports[0], "genuine max timer reports must validate"
    rows = csv_rows(api.compare_reports_csv(*reports))
    assert [[r["forMs"], r["keepFiringForMs"]] for r in rows] == [["9007199254740991", "1"], ["1", "9007199254740991"]]
    assert all(r["evaluationIntervalMs"] == "9007199254740991" and r["baselineForMs"] == "9007199254740991"
               and r["baselineKeepFiringForMs"] == "1" and r["scope"] == "aggregate" and r["seriesCount"] == "0"
               and r["firstFiringDelaySeconds"] == "null" for r in rows)


def test_saved_csv_signed_ms_deltas_never_fire_nulls_missing_nan_held_metrics_and_role_context():
    data = matrix(series([[1.001, "2"], [1.061, "2"], [1.121, "0"]]))
    base = cfgd({"evaluationInterval": "60ms", "for": "60ms"})
    candidate = {**base, "for": "0ms", "missingData": "error", "threshold": {"op": ">=", "value": 2}, "start": 1.001, "end": 1.121}
    reports = [api.replay(data, c) for c in (base, candidate)]
    rows = csv_rows(api.compare_reports_csv(*reports))
    assert [[r["thresholdJson"], r["missingData"], r["rangeSource"]] for r in rows] == [
        *[['{"op":">","value":1}', "false", "sample-extrema"]] * 2,
        *[['{"op":">=","value":2}', "error", "config"]] * 2]
    for r in rows[2:]:
        assert r["firstFiringDelaySeconds"] == "-0.06"
        assert r["deltaFiringDurationSeconds"] == "0.06"
        assert r["firingDurationSeconds"] == "0.12"
        assert r["windowStart"] == "1.001"
        assert r["windowEnd"] == "1.121"
    assert csv_rows(api.compare_reports_csv(*reports[::-1]))[2]["firstFiringDelaySeconds"] == "0.06"
    never = api.replay(data, {**base, "for": "121ms"})
    for p in ([never, reports[0]], [reports[0], never]):
        assert all(r["firstFiringDelaySeconds"] == "null" for r in csv_rows(api.compare_reports_csv(*p)))
    diag = matrix(series([[0, "NaN"], [120, "2"], [180, "0"]]))
    diagnostic_pair = [api.replay(diag, c) for c in (cfg(), cfg(keep_firing_for="120s"))]
    diagnostics = csv_rows(api.compare_reports_csv(*diagnostic_pair))
    assert all(r["missingSamples"] == "1" and r["nanSamples"] == "1" for r in diagnostics)
    assert all(r["heldEvaluations"] == "1" and r["openAtEnd"] == "1" for r in diagnostics[2:])


def test_saved_csv_canonical_quoted_labels_and_separate_pending_versus_censored_counts():
    labels = parse_json('{"__proto__":"=SUM(1,2)","__name__":"測定","instance":"a,\\"b\\"\\r\\nc","10":"@x","2":"+x"}')
    data = matrix(series([2], labels), series([2], {}))
    reports = [api.replay(data, c) for c in (cfg(), cfgd({"for": "1ms"}))]
    before = copy.deepcopy(reports)
    text = api.compare_reports_csv(*reports)
    rows = csv_rows(text)
    ids = sorted([to_json(sort_pairs(labels)), "[]"], key=lambda s: s.encode("utf-16-be", "surrogatepass"))
    assert [r["seriesId"] for r in rows if r["scope"] == "series"] == [*ids, *ids]
    assert '""instance""' in text, "CSV doubles embedded JSON quotes"
    assert [[r["firingEpisodes"], r["resolutions"], r["openAtEnd"], r["pendingAtEnd"]] for r in rows if r["scope"] == "aggregate"] == \
        [["2", "0", "2", "0"], ["0", "0", "0", "2"]]
    assert all(r["openAtEnd"] == "0" and r["pendingAtEnd"] == "1" and r["deltaOpenAtEnd"] == "-1" and r["deltaPendingAtEnd"] == "1"
               for r in rows if r["role"] == "candidate" and r["scope"] == "series")
    assert reports == before


def test_saved_csv_uses_complete_validation_and_combined_bounds_before_output(tmp_path):
    paths = files(tmp_path)

    def fresh():
        return [api.replay(report_matrix(), c) for c in (baseline_config(), candidate_config())]

    over = api.replay(matrix(), cfg(start=0, end=500000 * 60))
    cap = api.replay(matrix(), cfg(start=0, end=499999 * 60))
    assert len(csv_rows(api.compare_reports_csv(cap, cap))) == 2
    mismatched = report_matrix()
    mismatched["data"]["result"][0]["values"][1][1] = "2.0"
    different_capture = api.replay(mismatched, candidate_config())

    def window(a, b):
        b["window"]["start"] = 60
        b["window"]["evaluations"] -= 1

    mutations = [
        (lambda a, b: [a, different_capture], "identical raw samples"),
        (lambda a, b: b["series"][0]["labels"].update(instance="other"), "exact label sets"),
        (window, "identical evaluation grids"),
        (lambda a, b: b["checkpoint"].update(originStart=-60), "missing origin history"),
        (lambda a, b: b["summary"].update(resolutions=b["summary"]["resolutions"] + 1), "inconsistent"),
        (lambda a, b: b["series"][0]["summary"].update(firstFiring=0), "inconsistent"),
        (lambda a, b: b["series"][0]["timeline"][1].update(state="inactive"), "inconsistent"),
        (lambda a, b: b["checkpoint"]["config"].update({"for": "1ms"}), "inconsistent"),
        (lambda a, b: b["config"].update(forSeconds=0.001), "inconsistent"),
        (lambda a, b: b.update(command="grid"), "schemaVersion 1 replay"),
        (lambda a, b: [over, over], "combined timeline exceeds 1000000"),
    ]
    for mutate, pattern in mutations:
        reports = fresh()
        result = mutate(*reports)
        bad_pair = result if isinstance(result, list) else reports
        for inputs in (bad_pair, bad_pair[::-1]):
            with pytest.raises(Exception, match=pattern):
                api.compare_reports_csv(*inputs)
            for p, v in zip(paths, inputs):
                write_json(p, v)
            before = [p.read_bytes() for p in paths]
            bad(run(*paths, "--format", "csv"), pattern)
            assert [p.read_bytes() for p in paths] == before


def test_saved_csv_strict_json_file_and_format_errors_are_atomic(tmp_path):
    paths = files(tmp_path)
    before = [p.read_bytes() for p in paths]
    for fmt in ([], [""], ["CSV"], ["tsv"], ["--format"]):
        bad(run(*paths, "--format", *fmt), "--format: expected json or csv")
    bad(run(*paths, "--format", "csv", "--format", "json"), "duplicate option --format")
    bad(run(*paths, "--unknown", "csv"), "unsupported argument")
    bad(run(*paths, "--format", "csv", paths[0]), "unsupported argument")
    bad(run(paths[0], "--format", "csv"), "expected exactly two.*paths|unsupported option")
    for raw, pattern in [("{broken", "invalid JSON"), ('{"x":1,"\\u0078":2}', "duplicate JSON object key"),
                         (b"\xff", "encoded data was not valid"), ("[" * 101 + "0" + "]" * 101, "nesting")]:
        write_raw(paths[1], raw)
        bad(run(*paths, "--format", "csv"), pattern)
        assert paths[1].read_bytes() == (raw if isinstance(raw, bytes) else raw.encode())
    write_raw(paths[1], before[1])
    bad(run(paths[0], paths[1] / "absent.json", "--format", "csv"), "ENOTDIR|ENOENT")
    bad(run(paths[0], tempfile.gettempdir(), "--format", "csv"), "regular local file")
    os.truncate(paths[1], 64 * 1024 * 1024 + 1)
    bad(run(*paths, "--format", "csv"), "exceeds 64 MiB")


# Both endpoints round-trip as Unix seconds. The interval does not round-trip
# through seconds * 1000: that would add 1 ms.
MS = 4503599627370480


def test_saved_csv_retains_integer_ms_aggregate_exposure_and_signed_deltas_at_large_safe_spans(tmp_path):
    base = cfg(evaluationInterval=f"{MS}ms", start=-0.01, end=4503599627370.47)
    data = matrix(series([[base["start"], "2"], [base["end"], "2"]]))
    reports = [api.replay(data, c) for c in (base, {**base, "for": "1ms"})]
    before = copy.deepcopy(reports)
    assert reports[0]["summary"]["firingDurationSeconds"] == MS / 1000, "aggregate must use exact integer-ms exposure"
    assert reports[0]["series"][0]["summary"]["firingDurationSeconds"] == MS / 1000
    from otelwrench.flapsim._js import js_string
    for p, sign in ((reports, 1), (reports[::-1], -1)):
        comparison = api.compare_reports(*p)
        for r in csv_rows(api.compare_reports_csv(*p))[2:]:
            assert r["firstFiringDelaySeconds"] == js_string(sign * MS / 1000)
            assert r["deltaFiringDurationSeconds"] == js_string(-sign * MS / 1000)
        assert comparison["delta"]["firstFiringDelaySeconds"] == sign * MS / 1000
        assert comparison["delta"]["firingDurationSeconds"] == -sign * MS / 1000
        assert [[s["firstFiringDelaySeconds"], s["firingDurationSeconds"]] for s in comparison["seriesDeltas"]] == \
            [[sign * MS / 1000, -sign * MS / 1000]]
    paths = files(tmp_path, reports)
    raw = [p.read_bytes() for p in paths]
    assert ok(run(*paths, "--format", "csv")) == api.compare_reports_csv(*reports)
    implicit = ok(run(*paths))
    assert ok(run(*paths, "--format", "json")) == implicit
    assert parse_json(implicit) == api.compare_reports(*reports)
    altered = copy.deepcopy(reports[0])
    altered["summary"]["firingDurationSeconds"] = 4503599627370.481
    write_json(paths[0], altered)
    bad(run(*paths, "--format", "csv"), "inconsistent")
    assert paths[1].read_bytes() == raw[1]
    assert reports == before


def test_exact_exposure_survives_multi_series_aggregation_checkpoint_join_and_compact_csv():
    base = cfg(evaluationInterval=f"{MS}ms", start=-0.01, end=4503599627370.47)
    candidate = {**base, "for": "1ms"}
    data = matrix(*[series([[base["start"], "2"], [base["end"], "2"]], {"instance": i}) for i in ("b", "a")])
    reports = [api.replay(data, c) for c in (base, candidate)]
    duration = 2 * MS / 1000
    assert reports[0]["summary"]["firingDurationSeconds"] == duration
    assert all(s["summary"]["firingDurationSeconds"] == MS / 1000 for s in reports[0]["series"])
    for i, c in enumerate((base, candidate)):
        def point(end):
            return matrix(*[series([[end, "2"]], {"instance": inst}) for inst in ("a", "b")])
        first = api.replay(point(c["start"]), {**c, "end": c["start"]})
        last = api.replay(point(c["end"]), {**c, "start": c["end"]}, first["checkpoint"])
        assert api.join([first, last]) == reports[i]
    compared = api.compare(data, base, candidate)
    assert compared["delta"]["firingDurationSeconds"] == -duration
    assert compared["delta"]["firstFiringDelaySeconds"] == MS / 1000
    saved = api.compare_reports_csv(*reports)
    assert api.compare_csv(data, base, candidate) == saved
    grid = {"for": ["1ms"], "keep_firing_for": ["0ms"], "maxEvaluations": 8}
    compact = api.grid(data, base, grid)
    assert compact["baseline"]["summary"]["firingDurationSeconds"] == duration
    assert compact["candidates"][0]["delta"]["firingDurationSeconds"] == -duration
    assert compact["candidates"][0]["delta"]["firstFiringDelaySeconds"] == MS / 1000
    assert all(s["delta"]["firingDurationSeconds"] == -MS / 1000 for s in compact["candidates"][0]["series"])
    assert api.grid_csv(data, base, grid) == saved
    assert [r["seriesId"] for r in csv_rows(saved)] == \
        ["null", '[["instance","a"]]', '[["instance","b"]]', "null", '[["instance","a"]]', '[["instance","b"]]']
