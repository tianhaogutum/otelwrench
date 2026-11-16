"""otelwrench preflight: combined /metrics verdict built from promdiff, prom-names and cardinality."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from otelwrench.preflight import SCHEMA
from otelwrench.preflight.cli import Style, main, render_text
from otelwrench.preflight.core import build, explain_rename, infer_metric_renames, load_side
from otelwrench.preflight.queries import identifiers, load_queries
from otelwrench.promdiff.config import Config
from otelwrench.promdiff.parser import parse_snapshot

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "preflight"
BEFORE, AFTER = str(EXAMPLE / "before.prom"), str(EXAMPLE / "after.prom")
QUERY_FILES = [str(EXAMPLE / "checkout-dashboard.json"), str(EXAMPLE / "checkout-alerts.yaml")]
ORACLE = json.loads((EXAMPLE / "expected.json").read_text())


def example(fail_on="error", config=None):
    queries = [q for path in QUERY_FILES for q in load_queries(path)]
    return build(load_side(BEFORE), load_side(AFTER), queries, config or Config(), fail_on)


def write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text)
    return str(path)


def run_cli(args, capsys):
    code = main(args)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --- naming rules come from prom-names -------------------------------------------------

@pytest.mark.parametrize("old,new,counter,unit,rescaled,direction", [
    ("http_server_duration", "http_server_duration_milliseconds", False, "ms", False, "suffixes_added"),
    ("checkout_orders", "checkout_orders_total", True, "", False, "suffixes_added"),
    ("queue_wait_milliseconds", "queue_wait_seconds", False, "s", True, "suffixes_added"),
    ("payload", "payload_bytes", False, "By", False, "suffixes_added"),
    ("cpu_utilization", "cpu_utilization_ratio", False, "1", False, "suffixes_added"),
    ("request_size_bytes", "request_size", False, "By", False, "suffixes_removed"),
])
def test_explain_rename_uses_prom_names_rules(old, new, counter, unit, rescaled, direction):
    why = explain_rename(old, new, counter)
    assert why is not None
    assert (why["otlp_unit"], why["rescaled"], why["direction"]) == (unit, rescaled, direction)
    assert why["steps"]


@pytest.mark.parametrize("old,new,counter", [
    ("http_server_duration", "http_server_latency_milliseconds", False),  # different stem
    ("checkout_orders", "checkout_orders_total", False),                   # _total only for counters
    ("queue_wait", "queue_wait_minutes", False),                           # unit outside the table
])
def test_explain_rename_rejects_unrelated_names(old, new, counter):
    assert explain_rename(old, new, counter) is None


def test_rename_requires_same_type_and_one_to_one():
    before = parse_snapshot("# TYPE a gauge\na 1\n# TYPE b counter\nb 1\n")
    after = parse_snapshot("# TYPE a_bytes counter\na_bytes 1\n# TYPE b_total counter\nb_total 1\n")
    assert [(old, new) for old, new, _ in infer_metric_renames(before, after)] == [("b", "b_total")]
    ambiguous = parse_snapshot("# TYPE x gauge\nx 1\n")
    targets = parse_snapshot("# TYPE x_bytes gauge\nx_bytes 1\n# TYPE x_seconds gauge\nx_seconds 1\n")
    assert infer_metric_renames(ambiguous, targets) == []


# --- the bundled example against the hand-written oracle --------------------------------

def test_example_findings_match_oracle():
    doc = example()
    assert doc["schema"] == SCHEMA
    renamed = {f["family"]: f["renamed_to"] for f in doc["findings"] if f["kind"] == "renamed"}
    assert renamed == ORACLE["changes"]["renamed"]
    assert [f["family"] for f in doc["findings"] if f["kind"] == "renamed" and f["rescaled"]] == ORACLE["changes"]["rescaled"]
    assert doc["rename_map"]["labels"] == ORACLE["changes"]["label_renamed"]
    assert [f["family"] for f in doc["findings"] if f["kind"] == "family_removed"] == ORACLE["changes"]["removed"]
    types = {f["family"]: [f["before"], f["after"]] for f in doc["findings"] if f["kind"] == "type_changed"}
    assert types == ORACLE["changes"]["type_changed"]
    added = {f["family"]: f["added_labels"] for f in doc["findings"] if f["kind"] == "label_schema_changed"}
    assert added == ORACLE["changes"]["label_added"]
    assert [doc["summary"]["series_before"], doc["summary"]["series_after"]] == ORACLE["changes"]["series"]
    assert doc["summary"]["errors"] == 8 and doc["summary"]["warnings"] == 2 and doc["summary"]["failed"]


def test_example_queries_match_oracle():
    doc = example()
    flagged = {q["name"]: q["hit"] for q in doc["queries"]}
    assert flagged == {name: label["broken"] for name, label in ORACLE["queries"].items()}
    assert doc["summary"]["queries_hit"] == 9 and doc["summary"]["queries"] == 14
    assert doc["summary"]["queries_by_kind"] == {
        "alert": {"hit": 3, "total": 4}, "panel": {"hit": 5, "total": 8}, "record": {"hit": 1, "total": 2}}


def test_growth_is_traced_to_one_label_with_cardinality_collision_check():
    growth = [f for f in example()["findings"] if f["kind"] == "series_growth"]
    assert len(growth) == 1 and growth[0]["source"] == "promdiff + cardinality"
    driver = growth[0]["drivers"][0]
    assert (driver["family"], driver["label"], driver["values_before"], driver["values_after"]) == (
        "rpc_client_calls_total", "net_sock_peer_port", 0, 12)
    assert driver["drop_check"] == {"safe": False, "series_before": 24, "series_after": 2, "collision_groups": 2}
    assert growth[0]["next"] == [f"otelwrench cardinality {AFTER} --drop-labels net_sock_peer_port "
                                 "--metric rpc_client_calls_total"]


def test_every_finding_has_a_follow_up_command():
    for finding in example()["findings"]:
        assert finding["next"] and all(cmd.startswith("otelwrench ") for cmd in finding["next"]), finding
        assert finding["fix"]


def test_emitted_rename_map_and_allowlist_make_promdiff_pass(tmp_path):
    from otelwrench.promdiff.cli import main as promdiff
    doc = example()
    renames = write(tmp_path, "renames.json", json.dumps(doc["rename_map"]))
    allow = write(tmp_path, "allow.json", json.dumps(doc["allowlist"]))
    assert promdiff([BEFORE, AFTER, "--rename-map", renames]) == 1
    assert promdiff([BEFORE, AFTER, "--rename-map", renames, "--allowlist", allow]) == 0


# --- CLI ---------------------------------------------------------------------------------

def test_cli_text_report(capsys):
    code, out, err = run_cli([BEFORE, AFTER, *[a for p in QUERY_FILES for a in ("--queries", p)]], capsys)
    assert code == 1 and err == ""
    assert "\x1b[" not in out  # no colour when stdout is not a terminal
    for text in ("Renamed by the OTLP-to-Prometheus naming rules", "queue_wait_milliseconds -> queue_wait_seconds",
                 "Label renamed", "Family removed", "Type changed", "Series growth over budget",
                 "9 of 14 queries read something that changed", "FAIL (exit 1): 8 errors, 2 warnings",
                 "--emit rename-map > renames.json"):
        assert text in out, text


def test_cli_json_report(capsys):
    code, out, _ = run_cli([BEFORE, AFTER, "--format", "json"], capsys)
    doc = json.loads(out)
    assert code == 1 and doc["schema"] == SCHEMA and doc["summary"]["queries"] == 0
    assert all(f["queries"] == [] for f in doc["findings"])


def test_cli_emit_modes_exit_0(capsys):
    code, out, _ = run_cli([BEFORE, AFTER, "--emit", "rename-map"], capsys)
    assert code == 0 and json.loads(out)["metrics"] == ORACLE["changes"]["renamed"]
    code, out, _ = run_cli([BEFORE, AFTER, "--emit", "allowlist"], capsys)
    assert code == 0 and {e["kind"] for e in json.loads(out)} >= {"family_removed", "type_changed", "series_growth"}


def test_identical_captures_pass(capsys):
    code, out, _ = run_cli([BEFORE, BEFORE], capsys)
    assert code == 0 and "pass (exit 0): 0 errors, 0 warnings" in out


@pytest.mark.parametrize("fail_on,expected", [("error", 0), ("warning", 1), ("none", 0)])
def test_fail_on_threshold(tmp_path, capsys, fail_on, expected):
    before = write(tmp_path, "b.prom", "# HELP x Old.\n# TYPE x gauge\nx 1\n")
    after = write(tmp_path, "a.prom", "# HELP x New.\n# TYPE x gauge\nx 1\n")  # help_changed: warning
    code, _, _ = run_cli([before, after, "--fail-on", fail_on], capsys)
    assert code == expected


def test_growth_budget_flag(tmp_path, capsys):
    before = write(tmp_path, "b.prom", '# TYPE x gauge\nx{id="1"} 1\nx{id="2"} 1\n')
    after = write(tmp_path, "a.prom", '# TYPE x gauge\nx{id="1"} 1\nx{id="2"} 1\nx{id="3"} 1\n')
    assert run_cli([before, after, "--format", "json"], capsys)[0] == 1          # +50% > 20%
    assert run_cli([before, after, "--max-growth", "60", "--format", "json"], capsys)[0] == 0


@pytest.mark.parametrize("args", [["missing.prom", AFTER], [BEFORE, AFTER, "--queries", "missing.yaml"]])
def test_input_errors_exit_2(args, capsys):
    code, out, err = run_cli(args, capsys)
    assert code == 2 and out == "" and err.startswith("otelwrench preflight: ")


def test_malformed_exposition_exits_2(tmp_path, capsys):
    bad = write(tmp_path, "bad.prom", "not a metric line at all {\n")
    code, _, err = run_cli([BEFORE, bad], capsys)
    assert code == 2 and "bad.prom" in err


def test_usage_error_exits_2(capsys):
    with pytest.raises(SystemExit) as exc:
        main([BEFORE])
    assert exc.value.code == 2


def test_routed_through_dispatcher():
    result = subprocess.run([sys.executable, "-m", "otelwrench", "preflight", "--help"],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0 and result.stdout.startswith("usage: otelwrench preflight")


def test_render_text_colour_codes_only_when_enabled():
    doc = example()
    assert "\x1b[" in render_text(doc, Style(True)) and "\x1b[" not in render_text(doc, Style(False))


# --- query loading -----------------------------------------------------------------------

def test_identifiers_skip_strings_ranges_and_comments():
    names = identifiers('sum by (status_code) (rate(http_requests_total{peer="queue_wait"}[5m])) # old_metric')
    assert {"status_code", "http_requests_total", "peer"} <= names
    assert not {"queue_wait", "old_metric", "5m"} & names


def test_load_queries_formats(tmp_path):
    rule = write(tmp_path, "rule.yaml", "apiVersion: monitoring.coreos.com/v1\nkind: PrometheusRule\n"
                                        "spec:\n  groups:\n  - name: g\n    rules:\n    - alert: A\n      expr: up == 0\n")
    assert [(q.name, q.kind) for q in load_queries(rule)] == [("A", "alert")]
    text = write(tmp_path, "queries.txt", "# comment\n\nlatency: rate(x[5m])\nsum(y)\n")
    assert [(q.name, q.expr) for q in load_queries(text)] == [("latency", "rate(x[5m])"), ("line 4", "sum(y)")]
    dash = write(tmp_path, "d.json", json.dumps({"title": "D", "panels": [
        {"title": "P", "targets": [{"expr": "a"}, {"expr": "b"}]}]}))
    assert [q.name for q in load_queries(dash)] == ["P", "P #2"]


def test_bad_rule_file_is_an_input_error(tmp_path, capsys):
    bad = write(tmp_path, "bad.yaml", "just: a mapping\n")
    code, _, err = run_cli([BEFORE, AFTER, "--queries", bad], capsys)
    assert code == 2 and "groups" in err


# --- recorded benchmark ------------------------------------------------------------------

def test_recorded_benchmark_reproduces():
    result = subprocess.run([sys.executable, "benchmarks/preflight/run.py"], cwd=ROOT,
                            capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == json.loads((ROOT / "benchmarks/preflight/results.json").read_text())
