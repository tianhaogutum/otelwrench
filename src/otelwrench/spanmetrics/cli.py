# SPDX-License-Identifier: Apache-2.0
"""Bounded local-file and explicit JSONL stdin command line interface."""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import BinaryIO
import tempfile

from .core import Config, Dimension, InputError, convert, diagnostic, empty_report, parse_buckets, parse_dimensions
from .comparison import compare, comparison_report
from .dimension_config import MAX_CONFIG_BYTES, parse_dimension_config


def positive(value: str) -> int:
    try:
        number = int(value)
        if not 1 <= number <= 2**63 - 1:
            raise ValueError
        return number
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected positive integer <= 2**63-1") from exc


def dimension_sha256(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise argparse.ArgumentTypeError("expected exactly 64 lowercase hexadecimal digits")
    return value


def nonnegative(value: str) -> int:
    try:
        if re.fullmatch(r"[0-9]+", value) is None:
            raise ValueError
        number = int(value)
        if number > 2**63 - 1:
            raise ValueError
        return number
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected nonnegative integer <= 2**63-1") from exc


def parser(comparison: bool = False) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="otelwrench spanmetrics compare" if comparison else "otelwrench spanmetrics",
        description="Compare two explicit dimension configurations on one capture." if comparison else
                    "Convert bounded OTLP JSON captures to offline Prometheus RED fixtures. "
                    "Use 'compare --help' for dimension comparison or 'validate-dimensions --help' for capture-free pair inspection.")
    p.add_argument("input", nargs="?", help="UTF-8 OTLP request capture; '-' for explicit JSONL stdin")
    p.add_argument("--input", dest="input_option", metavar="SOURCE",
                   help="alternative to positional input; '-' requires --input-format jsonl")
    p.add_argument("--input-format", choices=("json", "jsonl"), default="json",
                   help="one JSON request (default), or one request per JSONL line; never inferred")
    p.add_argument("--output", required=True, type=Path,
                   help="comparison JSON destination" if comparison else "Prometheus text destination")
    if not comparison:
        p.add_argument("--report", type=Path, help="JSON manifest (default: OUTPUT.report.json)")
    p.add_argument("--buckets", default="0.1,0.5,1", help="strictly increasing finite seconds, comma separated")
    for option in ("--baseline-dimension", "--candidate-dimension") if comparison else ("--dimension",):
        p.add_argument(option, action="append", default=[], metavar="SOURCE:KEY=LABEL",
                       help="repeatable allowlist entry; SOURCE is resource or span; omitted means built-in labels only")
    p.add_argument("--dimensions-file", type=Path, metavar="FILE",
                   help="strict JSON baseline/candidate pair; cannot combine with dimension flags")
    if not comparison:
        p.add_argument("--dimensions-side", choices=("baseline", "candidate"),
                       help="required with --dimensions-file; explicitly select one side for conversion")
    p.add_argument("--dimension-provenance", action="store_true",
                   help=("include exact pair-byte SHA-256 for both configurations in the report; requires --dimensions-file"
                         if comparison else
                         "include selected side and exact pair-byte SHA-256 in the manifest; requires --dimensions-file"))
    p.add_argument("--expect-dimension-sha256", type=dimension_sha256, metavar="HEX",
                   help="require the exact complete-pair SHA-256 (64 lowercase hex digits); requires --dimensions-file")
    p.add_argument("--expect-input-sha256", type=dimension_sha256, metavar="HEX",
                   help="require the exact fully loaded capture SHA-256 (64 lowercase hex digits)")
    p.add_argument("--max-series", type=positive, default=10_000,
                   help="per-configuration prospective series cap (default 10000)" if comparison else
                        "actual emitted time-series cap (default 10000)")
    if comparison:
        p.add_argument("--summary-only", action="store_true",
                       help="omit observed-value partitions and diagnostic message text; retain counts and definitions; exact spelling required")
        p.add_argument("--max-series-increase", type=nonnegative, metavar="N",
                       help="optional capture-local candidate-minus-baseline series budget (0..2**63-1); exact spelling required")
    p.add_argument("--max-spans", type=positive, default=100_000, help="input record cap, including duplicates")
    p.add_argument("--max-input-bytes", type=positive, default=64 * 1024 * 1024)
    p.add_argument("--max-line-bytes", type=positive, default=1024 * 1024,
                   help="JSONL UTF-8 bytes per line, excluding LF/CRLF (default 1048576); ignored for JSON")
    p.add_argument("--force", action="store_true", help="replace existing regular output files")
    p.add_argument("--dry-run", action="store_true", help="validate/aggregate; print manifest; write no files")
    return p


