# SPDX-License-Identifier: Apache-2.0
"""``otelwrench alert-flapsim``: offline captured Prometheus matrix alert replay.

The argument grammar is hand-parsed (not argparse) so that option errors keep
their exact diagnostics and exit status 1 with an empty stdout.
"""
from __future__ import annotations

import os
import stat as stat_module
import sys
from typing import Any

from ._js import stringify
from ._nodefs import node_error
from .api import (
    compare,
    compare_csv,
    compare_report_set,
    compare_report_set_csv,
    compare_report_set_summary_csv,
    compare_reports,
    compare_reports_csv,
    grid,
    grid_csv,
    join,
    replay,
)
from .jsonparse import parse_json
from .local_output import realpath, validate_output_path, write_local_output
from .report_set_manifest import report_set_manifest_paths, validate_manifest_path
from .report_set_names import name_report_set_summary, report_set_names, validate_names_path
from .report_set_summary_csv import format_report_set_summary_csv
from .validation import FlapsimError

PROG = "otelwrench alert-flapsim"

HELP = f"""{PROG} — offline captured Prometheus matrix replay

Usage:
  {PROG} replay MATRIX.json --config ALERT.json [--checkpoint-in STATE.json]
  {PROG} checkpoint MATRIX.json --config ALERT.json [--checkpoint-in STATE.json]
  {PROG} compare MATRIX.json --baseline BASE.json --candidate CANDIDATE.json [--format json|csv]
  {PROG} compare-reports BASELINE.json CANDIDATE.json [--format json|csv]
  {PROG} compare-report-set BASELINE.json CANDIDATE1.json [CANDIDATE2.json ...] [--format json|csv] [--detail full|summary] [--names NAMES.json] [--output PATH]
  {PROG} compare-report-set --manifest SET.json [--format json|csv] [--detail full|summary] [--names NAMES.json] [--output PATH]
  {PROG} grid MATRIX.json --baseline BASE.json --grid TIMERS.json [--format json|csv]
  {PROG} join REPORT1.json REPORT2.json [REPORT3.json ...]
  {PROG} --help

All inputs are local JSON files; deterministic JSON is written to stdout by default.
compare/compare-reports/compare-report-set/grid --format csv emits baseline/candidate aggregate and exact-label tradeoff rows.
checkpoint emits only the final checkpoint; replay includes it in the report.
Resume requires explicit start/end and a contiguous, identical grid, rule and labels.
grid compares the Cartesian product of for/keep_firing_for arrays to a baseline.
join verifies 2..256 ordered replay reports from their checkpoint origin,
recomputes whole-capture metrics and emits a replay report (at most 1000000 evaluations).
compare-reports verifies two complete origin replay reports with identical raw captures,
then compares their explicit settings (at most 1000000 combined series-evaluations).
compare-report-set verifies one baseline and 1..255 candidates against the same raw capture,
with the same combined evaluation limit; baseline once, input-ordered candidates, no ranking.
compare-report-set --detail summary emits compact JSON or separately versioned CSV after full validation; default full.
Summary retains settings, identities, diagnostics, summaries/deltas and omits expanded histories.
compare-report-set --names NAMES.json requires --detail summary; adds display names to JSON or separate v2 CSV.
Names JSON: schemaVersion:1, baseline:string, candidates:[1..255 strings matching input order/count].
Names: 1..256 UTF-8 bytes each; no NUL/unpaired surrogates; duplicates/whitespace preserved.
compare-report-set --manifest SET.json replaces positional paths: schemaVersion:1, baseline:path, candidates:[1..255 paths].
Relative reports resolve at the canonical manifest directory; input symlinks to regular files are allowed.
compare-report-set --output PATH atomically replaces a local regular file after validation; stdout default.
Parent must exist; report/manifest/names input aliases, output symlinks and special targets are rejected. No stdout "-" alias.
Grid JSON: for, keep_firing_for, maxEvaluations (required, 1..1000000).
At most 256 combinations; budget includes baseline. No ranking or optimum.
No PromQL, regex, live queries, scripts, notifications or deployment.
Config: evaluationInterval, threshold:{{op,value}}, for, keep_firing_for,
        missingData:"false"|"error", optional paired start/end (Unix seconds).
Durations: one nonnegative ms/s/m/h/d unit, e.g. "60s", "1.5m".
"""

