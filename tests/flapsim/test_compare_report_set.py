# SPDX-License-Identifier: Apache-2.0
import copy
import os
import tempfile

import pytest

import otelwrench.flapsim as api
from otelwrench.flapsim import parse_json, to_json

from .support import (
    Probe, assert_set_csv, assert_set_json, bad, cfg, cli, csv_rows, forbid_warnings, joined_set, matrix, ok,
    reverse_keys, series, write_json, write_raw,
)


def run(*args):
    return cli("compare-report-set", *args)


def files(tmp_path, reports=None, prefix=""):
    reports = joined_set(api) if reports is None else reports
    paths = []
    for i, r in enumerate(reports):
        p = tmp_path / f"{prefix}{i}.json"
        write_json(p, r)
        paths.append(p)
    return paths


def test_library_report_set_independent_joined_two_label_oracle_and_immutability():
    base, *candidates = joined_set(api)
    before = copy.deepcopy([base, *candidates])
    assert_set_json(api.compare_report_set(base, candidates))
    assert_set_csv(api.compare_report_set_csv(base, candidates))
    assert [base, *candidates] == before


def test_cli_report_set_oracle_default_and_explicit_json_deterministic_csv_and_preserved_files(tmp_path):
    paths = files(tmp_path)
    raw = [p.read_bytes() for p in paths]
    json_text = ok(run(*paths))
    assert_set_json(parse_json(json_text))
    assert ok(run(*paths, "--format", "json")) == json_text
    csv_text = ok(run(*paths, "--format", "csv"))
    assert_set_csv(csv_text)
    assert ok(run(*paths, "--format", "csv")) == csv_text
    assert [p.read_bytes() for p in paths] == raw


def test_set_ordering_canonicalization_diagnostics_and_single_candidate_compatibility():
    base, *candidates = joined_set(api)
    expected = api.compare_report_set(base, candidates)
    scrambled = [reverse_keys(r) for r in (base, *candidates)]
    for r in scrambled:
        r["series"].reverse()
        r["checkpoint"]["series"].reverse()
    assert to_json(api.compare_report_set(scrambled[0], scrambled[1:])) == to_json(expected)
    reverse = api.compare_report_set(base, candidates[::-1])
    assert [c["report"] for c in reverse["candidates"]] == [c["report"] for c in expected["candidates"]][::-1]
    assert [c["candidateIndex"] for c in reverse["candidates"]] == [0, 1]
    for candidate in candidates:
        single, old = api.compare_report_set(base, [candidate]), api.compare_reports(base, candidate)
        assert single["baseline"] == old["baseline"]
        assert single["candidates"][0] == {"candidateIndex": 0, "report": old["candidate"], "delta": old["delta"],
                                           "seriesDeltas": old["seriesDeltas"]}
        assert api.compare_report_set_csv(base, [candidate]) == api.compare_reports_csv(base, candidate)
    base["sourceWarnings"] = ["w", "w"]
    candidates[1]["sourceInfos"] = ["independent", "independent"]
    diagnostics = api.compare_report_set(base, candidates)
    assert diagnostics["baseline"]["sourceWarnings"] == ["w", "w"]
    assert diagnostics["candidates"][1]["report"]["sourceInfos"] == ["independent", "independent"]
    assert len(diagnostics["candidates"][0]["report"]["sourceInfos"]) == 0


def empty(steps):
    return api.replay(matrix(), cfg(start=0, end=(steps - 1) * 60))