def parse_capture_args(p: argparse.ArgumentParser, argv: list[str]) -> argparse.Namespace:
    # Require exact gate/summary spellings while preserving legacy option abbreviations.
    argv = list(argv)
    for index, token in enumerate(argv):
        if token == "--":
            break
        option = token.partition("=")[0]
        if option.startswith("--") and any(
                option != gate and gate.startswith(option)
                for gate in ("--expect-dimension-sha256", "--expect-input-sha256", "--summary-only")):
            p.error(f"unrecognized arguments: {token}")
        budget = "--max-series-increase"
        if (budget in p._option_string_actions and option.startswith("--") and
                option != budget and budget.startswith(option)):
            legacy = [name for name in p._option_string_actions if name != budget and name.startswith(option)]
            if not legacy:
                p.error(f"unrecognized arguments: {token}")
            if len(legacy) == 1:
                # The new option must not make a formerly unique --max-series prefix ambiguous.
                argv[index] = legacy[0] + token[len(option):]
    return p.parse_args(argv)


def same_file(a: Path, b: Path) -> bool:
    return a.resolve() == b.resolve() or (a.exists() and b.exists() and a.samefile(b))


def input_source(args: argparse.Namespace, p: argparse.ArgumentParser) -> Path | None:
    if (args.input is None) == (args.input_option is None):
        p.error("provide exactly one positional input or --input SOURCE")
    raw_source = args.input if args.input is not None else args.input_option
    args.input = Path(raw_source)
    if raw_source == "-":
        if args.input_format != "jsonl":
            p.error("stdin requires --input-format jsonl")
        return None
    return args.input