SUBCOMMANDS = ("replay", "checkpoint", "compare", "compare-reports", "compare-report-set", "grid", "join")


def subcommand_help(command: str) -> str:
    """Usage lines for one command (an addition; the full reference is ``--help``)."""
    lines = [line.strip() for line in HELP.splitlines() if line.startswith(f"  {PROG} {command} ")]
    return "usage: " + "\n       ".join(lines) + f"\n\nRun '{PROG} --help' for semantics, formats and limits.\n"


MAX_BYTES = 64 * 1024 * 1024


def load(path: str) -> Any:
    """Read one local regular JSON file (at most 64 MiB, strict UTF-8, no duplicate keys)."""
    try:
        info = os.stat(path)
    except OSError as error:
        raise node_error(error, "stat", path) from None
    if not stat_module.S_ISREG(info.st_mode):
        raise FlapsimError(f"{path}: expected a regular local file")
    if info.st_size > MAX_BYTES:
        raise FlapsimError(f"{path}: input exceeds 64 MiB")
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_BYTES + 1)
    except OSError as error:
        raise node_error(error, "open", path) from None
    if len(data) > MAX_BYTES:
        raise FlapsimError(f"{path}: input exceeds 64 MiB")
    # Reject malformed UTF-8 instead of substituting replacement characters in labels.
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise FlapsimError("The encoded data was not valid for encoding utf-8") from None
    return parse_json(text, path)


def _write(stream: Any, text: str) -> None:
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        stream.flush()
        buffer.write(text.encode("utf-8"))
        buffer.flush()
    else:
        stream.write(text)
        stream.flush()


def _json(value: Any) -> str:
    return stringify(value, 2) + "\n"


def _arg(args: list, i: int) -> Any:
    return args[i] if i < len(args) else None


