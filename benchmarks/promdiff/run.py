"""Reproducible contract-detection experiment; all inputs are synthetic.

Baseline algorithm: accept a candidate if prometheus_client can parse both
snapshots. This is deliberately a syntax-only baseline, NOT promtool itself.
The expected detection matrix and counts below are an independent oracle.
"""
from __future__ import annotations

import argparse
from importlib.metadata import version
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from prometheus_client.parser import text_string_to_metric_families

ROOT = Path(__file__).resolve().parents[2]


def generate(mode: str) -> str:
    blocks = []
    for family in range(20):
        if mode == "removed_family" and family == 0:
            continue
        name = f"synthetic_{family:02d}"
        help_text = "Changed documentation." if mode == "help_change" and family == 0 else "Synthetic gauge."
        typ = "counter" if mode == "type_change" and family == 0 else "gauge"
        lines = [f"# HELP {name} {help_text}", f"# TYPE {name} {typ}"]
        count = 30 if mode == "growth" and family == 0 else (4 if mode == "missing_series" and family == 0 else 5)
        for identity in range(count):
            label_value = "replacement" if mode == "label_value_churn" and family == 0 and identity == 0 else str(identity)
            extra_label = ',region="eu"' if mode == "label_keys" and family == 0 else ""
            value = 9000 + identity if mode == "value_only" else identity
            lines.append(f'{name}{{id="{label_value}"{extra_label}}} {value}')
        blocks.append("\n".join(lines))
    if mode == "added_family":
        blocks.append("# TYPE extra gauge\nextra 1")
    return "\n".join(blocks) + "\n"


# case, expected exit, exact change classes, candidate series, new, missing
MATRIX = [
    ("value_only", 0, [], 100, 0, 0),
    ("removed_family", 1, ["family_removed"], 95, 0, 5),
    ("type_change", 1, ["type_changed"], 100, 0, 0),
    ("label_keys", 1, ["label_schema_changed", "series_added", "series_missing"], 100, 5, 5),
    ("growth", 1, ["series_added", "series_growth"], 125, 25, 0),
    ("missing_series", 0, ["series_missing"], 99, 0, 1),
    ("label_value_churn", 0, ["series_added", "series_missing"], 100, 1, 1),
    ("help_change", 0, ["help_changed"], 100, 0, 0),
    ("added_family", 0, ["family_added"], 101, 1, 0),
]


def run(directory: Path) -> dict:
    baseline_text = generate("baseline")
    baseline_path = directory / "baseline.prom"
    baseline_path.write_text(baseline_text, encoding="utf-8")
    parsed_baseline = list(text_string_to_metric_families(baseline_text))
    if sum(len(f.samples) for f in parsed_baseline) != 100:
        raise AssertionError("syntax baseline did not parse all 100 samples")
    cases = []
    for name, expected_exit, kinds, count, new, missing in MATRIX:
        text = generate(name)
        candidate_path = directory / f"{name}.prom"
        candidate_path.write_text(text, encoding="utf-8")
        parsed = list(text_string_to_metric_families(text))
        parsed_count = sum(len(f.samples) for f in parsed)
        if parsed_count != count:
            raise AssertionError(f"{name}: syntax parser sample count {parsed_count} != oracle {count}")
        result = subprocess.run([sys.executable, "-m", "otelwrench", "promdiff", str(baseline_path),
                                 str(candidate_path), "--format", "json"],
                                cwd=ROOT, text=True, capture_output=True, timeout=60)
        if result.returncode != expected_exit or result.stderr:
            raise AssertionError(f"{name}: CLI exit {result.returncode} != oracle {expected_exit}: {result.stderr}")
        report = json.loads(result.stdout)
        detected = sorted(event["kind"] for event in report["changes"])
        if detected != sorted(kinds):
            raise AssertionError(f"{name}: changes {detected} != oracle {kinds}")
        observed = report["observed_series"]
        if (observed["candidate"], observed["new"], observed["missing"]) != (count, new, missing):
            raise AssertionError(f"{name}: independently specified series counts do not match")
        (directory / f"{name}.report.json").write_text(result.stdout, encoding="utf-8")
        cases.append({
            "case": name, "expected_violation": expected_exit == 1,
            "single_snapshot_parse_accepted": True, "guard_exit": result.returncode,
            "guard_status": report["status"], "change_classes": detected,
            "candidate_series": count, "new_series": new, "missing_series": missing,
        })
    violations = [c for c in cases if c["expected_violation"]]
    compatible = [c for c in cases if not c["expected_violation"]]
    return {
        "experiment_version": 1,
        "prometheus_client_version": version("prometheus-client"),
        "synthetic_input": {"baseline_families": 20, "series_per_family": 5, "baseline_series": 100},
        "policy": "default, 20% maximum positive net series growth",
        "baseline_algorithm": "accept if both snapshots parse with prometheus_client (not promtool)",
        "cases": cases,
        "summary": {
            "cases": len(cases), "known_violations": len(violations), "compatible_cases": len(compatible),
            "single_snapshot_detected_violations": sum(not c["single_snapshot_parse_accepted"] for c in violations),
            "guard_detected_violations": sum(c["guard_exit"] == 1 for c in violations),
            "guard_false_violations": sum(c["guard_exit"] != 0 for c in compatible),
            "all_oracle_checks_passed": True,
        },
        "limitations": "Synthetic detection experiment only; no runtime speedup, production impact, or promtool equivalence claimed.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path,
                        help="retain generated snapshots and per-case reports in a NEW directory")
    args = parser.parse_args()
    if args.output_dir:
        args.output_dir.mkdir(parents=True, exist_ok=False)
        result = run(args.output_dir)
    else:
        with tempfile.TemporaryDirectory(prefix="promdiff-experiment-") as directory:
            result = run(Path(directory))
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