def read_bounded(stream: BinaryIO, limit: int) -> bytes:
    """Read through EOF or one byte past the cap; tolerate short binary reads."""
    data = bytearray()
    while len(data) <= limit:
        chunk = stream.read(min(65536, limit + 1 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def read_input(source: Path | None, limit: int) -> bytes:
    if source is not None and not source.is_file():
        raise InputError(f"input must be an existing regular file: {source}")
    # The caller owns stdin; closing the context must not close it.
    context = source.open("rb") if source is not None else nullcontext(sys.stdin.buffer)
    with context as stream:
        return read_bounded(stream, limit)


def read_dimension_pair(path: Path) -> tuple[tuple[tuple[Dimension, ...], tuple[Dimension, ...]], str]:
    """Validate the complete pair and hash the same bounded bytes, without reopening."""
    if not path.is_file():
        raise InputError(f"dimension configuration must be an existing regular file: {path}")
    with path.open("rb") as stream:
        data = read_bounded(stream, MAX_CONFIG_BYTES)
    return parse_dimension_config(data), hashlib.sha256(data).hexdigest()


def stdin_alias(target: Path) -> bool:
    try:
        source_stat = os.fstat(sys.stdin.buffer.fileno())
    except (AttributeError, OSError, ValueError):
        # In-memory stdin (e.g. an embedding) has no filesystem identity.
        return False
    return target.exists() and os.path.samestat(source_stat, target.stat())


def check_paths(source: Path | None, targets: list[Path], force: bool) -> None:
    if len(targets) == 2 and same_file(targets[0], targets[1]):
        raise InputError("output and report must be distinct paths")
    for target in targets:
        if ((source is not None and same_file(source, target)) or
                (source is None and stdin_alias(target))):
            raise InputError("output/report must not overwrite input")
        if target.is_symlink():
            raise InputError(f"refusing symlink destination: {target}")
        if target.exists() and (not target.is_file() or not force):
            raise InputError(f"destination exists (use --force for regular files): {target}")
        if not target.parent.is_dir():
            raise InputError(f"destination directory does not exist: {target.parent}")


def write_files(files: list[tuple[Path, str]], force: bool) -> None:
    # Stage everything before publishing. Each file is atomic; the pair is not a filesystem transaction.
    staged = []
    try:
        for target, content in files:
            fd, temp = tempfile.mkstemp(prefix=".spanmetrics-", dir=target.parent)
            staged.append((Path(temp), target))
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        for temp, target in staged:
            if force:
                os.replace(temp, target)
            else:
                os.link(temp, target)  # fails if a competing writer created the destination
                temp.unlink()
    finally:
        for temp, _ in staged:
            temp.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "compare":
        return comparison_main(argv[1:])
    if argv and argv[0] == "validate-dimensions":
        return validate_dimensions_main(argv[1:])
    p = parser()
    args = parse_capture_args(p, argv)
    source = input_source(args, p)
    try:
        if args.expect_dimension_sha256 is not None and args.dimensions_file is None:
            raise InputError("--expect-dimension-sha256 requires --dimensions-file and --dimensions-side baseline|candidate")
        if args.dimension_provenance and args.dimensions_file is None:
            raise InputError("--dimension-provenance requires --dimensions-file and --dimensions-side baseline|candidate")
        if args.dimensions_file is not None:
            if args.dimensions_side is None:
                raise InputError("--dimensions-file requires --dimensions-side baseline|candidate")
            if args.dimension:
                raise InputError("cannot combine --dimensions-file with --dimension")
            pair, pair_sha256 = read_dimension_pair(args.dimensions_file)
            if args.expect_dimension_sha256 is not None and args.expect_dimension_sha256 != pair_sha256:
                raise InputError("dimension configuration SHA-256 mismatch: "
                                 f"expected {args.expect_dimension_sha256}, got {pair_sha256}")
            dimensions = pair[0 if args.dimensions_side == "baseline" else 1]
        else:
            if args.dimensions_side is not None:
                raise InputError("--dimensions-side requires --dimensions-file")
            dimensions = parse_dimensions(args.dimension)
        config = Config(buckets=parse_buckets(args.buckets), dimensions=dimensions,
                        max_series=args.max_series, max_spans=args.max_spans, max_input_bytes=args.max_input_bytes,
                        max_line_bytes=args.max_line_bytes, input_format=args.input_format)
        report_path = args.report or Path(str(args.output) + ".report.json")
        if not args.dry_run:
            if args.dimensions_file is not None:
                check_paths(args.dimensions_file, [args.output, report_path], args.force)
            check_paths(source, [args.output, report_path], args.force)
        data = read_input(source, config.max_input_bytes)
        if len(data) > config.max_input_bytes:
            report = empty_report(config)
            report["record_counts_complete"] = False
            diagnostic(report, f"input exceeds byte cap {config.max_input_bytes}; input was not fully read")
            prom = None
        else:
            if args.expect_input_sha256 is not None:
                input_sha256 = hashlib.sha256(data).hexdigest()
                if args.expect_input_sha256 != input_sha256:
                    raise InputError("input SHA-256 mismatch: "
                                     f"expected {args.expect_input_sha256}, got {input_sha256}")
            result = convert(data, config)
            report, prom = result.report, result.prometheus
        if args.dimension_provenance:
            report["dimension_pair_provenance"] = {
                "schema_version": 1, "selected_side": args.dimensions_side, "sha256": pair_sha256}
        report["dry_run"] = args.dry_run
        report["input"] = str(args.input)
        report["metrics_emitted"] = prom is not None and not args.dry_run
        report["output"] = str(args.output)
        report["report"] = str(report_path)
        report_text = json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
        if not args.dry_run:
            files = [(report_path, report_text)]
            if prom is not None:
                # Publish metrics last; matching hash in the manifest identifies the pair.
                files.append((args.output, prom))
            write_files(files, args.force)
        sys.stdout.write(report_text)
        if prom is None:
            print("otelwrench spanmetrics: conversion rejected; no metrics written; existing metrics unchanged. "
                  "See JSON diagnostics.", file=sys.stderr)
        return 0 if prom is not None else 1
    except (InputError, OSError, ValueError, OverflowError) as exc:
        print(f"otelwrench spanmetrics: {exc}", file=sys.stderr)
        return 2


def validate_dimensions_main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="otelwrench spanmetrics validate-dimensions",
        allow_abbrev=False,
        description="Validate and inspect both reviewed dimension selections without reading a capture or writing files.")
    p.add_argument("--dimensions-file", required=True, type=Path, metavar="FILE",
                   help="strict bounded JSON baseline/candidate pair")
    p.add_argument("--dimension-provenance", action="store_true",
                   help="include exact pair-byte SHA-256 for both selections; requires --dimensions-file")
    p.add_argument("--expect-dimension-sha256", type=dimension_sha256, metavar="HEX",
                   help="require the exact complete-pair SHA-256 (64 lowercase hex digits); requires --dimensions-file")
    args = p.parse_args(argv)
    try:
        pair, pair_sha256 = read_dimension_pair(args.dimensions_file)
        if args.expect_dimension_sha256 is not None and args.expect_dimension_sha256 != pair_sha256:
            raise InputError("dimension configuration SHA-256 mismatch: "
                             f"expected {args.expect_dimension_sha256}, got {pair_sha256}")
        report = {"schema_version": 1, "report_type": "dimension_configuration", "status": "ok",
                  "configurations": {side: {"dimensions": [vars(d) for d in selection],
                                            "custom_dimension_count": len(selection)}
                                     for side, selection in zip(("baseline", "candidate"), pair)}}
        if args.dimension_provenance:
            report["dimension_pair_provenance"] = {"schema_version": 1, "sha256": pair_sha256}
        sys.stdout.write(json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True) + "\n")
        return 0
    except (InputError, OSError, ValueError, OverflowError) as exc:
        print(f"otelwrench spanmetrics: {exc}", file=sys.stderr)
        return 2