def test_set_count_budget_boundaries_charge_empty_matrices_and_preflight_before_any_replay(tmp_path):
    a = empty(1)
    for bad_candidates in (None, {}, [], [a] * 256, [None]):
        for fn in (api.compare_report_set, api.compare_report_set_csv):
            with pytest.raises(Exception, match="1 to 255|expected an object"):
                fn(a, bad_candidates)
    cap = empty(250000)
    assert api.compare_report_set(cap, [cap, cap, cap])["budget"]["requiredEvaluations"] == 1000000
    # A replay would read sourceWarnings; the cost rejection must happen first.
    over = forbid_warnings(empty(250001))
    for fn in (api.compare_report_set, api.compare_report_set_csv):
        with pytest.raises(Exception, match="combined timeline exceeds 1000000"):
            fn(over, [over, over, over])
    full = api.replay(matrix(series([0])), cfg(start=0, end=0))
    full["window"]["end"] = 250000 * 60
    full["window"]["evaluations"] = 250001
    full["checkpoint"]["evaluatedThrough"] = full["window"]["end"]
    with pytest.raises(Exception, match="combined timeline exceeds 1000000"):
        api.compare_report_set(full, [full, full, full])
    many = api.compare_report_set(a, [a] * 255)
    assert len(many["candidates"]) == 255
    assert many["budget"]["requiredEvaluations"] == 256
    assert [c["candidateIndex"] for c in many["candidates"]] == list(range(255))
    paths = files(tmp_path, [a, cap, empty(250001)])
    assert len(csv_rows(ok(run(*([paths[0]] * 256), "--format", "csv")))) == 256
    for args in ([], [paths[0]], ["/absent.json"] * 257):
        bad(run(*args), "2 to 256")
    assert parse_json(ok(run(*([paths[1]] * 4))))["budget"]["requiredEvaluations"] == 1000000
    for fmt in ("json", "csv"):
        bad(run(*([paths[2]] * 4), "--format", fmt), "combined timeline exceeds 1000000")


def test_baseline_verification_count_is_independent_of_candidate_count():
    base, candidate = joined_set(api)[:2]
    reads = [0]

    def count():
        reads[0] += 1
        return []

    probe = Probe({**base, "sourceWarnings": []}, count)
    api.compare_report_set(probe, [candidate])
    once = reads[0]
    reads[0] = 0
    api.compare_report_set(probe, [candidate] * 4)
    assert once > 0
    assert reads[0] == once, "baseline verification must not repeat per candidate"


def _window(r):
    r["window"]["start"] = 1.061
    r["window"]["evaluations"] -= 1


def _interval(r):
    r["config"]["evaluationIntervalSeconds"] = 0.03
    r["checkpoint"]["config"]["evaluationInterval"] = "30ms"
    r["window"]["evaluations"] = 13


INVALID = [
    (lambda r: r.update(schemaVersion=2), "schemaVersion 1 replay"),
    (lambda r: r.update(command="grid"), "schemaVersion 1 replay"),
    (lambda r: r["checkpoint"].update(originStart=0.941), "missing origin history"),
    (_window, "identical evaluation grids"),
    (_interval, "identical evaluation grids"),
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
]


def test_any_invalid_report_in_baseline_first_or_last_candidate_rejects_atomically(tmp_path):
    paths = files(tmp_path)
    for mutate, pattern in INVALID:
        for position in range(3):
            reports = joined_set(api)
            mutate(reports[position])
            for fn in (api.compare_report_set, api.compare_report_set_csv):
                with pytest.raises(Exception, match=pattern):
                    fn(reports[0], reports[1:])
            for p, r in zip(paths, reports):
                write_json(p, r)
            raw = [p.read_bytes() for p in paths]
            for fmt in ("json", "csv"):
                bad(run(*paths, "--format", fmt), pattern)
            assert [p.read_bytes() for p in paths] == raw


def make_unsafe():
    unsafe = api.replay(matrix(series([2]), series([2], {"instance": "b"})), cfg())
    unsafe["config"]["evaluationIntervalSeconds"] = 4503599627370.495
    unsafe["checkpoint"]["config"]["evaluationInterval"] = "4503599627370495ms"
    unsafe["window"]["end"] = 9007199254740.99
    unsafe["window"]["evaluations"] = 3
    unsafe["checkpoint"]["evaluatedThrough"] = unsafe["window"]["end"]
    return unsafe


