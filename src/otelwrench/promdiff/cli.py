"""Local files only: no network, shell execution, or input modification."""
from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .compare import compare, validate_allowlist
from .config import Config, _decode_json
from ._files import MAX_INPUT_BYTES, read_text as _read_text
from .parser import InputError, parse_snapshot
from .renames import validate_rename_map

def read_text(path: str) -> str:
    return _read_text(path, max_bytes=MAX_INPUT_BYTES)


def _read_json_text(path: str) -> str:
    # Retain the existing JSON-file wrapper for I/O/UTF-8/size diagnostics.
    try:
        return read_text(path)
    except (ValueError, RecursionError) as exc:
        raise InputError(f"{path}: invalid JSON: {exc}") from exc


def read_json(path: str, *, growth_config: bool = False) -> object:
    return _decode_json(_read_json_text(path), source=path, growth_config=growth_config)


def text_report(report: dict) -> str:
    summary = report["summary"]
    series = report["observed_series"]
    lines = [f"{report['status'].upper()}: {summary['failures']} failure(s), "
             f"{summary['warnings']} warning(s), {summary['allowed']} allowed change(s)",
             f"Observed series: {series['baseline']} -> {series['candidate']}; "
             f"new={series['new']}, missing={series['missing']}"]
    if "max_growth_percent_exact" in series:
        lines.append(f"Exact growth budget: {series['max_growth_percent_exact']}%")
    declarations = report.get("declared_renames", {})
    for old, new in declarations.get("metrics", {}).items():
        lines.append(f"DECLARED metric {old} -> {new}")
    for family, mapping in declarations.get("labels", {}).items():
        for old, new in mapping.items():
            lines.append(f"DECLARED label {family}: {old} -> {new}")
    for change in report["changes"]:
        action = "ALLOW" if change["allowed"] else change["severity"].upper()
        lines.append(f"{action} {change['kind']} {change['family'] or '(snapshot)'}: "
                     f"{json.dumps(change['before'], ensure_ascii=False, sort_keys=True)} -> "
                     f"{json.dumps(change['after'], ensure_ascii=False, sort_keys=True)}")
    if "raw_changes" in report:
        lines.append("RAW CHANGES (wire, informational):")
        if not report["raw_changes"]:
            lines.append("  (none)")
        for change in report["raw_changes"]:
            lines.append(f"RAW {change['kind']} {change['family'] or '(snapshot)'}: "
                         f"{json.dumps(change['before'], ensure_ascii=False, sort_keys=True)} -> "
                         f"{json.dumps(change['after'], ensure_ascii=False, sort_keys=True)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="otelwrench promdiff", description="Compare two local Prometheus 0.0.4 snapshots.")
    parser.add_argument("baseline", help="baseline UTF-8 .prom file")
    parser.add_argument("candidate", help="candidate UTF-8 .prom file")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--config", help="strict JSON policy and growth limit")
    parser.add_argument("--config-overlay", action="append", default=[], metavar="PATH",
                        help="exact JSON policy/growth overlay; repeat in order after --config "
                             "or defaults (64 MiB per file)")
    parser.add_argument("--allowlist", help="JSON array of exact expected changes")
    parser.add_argument("--rename-map", help="strict JSON metric and family-scoped label rename declarations")
    parser.add_argument("--include-raw-changes", action="store_true",
                        help="include informational wire changes before rename projection; "
                             "does not affect policy, allowlists or exit status")
    parser.add_argument("--validate-composites", action="store_true",
                        help="validate classic histogram/summary counts, histogram bucket order "
                             "and summary quantile uniqueness before comparison")
    parser.add_argument("--version", action="version", version=f"otelwrench promdiff {__version__}")
    args = parser.parse_args(argv)
    try:
        config = Config.from_json(_read_json_text(args.config), source=args.config) if args.config else Config()
        for path in args.config_overlay:
            config = config.with_overrides_file(path, max_bytes=MAX_INPUT_BYTES)
        allowlist = validate_allowlist(read_json(args.allowlist)) if args.allowlist else []
        rename_map = validate_rename_map(read_json(args.rename_map)) if args.rename_map else None
        baseline = parse_snapshot(read_text(args.baseline), args.baseline,
                                  validate_composites=args.validate_composites)
        candidate = parse_snapshot(read_text(args.candidate), args.candidate,
                                   validate_composites=args.validate_composites)
        report = compare(baseline, candidate, config, allowlist, rename_map=rename_map,
                         include_raw_changes=args.include_raw_changes)
    except (InputError, RecursionError) as exc:
        if args.format == "json":
            print(json.dumps({"report_version": 1, "status": "error", "error": str(exc)},
                             ensure_ascii=False, sort_keys=True), file=sys.stderr)
        else:
            print(f"otelwrench promdiff: {exc}", file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))
    else:
        print(text_report(report))
    return 1 if report["summary"]["failures"] else 0
