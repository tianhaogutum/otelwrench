# SPDX-License-Identifier: Apache-2.0
import hashlib
import json
import os
import re
import subprocess
import sys

import pytest

from .experiment import run_experiment
from .generate_examples import create_examples
from .support import BASE_ARGS, EXAMPLES, PROG, ROOT, clone, run, write_json


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_example_command_text_output_includes_both_services_edges_stats_and_policy():
    r = run([*BASE_ARGS, "--config", EXAMPLES / "budget.json"])
    assert r.status == 0 and r.stderr == ""
    assert "tracediff: PASS" in r.stdout
    assert "20 traces, 40 spans" in r.stdout
    assert re.search(r"errorFraction=0.1", r.stdout)
    assert "database" in r.stdout
    assert re.search(r"edge .*count=20", r.stdout)


def test_json_report_and_stdout_are_deterministic_inputs_remain_unchanged():
    paths = [EXAMPLES / "baseline.json", EXAMPLES / "compatible.json"]
    before = [sha(p) for p in paths]
    a, b = run([*BASE_ARGS, "--format", "json"]), run([*BASE_ARGS, "--format", "json"])
    assert a.status == 0 and a.stderr == "" and a.stdout == b.stdout
    report = json.loads(a.stdout)
    assert report["schemaVersion"] == 1 and report["gate"]["failed"] is False
    assert before == [sha(p) for p in paths]


def test_regression_e2e_produces_complete_json_on_stdout_with_exit_1():
    r = run([EXAMPLES / "baseline.json", EXAMPLES / "regression.json", "--format", "json"])
    assert r.status == 1 and r.stderr == ""
    report = json.loads(r.stdout)
    assert report["gate"]["latencyRegressions"] == 1 and report["gate"]["structuralChanges"] == 2


def test_opt_in_corpus_gates_fail_with_unchanged_latency_default_policy_still_passes():
    args = [EXAMPLES / "baseline.json", EXAMPLES / "observation-candidate.json"]
    assert run(args).status == 0
    r = run([*args, "--config", EXAMPLES / "observation-budget.json", "--format", "json"])
    assert r.status == 1 and r.stderr == ""
    report = json.loads(r.stdout)
    assert report["gate"]["latencyRegressions"] == 0 and report["gate"]["structuralChanges"] == 0
    assert report["gate"]["countGateFailures"] == 2 and report["gate"]["errorFractionGateFailures"] == 1
    assert [e["changePercent"] for e in report["observationGates"]["count"]] == [50, 50]
    checkout = next(e for e in report["observationGates"]["errorFraction"] if e["group"]["operation"] == "checkout")
    assert checkout["baselineValue"] == 0.1 and checkout["candidateValue"] == 0.2
    assert checkout["changePercent"] == 100 and checkout["reason"] == "threshold-exceeded"
    text = run([*args, "--config", EXAMPLES / "observation-budget.json"])
    assert text.status == 1 and "observational corpus differences" in text.stdout


@pytest.mark.parametrize("name,key,target", [
    ("scope", "scope", "scope"), ("scope-schema", "schemaUrl", "scope"), ("resource-schema", "schemaUrl", "resource")])
def test_conflicting_empty_otlp_context_fails_as_json_input_error_on_stderr(tmp_path, name, key, target):
    a = create_examples()["baseline.json"]
    copy = clone(a["resourceSpans"][0])
    holder = copy["scopeSpans"][0] if target == "scope" else copy
    holder[key] = {} if key == "scope" else ""
    a["resourceSpans"].append(copy)
    p = write_json(tmp_path / f"{name}.json", a)
    r = run([BASE_ARGS[0], p, "--format", "json"])
    assert r.status == 2 and r.stdout == ""
    error = json.loads(r.stderr)["error"]
    assert error["code"] == "INPUT_ERROR" and "conflicting duplicate identity" in error["message"]


def test_malformed_optional_gate_configuration_is_exit_2_not_a_comparison_failure(tmp_path):
    p = tmp_path / "policy.json"
    p.write_text('{"errorFractionGate":{"maxIncreasePercent":-1}}')
    r = run([*BASE_ARGS, "--config", p, "--format", "json"])
    assert r.status == 2 and r.stdout == ""
    assert re.search(r"config.errorFractionGate.maxIncreasePercent", json.loads(r.stderr)["error"]["message"])


