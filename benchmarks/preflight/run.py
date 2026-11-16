"""Reproducible check of `otelwrench preflight` on the bundled exporter-upgrade example.

All inputs are synthetic and live in examples/preflight/. The oracle is the
hand-written examples/preflight/expected.json, not otelwrench output. Three views
of the same upgrade are recorded:

1. syntax only: both captures parse with prometheus_client (what a scrape checks);
2. `otelwrench promdiff` alone: the raw schema gate, which sees removed and added
   families but not that most of them are renames;
3. `otelwrench preflight` with the dashboard and rule file: findings, and which of
   the 14 queries it flags, scored against the oracle.

It then checks the follow-up workflow the report prints: with the emitted rename
map and allowlist, `otelwrench promdiff` passes.

    python3 benchmarks/preflight/run.py            # prints the result
    python3 benchmarks/preflight/run.py --write    # also rewrites results.json
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from prometheus_client.parser import text_string_to_metric_families

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "preflight"
QUERIES = ["checkout-dashboard.json", "checkout-alerts.yaml"]


def otelwrench(*args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(ROOT / "src"), os.environ.get("PYTHONPATH")]))}
    env.pop("NO_COLOR", None)
    return subprocess.run([sys.executable, "-m", "otelwrench", *args], cwd=EXAMPLE, capture_output=True,
                          text=True, env=env, timeout=120)


def syntax_only() -> dict:
    families = {}
    for name in ("before.prom", "after.prom"):
        families[name] = sum(1 for _ in text_string_to_metric_families((EXAMPLE / name).read_text("utf-8")))
    return {"accepted": True, "families_parsed": families, "queries_flagged": 0}


def promdiff_alone() -> dict:
    run = otelwrench("promdiff", "before.prom", "after.prom", "--format", "json")
    report = json.loads(run.stdout)
    changes = [c for c in report["changes"] if not c["allowed"]]
    kinds: dict[str, int] = {}
    for change in changes:
        kinds[change["kind"]] = kinds.get(change["kind"], 0) + 1
    return {"exit": run.returncode, "status": report["status"], "failures": report["summary"]["failures"],
            "warnings": report["summary"]["warnings"], "changes_by_kind": dict(sorted(kinds.items())),
            "families_reported_removed": sorted(c["family"] for c in changes if c["kind"] == "family_removed")}


def preflight(oracle: dict) -> dict:
    flags = [arg for name in QUERIES for arg in ("--queries", name)]
    run = otelwrench("preflight", "before.prom", "after.prom", *flags, "--format", "json")
    doc = json.loads(run.stdout)
    flagged = {q["name"] for q in doc["queries"] if q["hit"]}
    broken = {name for name, label in oracle["queries"].items() if label["broken"]}
    kinds: dict[str, int] = {}
    for finding in doc["findings"]:
        kinds[finding["kind"]] = kinds.get(finding["kind"], 0) + 1
    renamed = {f["family"]: f["renamed_to"] for f in doc["findings"] if f["kind"] == "renamed"}
    return {
        "exit": run.returncode, "errors": doc["summary"]["errors"], "warnings": doc["summary"]["warnings"],
        "findings_by_kind": dict(sorted(kinds.items())),
        "renames_found": dict(sorted(renamed.items())),
        "renames_expected": dict(sorted(oracle["changes"]["renamed"].items())),
        "rescaled_found": sorted(f["family"] for f in doc["findings"] if f["kind"] == "renamed" and f["rescaled"]),
        "label_renames_found": doc["rename_map"]["labels"],
        "series": [doc["summary"]["series_before"], doc["summary"]["series_after"]],
        "growth_drivers": [{k: d[k] for k in ("family", "label", "values_before", "values_after", "drop_check")}
                           for f in doc["findings"] if f["kind"] == "series_growth" for d in f["drivers"]],
        "queries_total": len(doc["queries"]),
        "queries_broken_per_oracle": len(broken),
        "queries_flagged": len(flagged),
        "true_positives": len(flagged & broken),
        "false_positives": sorted(flagged - broken),
        "missed": sorted(broken - flagged),
        "queries_by_kind": doc["summary"]["queries_by_kind"],
    }


def follow_up() -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        renames, allow = Path(tmp) / "renames.json", Path(tmp) / "allowlist.json"
        renames.write_text(otelwrench("preflight", "before.prom", "after.prom", "--emit", "rename-map").stdout)
        allow.write_text(otelwrench("preflight", "before.prom", "after.prom", "--emit", "allowlist").stdout)
        declared = otelwrench("promdiff", "before.prom", "after.prom", "--rename-map", str(renames), "--format", "json")
        accepted = otelwrench("promdiff", "before.prom", "after.prom", "--rename-map", str(renames),
                              "--allowlist", str(allow), "--format", "json")
    declared_report = json.loads(declared.stdout)
    return {"promdiff_with_rename_map": {"exit": declared.returncode,
                                         "failures": declared_report["summary"]["failures"],
                                         "warnings": declared_report["summary"]["warnings"]},
            "promdiff_with_rename_map_and_allowlist": {"exit": accepted.returncode,
                                                       "status": json.loads(accepted.stdout)["status"]}}


def run() -> dict:
    oracle = json.loads((EXAMPLE / "expected.json").read_text("utf-8"))
    return {"inputs": "examples/preflight (synthetic, fictional service)", "oracle": "examples/preflight/expected.json",
            "syntax_only": syntax_only(), "promdiff_alone": promdiff_alone(), "preflight": preflight(oracle),
            "follow_up": follow_up()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true", help="rewrite benchmarks/preflight/results.json")
    args = parser.parse_args()
    result = run()
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.write:
        (Path(__file__).parent / "results.json").write_text(text, encoding="utf-8")
    sys.stdout.write(text)


if __name__ == "__main__":
    main()
