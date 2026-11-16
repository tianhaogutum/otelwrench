# SPDX-License-Identifier: Apache-2.0
from .support import assert_jitter_csv, bad, cfg, cfgd, cli, example_path as ex, matrix, ok, series, sha, write_json, write_raw

PATHS = [ex("jitter-query-range.json"), ex("baseline.json"), ex("candidate.json")]
ARGS = ["compare", PATHS[0], "--baseline", PATHS[1], "--candidate", PATHS[2]]


def test_cli_compare_format_csv_exports_independent_jitter_oracle_and_preserves_bytes():
    before = [open(p, "rb").read() for p in PATHS]
    output = ok(cli(*ARGS, "--format", "csv"))
    assert_jitter_csv(output)
    assert ok(cli(*ARGS, "--format", "csv")) == output
    assert [open(p, "rb").read() for p in PATHS] == before
    assert "compare MATRIX.json --baseline BASE.json --candidate CANDIDATE.json [--format json|csv]" in ok(cli("--help"))


def test_compare_json_remains_byte_compatible_and_explicit_json_equals_default():
    implicit = ok(cli(*ARGS))
    assert sha(implicit) == "ae4480742b69d0784545f495d80fbf52275a51ac999a380f1d58e1d0bdf1297f"
    assert ok(cli(*ARGS, "--format", "json")) == implicit


def test_compare_format_errors_mismatched_grids_and_failures_leave_empty_stdout(tmp_path):
    data, base, candidate = tmp_path / "matrix.json", tmp_path / "base.json", tmp_path / "candidate.json"
    for fmt in ([], [""], ["CSV"], ["tsv"], ["--baseline"]):
        bad(cli(*ARGS, "--format", *fmt), "--format: expected json or csv")
    bad(cli(*ARGS, "--format", "csv", "--format", "json"), "duplicate option --format")
    bad(cli(*ARGS, "--format", "csv", "--checkpoint-in", base), "unsupported argument")
    bad(cli("compare", PATHS[0], "--baseline", PATHS[1], "--format", "csv"), "missing required option --candidate")
    custom = ["compare", data, "--baseline", base, "--candidate", candidate, "--format", "csv"]
    for m, a, b, pattern in [
        (matrix(series([2])), cfg(start=0, end=60), cfg(start=0, end=120), "identical evaluation grids and windows"),
        (matrix(series([2])), cfg(), cfg(evaluationInterval="30s"), "identical evaluation grids and windows"),
        (matrix(series([[0, "2"], [120, "2"]])), cfg(missingData="error"), cfg(), "missing sample at 60"),
        (matrix(series([[0, "2"], [120, "2"]])), cfg(), cfg(missingData="error"), "missing sample at 60"),
        (matrix(series([[1, "2"], [60, "2"]])), cfg(), cfg(), "exact multiple"),
        (matrix(), cfg(start=0, end=1000000, evaluationInterval="1s"), cfg(), "1000000 series-evaluations"),
        (matrix(series([2])), cfg(), cfgd({"for": "-1s"}), "nonnegative duration"),
    ]:
        for path, value in zip((data, base, candidate), (m, a, b)):
            write_json(path, value)
        before = [p.read_bytes() for p in (data, base, candidate)]
        bad(cli(*custom), pattern)
        assert [p.read_bytes() for p in (data, base, candidate)] == before
    write_json(base, cfg())
    write_json(candidate, cfg())
    for raw, pattern in [('{"status":"success","status":"error"}', "duplicate JSON object key"),
                         ("{broken", "invalid JSON"), (b"\xff", "encoded data was not valid")]:
        write_raw(data, raw)
        bad(cli(*custom), pattern)
