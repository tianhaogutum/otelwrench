# SPDX-License-Identifier: Apache-2.0
import os

import pytest

import otelwrench.flapsim as api
from otelwrench.flapsim import compare

from .support import (
    EXAMPLES, bad, cfg, cfgd, cli, cli_process, example_path as ex, experiment_result, jitter_text, matrix,
    series, success, write_json, write_raw,
)


def test_cli_replay_parses_real_api_json_into_expected_timeline_and_counts():
    r = success(cli("replay", ex("query-range.json"), "--config", ex("alert.json")))
    assert r["schemaVersion"] == 1
    assert r["window"]["evaluations"] == 7
    assert [[t["timestamp"], t["to"]] for t in r["series"][0]["transitions"]] == [[60, "pending"], [120, "firing"]]
    assert r["series"][0]["episodes"] == [{"start": 120, "end": None, "durationSeconds": 240, "censored": True}]


def test_cli_compare_five_baseline_and_two_candidate_episodes():
    r = success(cli("compare", ex("jitter-query-range.json"), "--baseline", ex("baseline.json"), "--candidate", ex("candidate.json")))
    assert [[e["start"], e["end"]] for e in r["baseline"]["series"][0]["episodes"]] == \
        [[60, 180], [240, 360], [420, 540], [720, 840], [900, 1020]]
    assert [[e["start"], e["end"]] for e in r["candidate"]["series"][0]["episodes"]] == [[120, 660], [780, 1140]]
    assert r["baseline"]["summary"]["firstFiring"] == 60
    assert r["candidate"]["summary"]["firstFiring"] == 120
    assert r["baseline"]["summary"]["firingDurationSeconds"] == 600
    assert r["candidate"]["summary"]["firingDurationSeconds"] == 900
    assert r["baseline"]["summary"]["resolutions"] == 5
    assert r["candidate"]["summary"]["resolutions"] == 2
    assert r["delta"] == {"firstFiringDelaySeconds": 60, "firingDurationSeconds": 300, "firingEpisodes": -3,
                          "resolutions": -3, "openAtEnd": 0}
    assert r["seriesDeltas"][0]["firstFiringDelaySeconds"] == 60


def test_repeat_cli_replay_and_compare_are_byte_identical():
    args = ["compare", ex("jitter-query-range.json"), "--baseline", ex("baseline.json"), "--candidate", ex("candidate.json")]
    a, b = cli(*args), cli(*args)
    success(a)
    success(b)
    assert a.stdout == b.stdout
    replay_args = ["replay", ex("query-range.json"), "--config", ex("alert.json")]
    assert cli(*replay_args).stdout == cli(*replay_args).stdout


def test_compare_identical_config_is_zero_absent_first_firing_gives_null_delay():
    c = cfg()
    r = compare(matrix(series([0, 0])), c, c)
    assert r["delta"] == {"firstFiringDelaySeconds": None, "firingDurationSeconds": 0, "firingEpisodes": 0,
                          "resolutions": 0, "openAtEnd": 0}
    never = compare(matrix(series([2, 0])), cfg(), cfgd({"for": "120s"}))
    assert never["delta"]["firstFiringDelaySeconds"] is None
    assert never["delta"]["firingEpisodes"] == -1


def test_experiment_result_and_saved_synthetic_fixture_reproduce_byte_for_byte():
    assert jitter_text() == (EXAMPLES / "jitter-query-range.json").read_bytes().decode("utf-8")
    assert experiment_result(api) == (EXAMPLES / "experiment-result.json").read_bytes().decode("utf-8")


def test_cli_help_required_arguments_unknown_flags_and_duplicate_flags():
    help_result = cli("--help")
    assert help_result.status == 0
    assert "offline captured Prometheus" in help_result.stdout
    for args in [[], ["nonsense"], ["replay"], ["replay", ex("query-range.json")],
                 ["compare", ex("query-range.json"), "--baseline", ex("baseline.json")],
                 ["replay", ex("query-range.json"), "--config"],
                 ["replay", ex("query-range.json"), "--config", ex("alert.json"), "--config", ex("alert.json")],
                 ["replay", ex("query-range.json"), "--config", ex("alert.json"), "--dry-run"],
                 ["replay", ex("query-range.json"), "--regex", ".*"]]:
        bad(cli(*args), "alert-flapsim:")