def test_set_shares_complete_raw_capture_including_missing_null_nan_identity_and_safe_exposure(tmp_path):
    base_cfg = cfg(start=0, end=120)
    inputs = [matrix(series([[0, "NaN"], [120, "2"]])), matrix(series([[0, "NaN"], [60, "NaN"], [120, "2"]])),
              matrix(series([[0, "NaN"], [60, "0"], [120, "2"]]))]
    reports = [api.replay(m, base_cfg) for m in inputs]
    assert api.compare_report_set(reports[0], [reports[0]])["baseline"]["summary"]["missingSamples"] == 1
    paths = files(tmp_path, reports)
    for fn in (api.compare_report_set, api.compare_report_set_csv):
        with pytest.raises(Exception, match="identical raw samples"):
            fn(reports[0], reports[1:])
    for fmt in ("json", "csv"):
        bad(run(*paths, "--format", fmt), "identical raw samples")
    unsafe = make_unsafe()
    unsafe_paths = files(tmp_path, [unsafe, unsafe], prefix="unsafe-")
    for fn in (api.compare_report_set, api.compare_report_set_csv):
        with pytest.raises(Exception, match="aggregate duration exceeds safe integer"):
            fn(unsafe, [unsafe])
    for fmt in ("json", "csv"):
        bad(run(*unsafe_paths, "--format", fmt), "aggregate duration exceeds safe integer")


def test_set_strict_files_json_and_misplaced_duplicate_missing_invalid_format_fail_with_empty_stdout(tmp_path):
    paths = files(tmp_path)
    original = paths[2].read_bytes()
    for suffix in (["--format"], ["--format", ""], ["--format", "tsv"], ["--format", "CSV"],
                   ["--format", "csv", "--format", "json"], ["--unknown", "json"], ["--format", "csv", paths[0]]):
        bad(run(*paths, *suffix), "--format: expected json or csv|duplicate option|unsupported argument")
    bad(run(paths[0], "--format", "json", paths[1]), "2 to 256")
    for raw, pattern in [("{broken", "invalid JSON"), ('{"x":1,"\\u0078":2}', "duplicate JSON object key"),
                         (b"\xff", "encoded data was not valid"), ("[" * 101 + "0" + "]" * 101, "nesting")]:
        write_raw(paths[2], raw)
        for fmt in ("json", "csv"):
            bad(run(*paths, "--format", fmt), pattern)
        assert paths[2].read_bytes() == (raw if isinstance(raw, bytes) else raw.encode())
    write_raw(paths[2], original)
    for fmt in ("json", "csv"):
        bad(run(*paths[:2], paths[2] / "absent.json", "--format", fmt), "ENOENT|ENOTDIR")
        bad(run(*paths[:2], tempfile.gettempdir(), "--format", fmt), "regular local file")
    os.truncate(paths[2], 64 * 1024 * 1024 + 1)
    for fmt in ("json", "csv"):
        bad(run(*paths, "--format", fmt), "exceeds 64 MiB")


def test_set_preserves_exact_integer_ms_arithmetic_and_maximum_timer_identity():
    ms = 4503599627370480
    base = cfg(evaluationInterval=f"{ms}ms", start=-0.01, end=4503599627370.47)
    data = matrix(*[series([[base["start"], "2"], [base["end"], "2"]], {"instance": i}) for i in ("b", "a")])
    reports = [api.replay(data, c) for c in (base, {**base, "for": "1ms"}, {**base, "threshold": {"op": ">", "value": 3}})]
    result = api.compare_report_set(reports[0], reports[1:])
    assert result["baseline"]["summary"]["firingDurationSeconds"] == 2 * ms / 1000
    assert result["candidates"][0]["delta"]["firingDurationSeconds"] == -2 * ms / 1000
    assert result["candidates"][0]["delta"]["firstFiringDelaySeconds"] == ms / 1000
    rows = csv_rows(api.compare_report_set_csv(reports[1], [reports[0], reports[2]]))
    from otelwrench.flapsim._js import js_string
    assert rows[3]["deltaFiringDurationSeconds"] == js_string(2 * ms / 1000)
    assert rows[3]["firstFiringDelaySeconds"] == js_string(-ms / 1000)
    assert rows[6]["firstFiringDelaySeconds"] == "null"
    mx = "9007199254740991ms"
    e = api.replay(matrix(), {**cfg(evaluationInterval=mx, keep_firing_for="1ms", start=0, end=0), "for": mx})
    assert all(r["evaluationIntervalMs"] == "9007199254740991" and r["forMs"] == "9007199254740991"
               and r["keepFiringForMs"] == "1" for r in csv_rows(api.compare_report_set_csv(e, [e, e])))