def test_real_format_mutation_experiment_is_independently_verified():
    result = run_experiment()
    assert result["verified"] is True and [c["exitCode"] for c in result["cases"]] == [0, 0, 1]


def test_committed_synthetic_examples_exactly_match_reproducible_generator():
    for name, data in create_examples().items():
        assert json.loads((EXAMPLES / name).read_text(encoding="utf-8")) == data, name


def test_stored_experiment_result_matches_current_independently_verified_result():
    assert json.loads((EXAMPLES / "experiment-results.json").read_text(encoding="utf-8")) == run_experiment()


B = ["examples/tracediff/baseline.json", "examples/tracediff/compatible.json"]


@pytest.mark.parametrize("name,args,expected", [
    ("missing inputs", [], r"expected BASELINE"),
    ("one input", [B[0]], r"expected BASELINE"),
    ("extra input", [*B, "extra.json"], r"expected BASELINE"),
    ("missing config option value", [*B, "--config"], r"requires a value"),
    ("missing format option value", [*B, "--format"], r"requires a value"),
    ("unsupported flag", [*B, "--execute"], r"unsupported option"),
    ("unsupported dry-run flag", [*B, "--dry-run"], r"unsupported option"),
    ("bad format", [*B, "--format", "xml"], r"must be text or json"),
    ("duplicate flag", [*B, "--format", "json", "--format", "json"], r"duplicate option"),
    ("missing input file", ["missing-input.json", B[1]], r"ENOENT"),
    ("missing config file", [*B, "--config", "missing-policy.json"], r"ENOENT"),
    ("directory input", ["examples", B[1]], r"regular file"),
    ("stdin is unsupported", ["-", B[1]], r"unsupported option"),
    ("no inferred URL support", ["https://example.invalid/trace.json", B[1]], r"ENOENT"),
])
def test_cli_rejects_with_exit_2(name, args, expected):
    r = run(args, cwd=ROOT)
    assert r.status == 2 and r.stdout == ""
    assert re.search(expected, r.stderr)


@pytest.mark.parametrize("name,data,expected", [
    ("duplicate JSON keys", b'{"data":[],"data":[]}', r"duplicate object key"),
    ("unsupported wrapper", b'{"traces":[]}', r"unsupported trace wrapper"),
    ("non-JSON executable content", b"process.exit(0)", r"expected JSON value"),
    ("invalid UTF-8", bytes([0xFF, 0xFE]), r"invalid UTF-8"),
    ("nonfinite JSON number", b'{"data":[],"total":1e999}', r"nonfinite"),
    ("empty input", b"", r"expected JSON value"),
])
def test_cli_invalid_input_emits_json_error_only_on_stderr(tmp_path, name, data, expected):
    p = tmp_path / "input.json"
    p.write_bytes(data)
    r = run(["--format", "json", p, BASE_ARGS[1]])
    assert r.status == 2 and r.stdout == ""
    error = json.loads(r.stderr)["error"]
    assert error["code"] == "INPUT_ERROR" and re.search(expected, error["message"])


def test_cli_rejects_oversized_regular_files_before_reading_content(tmp_path):
    p = tmp_path / "oversize.json"
    with open(p, "wb") as f:
        f.truncate(64 * 1024 * 1024 + 1)
    r = run([p, BASE_ARGS[1]])
    assert r.status == 2 and "exceeds 64 MiB" in r.stderr


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="FIFO check runs on Linux")
def test_cli_rejects_fifos_without_blocking(tmp_path):
    p = tmp_path / "pipe"
    os.mkfifo(p)
    r = run([p, BASE_ARGS[1]])
    assert r.status == 2 and "regular file" in r.stderr