def test_real_process_entry_point_help_and_failure():
    result = cli_process("--help")
    assert result.status == 0
    assert "otelwrench alert-flapsim replay MATRIX.json" in result.stdout
    failed = cli_process("nonsense")
    assert failed.status == 1 and failed.stdout == ""
    assert failed.stderr.startswith("otelwrench alert-flapsim: expected command")


def test_subcommand_help_lists_usage():
    for command in ("replay", "checkpoint", "compare", "compare-reports", "compare-report-set", "grid", "join"):
        result = cli(command, "--help")
        assert result.status == 0
        assert result.stdout.startswith(f"usage: otelwrench alert-flapsim {command} ")


def test_cli_failure_paths_invalid_local_data_missing_file_promql_and_corrupt_utf8(tmp_path):
    data, config = tmp_path / "matrix.json", tmp_path / "config.json"
    write_json(data, matrix(series([2, 0])))
    write_json(config, cfg(missingData="error", start=0, end=120))
    bad(cli("replay", data, "--config", config), "missing sample at 120")
    write_json(config, cfg(expr="process.exit(99)"))
    bad(cli("replay", data, "--config", config), 'unsupported field "expr"')
    write_json(config, cfg())
    write_raw(data, '{"status":"success","status":"error"}')
    bad(cli("replay", data, "--config", config), "duplicate JSON object key")
    write_raw(data, "{broken")
    bad(cli("replay", data, "--config", config), "invalid JSON")
    write_raw(data, b"\xff")
    bad(cli("replay", data, "--config", config), "encoded data was not valid")
    write_json(data, {"status": "error", "error": "execution failed"})
    bad(cli("replay", data, "--config", config), "expected status success")
    write_json(data, {"status": "success", "data": {"resultType": "scalar", "result": [0, "1"]}})
    bad(cli("replay", data, "--config", config), "unsupported resultType")
    bad(cli("replay", tmp_path / "absent.json", "--config", config), "ENOENT")
    bad(cli("replay", tmp_path, "--config", config), "regular local file")
    os.truncate(data, 64 * 1024 * 1024 + 1)
    bad(cli("replay", data, "--config", config), "exceeds 64 MiB")


def test_cli_never_modifies_its_input_or_config_files():
    before = (EXAMPLES / "query-range.json").read_bytes()
    config_before = (EXAMPLES / "alert.json").read_bytes()
    success(cli("replay", ex("query-range.json"), "--config", ex("alert.json")))
    assert (EXAMPLES / "query-range.json").read_bytes() == before
    assert (EXAMPLES / "alert.json").read_bytes() == config_before


def test_millisecond_compare_deltas_are_exact_at_nonzero_epoch_offsets():
    data = matrix(series([[1.001, "2"], [1.061, "2"], [1.121, "0"]]))
    c = cfg(evaluationInterval="60ms")
    r = compare(data, c, {**c, "for": "60ms"})
    assert r["delta"]["firstFiringDelaySeconds"] == 0.06
    assert r["delta"]["firingDurationSeconds"] == -0.06


def test_cli_checkpoint_export_import_matches_full_replay_and_rejects_invalid_state(tmp_path):
    checkpoint_path = tmp_path / "state.json"
    first_args = [ex("checkpoint-window-1.json"), "--config", ex("checkpoint-alert-1.json")]
    first = success(cli("replay", *first_args))
    exported = success(cli("checkpoint", *first_args))
    assert exported == first["checkpoint"]
    assert exported["series"][0]["state"] == "pending"
    assert exported["series"][0]["pendingSince"] == 60
    write_json(checkpoint_path, exported)
    args = [ex("checkpoint-window-2.json"), "--config", ex("checkpoint-alert-2.json"), "--checkpoint-in", checkpoint_path]
    second = success(cli("replay", *args))
    whole = success(cli("replay", ex("query-range.json"), "--config", ex("alert.json")))
    assert [*first["series"][0]["timeline"], *second["series"][0]["timeline"]] == whole["series"][0]["timeline"]
    assert success(cli("checkpoint", *args)) == whole["checkpoint"]
    assert cli("replay", *args).stdout == cli("replay", *args).stdout
    exported["evaluatedThrough"] = 0
    write_json(checkpoint_path, exported)
    bad(cli("replay", *args), "must be contiguous")
    write_raw(checkpoint_path, '{"schemaVersion":1,"schemaVersion":1}')
    bad(cli("replay", *args), "duplicate JSON object key")
    bad(cli("compare", ex("query-range.json"), "--baseline", ex("baseline.json"), "--candidate", ex("candidate.json"),
            "--checkpoint-in", checkpoint_path), "unsupported argument")


