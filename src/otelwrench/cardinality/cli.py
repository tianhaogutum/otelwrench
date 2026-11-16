# SPDX-License-Identifier: Apache-2.0
"""Read-only command line interface. Reports and candidate YAML go to stdout."""

import argparse
import json
from pathlib import Path
import sys

from . import __version__
from .analysis import report, report_scrapes
from .candidates import load_candidates
from .parser import InputError, parse
from .scrapes import load_scrape_set


def _text(result):
    if "scrapes" in result:
        return _text_scrapes(result)
    lines = [f"Observed series: {result['summary']['observed_series']} (dry-run)"]
    for family in result["summary"]["families"]:
        lines.append(f"Family {family['name']} ({family['type']}): {family['observed_series']} series")
        for label, stats in family["labels"].items():
            lines.append(f"  {label}: {stats['distinct_value_count']} distinct values {json.dumps(stats['values'], ensure_ascii=True)}")
    for plan in result["plans"]:
        lines.append(f"{plan['name']} drop {','.join(plan['drop_labels'])}: observed keys {plan['observed_keys_before']} -> {plan['projected_keys_after']}; {plan['status']}; delta {plan['observed_cardinality_delta']}")
        lines.append(f"  Scope metrics: {', '.join(plan['scope_metric_names'])}")
        lines.append(f"  Affected metrics: {', '.join(plan['affected_metric_names'])}")
        for collision in plan["collisions"]:
            lines.append(f"  Collision ({collision['sample_value_relation']} values): {json.dumps(collision['projected_key'], sort_keys=True, ensure_ascii=True)}")
            for original in collision["originals"]:
                lines.append("    " + json.dumps(original, sort_keys=True, ensure_ascii=True))
        if "config_refusal" in plan:
            lines.append("  " + plan["config_refusal"])
        if "metric_relabel_configs_yaml" in plan:
            lines.append(plan["metric_relabel_configs_yaml"].rstrip())
    if "candidate_ranking" in result:
        lines.extend(_ranking_lines(result["candidate_ranking"], "Capture-local"))
    lines.extend("Warning: " + warning for warning in result["warnings"])
    return "\n".join(lines) + "\n"


def _ranking_lines(ranking, scope):
    limits = ranking["limits"]
    lines = [f"{scope} collision-free ranking (max {limits['max_candidates']} candidates, {limits['max_labels_per_candidate']} labels each):"]
    lines.append("  Objective: explicit label pairs removed, descending; ties: label names lexicographically. No series-count or cost saving is implied.")
    for entry in ranking["ranked_safe_plans"]:
        lines.append(f"  {entry['rank']}. {entry['name']} drop {','.join(entry['drop_labels'])}: {entry['label_pairs_removed']} label pairs removed")
    if not ranking["ranked_safe_plans"]:
        lines.append("  No collision-free candidates")
    lines.append(f"  Unsafe candidates excluded: {ranking['excluded_unsafe_candidates']}; all witnesses retained above")
    return lines