def test_cli_invalid_config_and_actual_malformed_span_errors_are_exit_2(tmp_path):
    p = tmp_path / "policy.json"
    p.write_text('{"absoluteP95Ms":"NaN"}')
    r = run([*BASE_ARGS, "--config", p, "--format", "json"])
    assert r.status == 2 and "finite" in json.loads(r.stderr)["error"]["message"]
    data = create_examples()["baseline.json"]
    data["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["spanId"] = "bad"
    write_json(p, data)
    r = run([p, BASE_ARGS[1], "--format", "json"])
    assert r.status == 2 and "hex ID" in json.loads(r.stderr)["error"]["message"]


def test_cli_help_and_version_are_local_successful_commands():
    assert re.search(r"usage: otelwrench tracediff", run(["--help"]).stdout)
    assert run(["--help"]).status == 0 and run(["-h"]).status == 0
    assert run(["--version"]).stdout == "1.0.0\n" and run(["--version"]).status == 0


def test_cli_module_entry_point_runs_in_a_real_process():
    r = subprocess.run([sys.executable, "-m", "otelwrench.tracediff", *BASE_ARGS, "--format", "json"],
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["gate"]["failed"] is False
    bad = subprocess.run([sys.executable, "-m", "otelwrench.tracediff", "--bogus"], capture_output=True, text=True,
                         encoding="utf-8", timeout=120)
    assert bad.returncode == 2 and bad.stdout == "" and bad.stderr.startswith(f"{PROG}: unsupported option --bogus")


def test_cli_never_interprets_scripts_from_config_or_creates_output_files(tmp_path):
    p = tmp_path / "config.json"
    p.write_text('{"script":"touch SHOULD_NOT_EXIST"}')
    r = run([*BASE_ARGS, "--config", p], cwd=tmp_path)
    assert r.status == 2 and "unsupported field" in r.stderr
    assert sorted(os.listdir(tmp_path)) == ["config.json"]


def test_opt_in_witness_cli_exposes_source_backed_candidate_sets_and_preserves_default_report():
    args = [EXAMPLES / "witness-baseline.json", EXAMPLES / "witness-candidate.json"]
    plain = run([*args, "--format", "json"])
    assert plain.status == 0 and "structuralWitnesses" not in json.loads(plain.stdout)
    r = run([*args, "--witnesses", "--format", "json"])
    assert r.status == 0 and r.stderr == ""
    report = json.loads(r.stdout)
    view = report["structuralWitnesses"]
    root = next(s for s in view["subtrees"] if s["key"]["group"]["operation"] == "checkout")
    query = next(s for s in view["subtrees"] if s["key"]["group"]["operation"] == "query")
    assert root["pairing"] == "unique-structural-candidates" and query["pairing"] == "ambiguous"
    assert query["key"]["parentPath"] == [{"service": "shop", "operation": "checkout", "kind": "server"}]
    assert query["key"]["group"] == {"service": "database", "operation": "query", "kind": "client"}
    assert len(query["baseline"]) == 1 and len(query["candidate"]) == 2
    assert [[e["baselineCount"], e["candidateCount"]] for e in root["edgeChanges"]] == [[1, 2]]
    assert [[e["baselineCount"], e["candidateCount"]] for e in view["edgeChanges"]] == [[1, 2]]
    assert re.search(r"resourceSpans\[1\]", query["baseline"][0]["sourcePath"])
    del report["structuralWitnesses"]
    assert report == json.loads(plain.stdout)
    text = run([*args, "--witnesses"])
    assert text.status == 0 and "witness ambiguous" in text.stdout
    assert re.search(r"path edge .*1 -> 2", text.stdout)


def test_witness_cli_retains_full_reports_on_gate_failure_and_rejects_duplicate_flags_or_invalid_graphs(tmp_path):
    r = run([EXAMPLES / "baseline.json", EXAMPLES / "regression.json", "--witnesses", "--format", "json"])
    assert r.status == 1 and r.stderr == ""
    assert json.loads(r.stdout)["structuralWitnesses"]["edgeChanges"][0]["change"] == "added"
    dup = run([*BASE_ARGS, "--witnesses", "--witnesses", "--format", "json"])
    assert dup.status == 2 and dup.stdout == "" and "duplicate option" in dup.stderr
    data = create_examples()["witness-baseline.json"]
    data["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["parentSpanId"] = "0000000000000001"
    path = write_json(tmp_path / "cycle.json", data)
    bad = run([path, BASE_ARGS[1], "--witnesses", "--format", "json"])
    assert bad.status == 2 and bad.stdout == ""
    assert "cyclic parent graph" in json.loads(bad.stderr)["error"]["message"]
