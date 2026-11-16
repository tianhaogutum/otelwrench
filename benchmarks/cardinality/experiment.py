# SPDX-License-Identifier: Apache-2.0
"""Deterministic synthetic scrape, independently checked CLI experiment.

No promcard_trim imports: expected identities are computed from generator records.
No baseline/plan runtime speedup is claimed; output size and work differ.
"""

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[2]


def generate(routes=100, pods=30):
    if routes < 1 or pods < 2:
        raise ValueError("routes must be >= 1 and pods >= 2 to demonstrate collisions")
    records = []
    lines = ["# HELP requests_total Deterministic synthetic requests.", "# TYPE requests_total counter"]
    for route in range(routes):
        for method in ("GET", "POST"):
            for pod in range(pods):
                labels = {"route": f"/route/{route}", "method": method, "pod": f"pod-{pod}", "debug_id": f"r{route}-{method}-p{pod}"}
                # Half of route/method groups have equal values; half differ by pod.
                value = 10 if route % 2 == 0 else 10 + pod
                records.append(("requests_total", labels, str(value)))
                rendered = ",".join(f'{key}="{val}"' for key, val in labels.items())
                lines.append(f"requests_total{{{rendered}}} {value}")
    return "\n".join(lines) + "\n", records


def independent_expected(records, dropped):
    groups = defaultdict(list)
    for metric, labels, value in records:
        # Structurally independent of production canonical_key/analyze_plan.
        identity = (metric, frozenset((key, val) for key, val in labels.items() if key not in dropped))
        groups[identity].append((labels, value))
    collisions = {key: group for key, group in groups.items() if len(group) > 1}
    relations = Counter("equal" if len({val for _, val in group}) == 1 else "different" for group in collisions.values())
    return groups, collisions, dict(relations)


def run_experiment(routes=100, pods=30, repeats=3):
    if repeats < 1:
        raise ValueError("repeats must be >= 1")
    text, records = generate(routes, pods)
    plans = [("baseline_keep_all", []), ("safe_drop_debug_id", ["debug_id"]), ("unsafe_drop_pod_and_debug_id", ["pod", "debug_id"])]
    results = []
    with tempfile.TemporaryDirectory(prefix="promcard-trim-experiment-") as directory:
        fixture = Path(directory) / "generated.prom"
        fixture.write_bytes(text.encode("utf-8"))
        for name, dropped in plans:
            groups, expected_collisions, relations = independent_expected(records, set(dropped))
            durations = []
            command = [sys.executable, "-m", "otelwrench", "cardinality", str(fixture), "--format", "json"]
            if dropped:
                command += ["--drop-labels", ",".join(dropped)]
            for _ in range(repeats):
                start = time.perf_counter()
                process = subprocess.run(command, capture_output=True, timeout=120, cwd=ROOT)
                durations.append(time.perf_counter() - start)
                expected_code = 1 if expected_collisions else 0
                if process.returncode != expected_code:
                    raise AssertionError(f"{name}: expected exit {expected_code}, got {process.returncode}: {process.stderr.decode()}")
                actual = json.loads(process.stdout)
                if actual["summary"]["observed_series"] != len(records):
                    raise AssertionError("observed series do not match generator records")
                stats = actual["summary"]["families"][0]["labels"]
                for label in ("route", "method", "pod", "debug_id"):
                    expected_values = sorted({labels[label] for _, labels, _ in records})
                    if stats[label]["values"] != expected_values or stats[label]["distinct_value_count"] != len(expected_values):
                        raise AssertionError(f"wrong distinct values for {label}")
                if dropped:
                    plan = actual["plans"][0]
                    if plan["projected_keys_after"] != len(groups) or plan["collision_group_count"] != len(expected_collisions):
                        raise AssertionError("wrong projected or collision counts")
                    if plan["unsafe"] != bool(expected_collisions):
                        raise AssertionError("wrong safety status")
                    observed_relations = dict(Counter(c["sample_value_relation"] for c in plan["collisions"]))
                    if observed_relations != relations:
                        raise AssertionError("wrong equal/different collision counts")
                    # Verify every projected collision and every original value, not just totals.
                    emitted = {}
                    for collision in plan["collisions"]:
                        key = collision["projected_key"]
                        identity = (key["metric"], frozenset(key["labels"].items()))
                        emitted[identity] = {(frozenset(w["original_labels"].items()), w["value"]) for w in collision["originals"]}
                    expected = {key: {(frozenset(labels.items()), val) for labels, val in values}
                                for key, values in expected_collisions.items()}
                    if emitted != expected:
                        raise AssertionError("collision witness mismatch")
            results.append({
                "name": name, "drop_labels": dropped,
                "baseline_observed_keys": len(records), "projected_keys": len(groups),
                "unsafe": bool(expected_collisions), "collision_groups": len(expected_collisions),
                "colliding_original_series": sum(len(group) for group in expected_collisions.values()),
                "collision_value_relations": relations,
                "wall_seconds_including_process_and_json_output": durations,
                "median_wall_seconds": statistics.median(durations),
            })
    return {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(), "platform": platform.platform(),
        "generator": {"routes": routes, "pods": pods, "methods": 2, "repeats": repeats, "seed": "none; fixed nested loops"},
        "input_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "input_bytes": len(text.encode("utf-8")), "results": results,
        "verification": "Independent generator-record identities, all label cardinalities, every collision and every original witness checked on each run.",
        "interpretation": "Baseline retains all labels. Safe deletion preserves keys. Lower projected unique-key count is unsafe merging, not valid ingestion or a speed/cost/memory gain. Timings are measured wall time, not speedup comparisons.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--routes", type=int, default=100)
    parser.add_argument("--pods", type=int, default=30)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--write-fixture", type=Path, help="optional new synthetic file (refuses overwrite)")
    args = parser.parse_args()
    try:
        result = run_experiment(args.routes, args.pods, args.repeats)
        if args.write_fixture:
            text, _ = generate(args.routes, args.pods)
            with args.write_fixture.open("x", encoding="utf-8", newline="\n") as output:
                output.write(text)
        print(json.dumps(result, indent=2, sort_keys=True))
    except (ValueError, OSError, AssertionError, subprocess.TimeoutExpired) as exc:
        parser.exit(2, f"experiment: {exc}\n")


if __name__ == "__main__":
    main()