def comparison_main(argv: list[str]) -> int:
    p = parser(comparison=True)
    args = parse_capture_args(p, argv)
    source = input_source(args, p)
    try:
        if args.expect_dimension_sha256 is not None and args.dimensions_file is None:
            raise InputError("--expect-dimension-sha256 requires --dimensions-file")
        if args.dimension_provenance and args.dimensions_file is None:
            raise InputError("--dimension-provenance requires --dimensions-file")
        if args.dimensions_file is not None:
            if args.baseline_dimension or args.candidate_dimension:
                raise InputError("cannot combine --dimensions-file with --baseline-dimension or --candidate-dimension")
            dimensions, pair_sha256 = read_dimension_pair(args.dimensions_file)
            if args.expect_dimension_sha256 is not None and args.expect_dimension_sha256 != pair_sha256:
                raise InputError("dimension configuration SHA-256 mismatch: "
                                 f"expected {args.expect_dimension_sha256}, got {pair_sha256}")
        else:
            dimensions = tuple(parse_dimensions(specs)
                               for specs in (args.baseline_dimension, args.candidate_dimension))
        configs = [Config(buckets=parse_buckets(args.buckets), dimensions=selection,
                          max_series=args.max_series, max_spans=args.max_spans, max_input_bytes=args.max_input_bytes,
                          max_line_bytes=args.max_line_bytes, input_format=args.input_format)
                   for selection in dimensions]
        if not args.dry_run:
            if args.dimensions_file is not None:
                check_paths(args.dimensions_file, [args.output], args.force)
            check_paths(source, [args.output], args.force)
        data = read_input(source, args.max_input_bytes)
        if len(data) > args.max_input_bytes:
            reports = [empty_report(config) for config in configs]
            for report in reports:
                report["record_counts_complete"] = False
                diagnostic(report, f"input exceeds byte cap {args.max_input_bytes}; input was not fully read")
            report = comparison_report(*configs, reports)
        else:
            if args.expect_input_sha256 is not None:
                input_sha256 = hashlib.sha256(data).hexdigest()
                if args.expect_input_sha256 != input_sha256:
                    raise InputError("input SHA-256 mismatch: "
                                     f"expected {args.expect_input_sha256}, got {input_sha256}")
            report = compare(data, *configs)
        if args.dimension_provenance:
            report["dimension_pair_provenance"] = {"schema_version": 1, "sha256": pair_sha256}
        budget_exceeded = False
        if args.max_series_increase is not None:
            budget_status = "unavailable"
            if report["comparison_complete"]:
                budget_exceeded = report["series_delta"] > args.max_series_increase
                budget_status = "exceeded" if budget_exceeded else "within_budget"
            report["series_increase_budget"] = {
                "max_series_increase": args.max_series_increase, "status": budget_status}
        if args.summary_only:
            # Transform only after the same complete validation and budget decision.
            # Diagnostic text can quote capture-controlled keys/bytes, so withhold it too.
            del report["partitions"]
            report["summary_only"] = True
            report["attribution"] = {"status": "omitted", "reason": "summary_only"}
            report["summary_omissions"] = ["partitions", "validation_diagnostic_messages"]
            for config in report["configurations"].values():
                validation = config["validation"]
                validation["diagnostics_omitted"] += len(validation["diagnostics"])
                validation["diagnostics"] = []
        report.update(input=str(args.input), output=str(args.output), dry_run=args.dry_run)
        report_text = json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
        if not args.dry_run:
            write_files([(args.output, report_text)], args.force)
        sys.stdout.write(report_text)
        if not report["comparison_complete"]:
            print("otelwrench spanmetrics: comparison rejected; counts unavailable. See JSON validation diagnostics.",
                  file=sys.stderr)
        elif budget_exceeded:
            detail = "counts" if args.summary_only else "counts and attribution"
            print(f"otelwrench spanmetrics: series increase budget exceeded: series_delta {report['series_delta']} "
                  f"> {args.max_series_increase}. See JSON {detail}.", file=sys.stderr)
        return 0 if report["comparison_complete"] and not budget_exceeded else 1
    except (InputError, OSError, ValueError, OverflowError) as exc:
        print(f"otelwrench spanmetrics: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