def test_cli_grid_compares_every_explicit_timer_pair_deterministically_and_preserves_files():
    paths = [ex("grid-query-range.json"), ex("grid-baseline.json"), ex("timer-grid.json")]
    before = [open(p, "rb").read() for p in paths]
    args = ["grid", paths[0], "--baseline", paths[1], "--grid", paths[2]]
    output = cli(*args)
    r = success(output)
    assert r["command"] == "grid"
    assert r["budget"]["requestedEvaluations"] == 40
    assert [[c["summary"]["transitionCount"], c["summary"]["firingDurationSeconds"], c["delta"]["firstFiringDelaySeconds"]]
            for c in r["candidates"]] == [[5, 240, 0], [1, 420, 0], [7, 120, 60], [2, 360, 60]]
    assert cli(*args).stdout == output.stdout
    assert [open(p, "rb").read() for p in paths] == before


def test_cli_grid_refuses_invalid_budgets_missing_data_error_and_invalid_flags(tmp_path):
    base, grid_path = tmp_path / "baseline.json", tmp_path / "grid.json"
    parameters = {"for": ["0s", "60s"], "keep_firing_for": ["0s", "120s"], "maxEvaluations": 39}
    config = cfg(start=0, end=420)
    write_json(base, config)
    write_json(grid_path, parameters)
    args = ["grid", ex("grid-query-range.json"), "--baseline", base, "--grid", grid_path]
    bad(cli(*args), "requires 40.*budget 39")
    write_json(grid_path, {**parameters, "maxEvaluations": 40})
    write_json(base, {**config, "missingData": "error"})
    bad(cli(*args), "missing sample at 180")
    write_json(grid_path, {**parameters, "for": ["-1s"]})
    bad(cli(*args), "nonnegative duration")
    write_raw(grid_path, '{"for":["0s"],"for":["60s"],"keep_firing_for":["0s"],"maxEvaluations":40}')
    bad(cli(*args), "duplicate JSON object key")
    bad(cli(*args, "--checkpoint-in", base), "unsupported argument")
    bad(cli("grid", ex("grid-query-range.json"), "--baseline", base), "missing required option --grid")
    bad(cli(*args, "--grid", grid_path), "duplicate option --grid")


def test_cli_joins_saved_checkpoint_backed_reports_deterministically(tmp_path):
    first = success(cli("replay", ex("checkpoint-window-1.json"), "--config", ex("checkpoint-alert-1.json")))
    checkpoint_path = tmp_path / "state.json"
    write_json(checkpoint_path, first["checkpoint"])
    second = success(cli("replay", ex("checkpoint-window-2.json"), "--config", ex("checkpoint-alert-2.json"),
                         "--checkpoint-in", checkpoint_path))
    paths = [tmp_path / "first.json", tmp_path / "second.json"]
    for p, r in zip(paths, [first, second]):
        write_json(p, r)
    before = [p.read_bytes() for p in paths]
    output = cli("join", *paths)
    joined = success(output)
    assert joined == success(cli("replay", ex("query-range.json"), "--config", ex("alert.json")))
    assert joined["summary"]["firingDurationSeconds"] == 240
    assert cli("join", *paths).stdout == output.stdout
    assert [p.read_bytes() for p in paths] == before
    bad(cli("join", paths[1], paths[0]), "originStart")
    second["series"][0]["timeline"][0]["state"] = "pending"
    write_json(paths[1], second)
    bad(cli("join", *paths), "inconsistent")
    write_raw(paths[1], '{"command":"replay","command":"replay"}')
    bad(cli("join", *paths), "duplicate JSON object key")
    bad(cli("join", paths[0], tmp_path / "absent.json"), "ENOENT")
    bad(cli("join", paths[0], tmp_path), "regular local file")
    bad(cli("join"), "2 to 256")
    bad(cli("join", paths[0]), "2 to 256")
    bad(cli("join", *paths, "--config", checkpoint_path), "unsupported option")
    bad(cli("join", *([paths[0]] * 257)), "2 to 256")


@pytest.mark.parametrize("args", [["replay", "--help", "x"], ["grid", "-h", "--baseline"]])
def test_help_flags_are_only_recognized_alone(args):
    bad(cli(*args), "alert-flapsim:")