def _text_scrapes(result):
    summary = result["summary"]
    lines = [f"Observed union: {summary['observed_series']} identities, {summary['sample_observations']} separate sample observations in {summary['scrape_count']} scrapes (dry-run)"]
    if result["required_target_labels"]:
        lines.append("Required target labels (nonempty on every sample): " + ", ".join(result["required_target_labels"]))
    if result["protected_target_labels"]:
        lines.append("Protected target labels (nonempty; removal forbidden): " + ", ".join(result["protected_target_labels"]))
    for entry in result["scrapes"]:
        lines.append("Scrape " + json.dumps({k: entry[k] for k in ("id", "source", "captured_at", "target_labels") if k in entry}, ensure_ascii=True))
        lines.append(_text({"summary": entry["summary"], "plans": entry["plans"], "warnings": []}).rstrip())
    lines.append("Observed union checks (distinct identities; no values merged):")
    for plan in result["plans"]:
        lines.append(f"  {plan['name']} drop {','.join(plan['drop_labels'])}: observed keys {plan['observed_keys_before']} -> {plan['projected_keys_after']}; {plan['status']}; all scrapes safe: {plan['all_scrapes_safe']}")
        lines.append(f"    Scope metrics: {', '.join(plan['scope_metric_names'])}")
        for collision in plan["collisions"]:
            lines.append(f"  Collision {collision['classification']} ({collision['sample_value_relation']} values): " + json.dumps(collision["projected_key"], sort_keys=True, ensure_ascii=True))
            for witness in collision["originals"]:
                lines.append("    " + json.dumps(witness, sort_keys=True, ensure_ascii=True))
    if "candidate_ranking" in result:
        lines.extend(_ranking_lines(result["candidate_ranking"], "Observed-union"))
    lines.extend("Warning: " + warning for warning in result["warnings"])
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="otelwrench cardinality", description="Offline collision-aware what-if label removal; never changes input or production configuration.")
    parser.add_argument("input", nargs="?", help="UTF-8 Prometheus text 0.0.4 file, or - for stdin")
    parser.add_argument("--scrape-set", metavar="MANIFEST.json", help="ordered version-1 local manifest with id/source/captured_at/path and optional target_labels per scrape; checks each and the observed union")
    parser.add_argument("--require-target-label", action="append", default=[], metavar="LABEL", help="require a nonempty effective label on every sample in --scrape-set; repeat for distinct names; no provenance inference")
    parser.add_argument("--protect-target-label", action="append", default=[], metavar="LABEL", help="require nonempty coverage and forbid candidate removal of this label in --scrape-set; repeat for distinct names")
    parser.add_argument("--drop-labels", action="append", default=[], metavar="LABEL[,LABEL...]", help="one plan; repeat to compare independent plans against the unchanged baseline")
    parser.add_argument("--candidate-file", action="append", metavar="CANDIDATES.json", help="one local JSON file of named candidates: v1 shared scope or v2 per-candidate exact metrics (no --metric); at most 64 combinations, 8 labels each; cannot combine with --drop-labels")
    parser.add_argument("--rank-plans", action="store_true", help="rank only collision-free explicit plans by label pairs removed; at most 64 unique candidates, 8 labels each; no combinations generated or configuration emitted")
    parser.add_argument("--metric", action="append", help="exact sample metric name to scope all plans; repeat for more names (default: all observed names); incompatible with candidate-file v2")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--emit-config", action="store_true", help="include candidate YAML in report for one observed-safe plan; no file is written")
    parser.add_argument("--version", action="version", version=f"otelwrench cardinality {__version__}")
    args = parser.parse_args(argv)
    try:
        if (args.input is None) == (args.scrape_set is None):
            raise InputError("supply exactly one input file (or -) or --scrape-set manifest")
        if args.require_target_label and args.scrape_set is None:
            raise InputError("--require-target-label requires --scrape-set")
        if args.protect_target_label and args.scrape_set is None:
            raise InputError("--protect-target-label requires --scrape-set")
        plans = [[label.strip() for label in plan.split(",")] for plan in args.drop_labels]
        names = None
        scopes = None
        if args.candidate_file is not None:
            if len(args.candidate_file) != 1 or args.drop_labels:
                raise InputError("supply one --candidate-file without any --drop-labels")
            candidates = load_candidates(args.candidate_file[0])
            plans = [candidate.drop_labels for candidate in candidates]
            names = [candidate.name for candidate in candidates]
            if candidates[0].metrics is not None:
                if args.metric is not None:
                    raise InputError("--metric cannot be combined with --candidate-file v2")
                scopes = [candidate.metrics for candidate in candidates]
        if args.scrape_set is not None:
            result = report_scrapes(load_scrape_set(args.scrape_set), plans, args.metric, args.emit_config,
                                    rank_plans=args.rank_plans, plan_names=names, plan_metrics=scopes,
                                    required_target_labels=args.require_target_label,
                                    protected_target_labels=args.protect_target_label)
        else:
            if args.input == "-":
                # Decode bytes explicitly so UTF-8 behavior does not depend on locale.
                text = sys.stdin.buffer.read().decode("utf-8")
            else:
                text = Path(args.input).read_bytes().decode("utf-8")
            result = report(parse(text), plans, args.metric, args.emit_config,
                            rank_plans=args.rank_plans, plan_names=names, plan_metrics=scopes)
        sys.stdout.write(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n" if args.format == "json" else _text(result))
        return 1 if result["unsafe"] else 0
    except (InputError, OSError, UnicodeError) as exc:
        error = {"error": str(exc), "error_type": "invalid_or_unsupported_input"}
        if isinstance(exc, InputError) and exc.details is not None:
            error["details"] = exc.details
        sys.stderr.write(json.dumps(error, ensure_ascii=True) + "\n" if args.format == "json" else f"otelwrench cardinality: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
