# SPDX-License-Identifier: Apache-2.0
from otelwrench.flapsim import parse_json

from .support import bad, cli, csv_rows, example_path as ex, ok, write_json, write_raw

PATHS = [ex("grid-query-range.json"), ex("grid-baseline.json"), ex("timer-grid.json")]
ARGS = ["grid", PATHS[0], "--baseline", PATHS[1], "--grid", PATHS[2]]


def test_cli_grid_csv_is_opt_in_deterministic_preserves_inputs_and_retains_json():
    before = [open(p, "rb").read() for p in PATHS]
    output = ok(cli(*ARGS, "--format", "csv"))
    rows = csv_rows(output)
    assert len(rows) == 10
    assert [[r["role"], r["forMs"], r["keepFiringForMs"], r["firingDurationSeconds"]] for r in rows if r["scope"] == "aggregate"] == [
        ["baseline", "0", "0", "240"], ["candidate", "0", "0", "240"], ["candidate", "0", "120000", "420"],
        ["candidate", "60000", "0", "120"], ["candidate", "60000", "120000", "360"]]
    assert ok(cli(*ARGS, "--format", "csv")) == output
    implicit = cli(*ARGS)
    explicit = cli(*ARGS, "--format", "json")
    assert implicit.status == 0
    assert explicit.status == 0, explicit.stderr
    assert explicit.stdout == implicit.stdout
    assert parse_json(explicit.stdout)["command"] == "grid"
    assert "--format json|csv" in cli("--help").stdout
    assert [open(p, "rb").read() for p in PATHS] == before


def test_csv_flags_and_replay_failures_produce_no_partial_header_or_report(tmp_path):
    grid_path, base_path = tmp_path / "grid.json", tmp_path / "base.json"
    params = parse_json(open(PATHS[2], encoding="utf-8").read())
    base = parse_json(open(PATHS[1], encoding="utf-8").read())
    for argv, pattern in [
        ([*ARGS, "--format"], "--format: expected json or csv"),
        ([*ARGS, "--format", ""], "--format: expected json or csv"),
        ([*ARGS, "--format", "CSV"], "--format: expected json or csv"),
        ([*ARGS, "--format", "tsv"], "--format: expected json or csv"),
        ([*ARGS, "--format", "--baseline"], "--format: expected json or csv"),
        ([*ARGS, "--format", "csv", "--format", "json"], "duplicate option --format"),
        (["replay", PATHS[0], "--config", PATHS[1], "--format", "csv"], "unsupported argument"),
        (["checkpoint", PATHS[0], "--config", PATHS[1], "--format", "csv"], "unsupported argument"),
        (["join", PATHS[0], PATHS[0], "--format", "csv"], "unsupported option"),
        ([*ARGS, "--format", "csv", "--checkpoint-in", base_path], "unsupported argument"),
    ]:
        bad(cli(*argv), pattern)
    csv_args = ["grid", PATHS[0], "--baseline", base_path, "--grid", grid_path, "--format", "csv"]
    for grid_value, base_config, pattern in [
        ({**params, "maxEvaluations": 39}, base, "requires 40.*budget 39"),
        (params, {**base, "missingData": "error"}, "missing sample at 180"),
        ({**params, "for": ["60s", "1m"]}, base, "duplicate normalized duration"),
        ('{"for":[],"for":[]}', base, "duplicate JSON object key"),
    ]:
        if isinstance(grid_value, str):
            write_raw(grid_path, grid_value)
        else:
            write_json(grid_path, grid_value)
        write_json(base_path, base_config)
        bad(cli(*csv_args), pattern)