def _run(args: list) -> str | None:
    """Execute one command; return stdout text (None when written to --output)."""
    if len(args) == 1 and args[0] in ("--help", "-h"):
        return HELP
    if len(args) == 2 and args[0] in SUBCOMMANDS and args[1] in ("--help", "-h"):
        return subcommand_help(args[0])
    command = args[0] if args else None
    if command == "compare-reports":
        paths = args[1:3]
        if len(paths) != 2:
            raise FlapsimError("compare-reports: expected exactly two replay report JSON file paths")
        if any(p.startswith("-") for p in paths):
            raise FlapsimError("compare-reports: unsupported option; expected report JSON file paths")
        fmt = "json"
        seen_format = False
        for i in range(3, len(args), 2):
            if args[i] != "--format":
                raise FlapsimError(f"unsupported argument {stringify(args[i])}; use --help")
            if seen_format:
                raise FlapsimError("duplicate option --format")
            if _arg(args, i + 1) not in ("json", "csv"):
                raise FlapsimError("--format: expected json or csv")
            fmt = args[i + 1]
            seen_format = True
        reports = [load(p) for p in paths]
        return compare_reports_csv(*reports) if fmt == "csv" else _json(compare_reports(*reports))
    if command == "compare-report-set":
        option_start = next((i for i, a in enumerate(args) if i > 0 and a.startswith("-")), -1)
        paths_end = len(args) if option_start == -1 else option_start
        paths = args[1:paths_end]
        if paths and (len(paths) < 2 or len(paths) > 256):
            raise FlapsimError("compare-report-set: expected 2 to 256 replay report JSON file paths")
        fmt, detail = "json", "full"
        output_path = manifest_path = names_path = None
        seen: set[str] = set()
        for i in range(paths_end, len(args), 2):
            flag, value = args[i], _arg(args, i + 1)
            if flag not in ("--format", "--detail", "--output", "--manifest", "--names"):
                raise FlapsimError(f"unsupported argument {stringify(flag)}; use --help")
            if flag in seen:
                raise FlapsimError(f"duplicate option {flag}")
            seen.add(flag)
            if flag == "--format":
                if value not in ("json", "csv"):
                    raise FlapsimError("--format: expected json or csv")
                fmt = value
            elif flag == "--manifest":
                validate_manifest_path(value)
                manifest_path = value
            elif flag == "--names":
                validate_names_path(value)
                names_path = value
            elif flag == "--output":
                validate_output_path(value)
                output_path = value
            else:
                if value not in ("full", "summary"):
                    raise FlapsimError("--detail: expected full or summary")
                detail = value
        if names_path is not None and detail != "summary":
            raise FlapsimError("--names: requires --detail summary")
        inputs = list(paths)
        if manifest_path is not None:
            if paths:
                raise FlapsimError("--manifest: mutually exclusive with positional report paths")
            canonical = realpath(manifest_path)
            paths = report_set_manifest_paths(load(canonical), canonical)
            inputs.extend([manifest_path, canonical, *paths])
        elif len(paths) < 2:
            raise FlapsimError("compare-report-set: expected 2 to 256 replay report JSON file paths")
        names = None if names_path is None else report_set_names(load(names_path), len(paths) - 1)
        if names_path is not None:
            inputs.append(names_path)
        baseline, *candidates = [load(p) for p in paths]
        if names is not None:
            summary = compare_report_set(baseline, candidates, {"detail": "summary"})
            output = (format_report_set_summary_csv(summary, names) if fmt == "csv"
                      else _json(name_report_set_summary(summary, names)))
        elif fmt == "csv":
            output = (compare_report_set_summary_csv(baseline, candidates) if detail == "summary"
                      else compare_report_set_csv(baseline, candidates))
        else:
            output = _json(compare_report_set(baseline, candidates, {"detail": detail}))
        if output_path is None:
            return output
        write_local_output(output_path, inputs, output)
        return None
    if command == "join":
        paths = args[1:]
        if len(paths) < 2 or len(paths) > 256:
            raise FlapsimError("join: expected 2 to 256 replay report JSON file paths")
        if any(p.startswith("-") for p in paths):
            raise FlapsimError("join: unsupported option; expected report JSON file paths")
        return _json(join([load(p) for p in paths]))
    matrix_path = _arg(args, 1)
    rest = args[2:]
    if command in ("replay", "checkpoint"):
        allowed = ["--config", "--checkpoint-in"]
    elif command == "compare":
        allowed = ["--baseline", "--candidate", "--format"]
    elif command == "grid":
        allowed = ["--baseline", "--grid", "--format"]
    else:
        raise FlapsimError("expected command replay, checkpoint, compare, compare-reports, "
                           "compare-report-set, grid or join; use --help")
    if not matrix_path or matrix_path.startswith("-"):
        raise FlapsimError(f"{command}: expected a matrix JSON file path")
    flags: dict[str, str] = {}
    for i in range(0, len(rest), 2):
        flag, path = rest[i], _arg(rest, i + 1)
        if flag not in allowed:
            raise FlapsimError(f"unsupported argument {stringify(flag)}; use --help")
        if flag in flags:
            raise FlapsimError(f"duplicate option {flag}")
        if flag == "--format" and path not in ("json", "csv"):
            raise FlapsimError("--format: expected json or csv")
        if not path or path.startswith("-"):
            raise FlapsimError(f"{flag}: expected a JSON file path")
        flags[flag] = path
    for flag in allowed:
        if flag not in ("--checkpoint-in", "--format") and flag not in flags:
            raise FlapsimError(f"missing required option {flag}")
    matrix = load(matrix_path)
    if flags.get("--format") == "csv":
        if command == "compare":
            return compare_csv(matrix, load(flags["--baseline"]), load(flags["--candidate"]))
        return grid_csv(matrix, load(flags["--baseline"]), load(flags["--grid"]))
    if command == "grid":
        report = grid(matrix, load(flags["--baseline"]), load(flags["--grid"]))
    elif command == "compare":
        report = compare(matrix, load(flags["--baseline"]), load(flags["--candidate"]))
    elif "--checkpoint-in" in flags:
        report = replay(matrix, load(flags["--config"]), load(flags["--checkpoint-in"]))
    else:
        report = replay(matrix, load(flags["--config"]))
    return _json(report["checkpoint"] if command == "checkpoint" else report)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        output = _run(args)
    except Exception as error:  # noqa: BLE001 - every failure is one diagnostic line
        _write(sys.stderr, f"{PROG}: {error}\n")
        return 1
    if output is None:
        return 0
    try:
        _write(sys.stdout, output)
    except BrokenPipeError:
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except (OSError, ValueError, AttributeError):
            pass
        return 0
    except OSError as error:
        _write(sys.stderr, f"{PROG}: {error}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
