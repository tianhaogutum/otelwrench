# SPDX-License-Identifier: Apache-2.0
"""``otelwrench tracediff`` command line.

The parser is hand-written rather than argparse-based because its error
contract predates this package: every usage, input and config error exits 2,
and the *first* ``--format`` value decides whether that error is printed as
text or as a JSON ``INPUT_ERROR`` object, even when the bad option comes first.
"""
from __future__ import annotations

import errno
import os
import re
import stat
import sys

from . import __version__
from .core import compare, render_text
from .jsontext import InputError, is_safe_integer, parse_json, stringify

PROG = "otelwrench tracediff"
LIMIT = 64 * 1024 * 1024
HELP = f"""usage: {PROG} BASELINE.json CANDIDATE.json [--config POLICY.json] [--format text|json] [--missing-parent-limit N] [--upstream-warning-limit N] [--group-limit N] [--edge-limit N] [--comparison-limit N] [--observation-limit N] [--operation-diff-limit N] [--witnesses [--witness-path PATH.json] [--witness-source-limit N] [--witness-unresolved-limit N] [--witness-parent-limit N]]

Compare local OTLP resourceSpans or Jaeger {{data:[...]}} captures. No network or writes.
Defaults: absoluteP95Ms=20, relativeP95Percent=20, minSamples=20, failOnStructure=false.
Optional config gates: countGate {{maxChangePercent, optional maxChangeCount, minSamples=20}},
errorFractionGate {{maxIncreasePercent, optional maxIncreasePercentagePoints, minSamples=20}}.
Counts fail only above both configured budgets; maxChangeCount is a nonnegative safe integer.
Error fractions fail only above both configured budgets; points are absolute percentage points.
Count/error gates are observational, not causal.
Exit codes: 0 pass; 1 budget/structural gate failed; 2 input/config/usage error.
--witness-path PATH.json requires --witnesses: nonempty root-inclusive array of
{{service, operation, kind}} groups; selects that subtree, preserves whole-corpus diffs.
--witness-source-limit N requires --witnesses: positive safe integer; caps each side of
subtree references and path/aggregate relations, with totals and truncation markers.
--witness-unresolved-limit N requires --witnesses: positive safe integer; caps unresolved
entries per capture with exact whole-corpus totals/reason counts.
--witness-parent-limit N requires --witnesses: positive safe integer; caps parent IDs per
listed unresolved entry with exact totals/truncation markers; omitted means complete parents.
--missing-parent-limit N: positive safe integer; caps ordinary missing-parent diagnostic
entries per capture with exact totals; witnesses optional. Omitted means complete diagnostics.
--upstream-warning-limit N: positive safe integer; caps ordinary upstream-warning diagnostic
entries per capture with exact occurrence totals before span deduplication; witnesses optional.
--group-limit N: positive safe integer; caps ordinary group-statistics rows per capture
with exact distinct-group totals after span deduplication; witnesses optional.
--edge-limit N: positive safe integer; caps ordinary edge-statistics rows per capture
with exact distinct parent-group/child-group totals; edge.count keeps full multiplicity.
--comparison-limit N: positive safe integer; caps ordinary paired-group latency comparison
rows with exact totals including insufficient samples, excluding new/removed groups.
--observation-limit N: positive safe integer; caps ordinary rows per enabled count/error
observation gate with exact union-group totals including unpaired/insufficient rows.
--operation-diff-limit N: positive safe integer; caps added/removed operation-diff rows
independently with exact distinct-group totals, regardless of sample eligibility.
All ten caps are independent output-list limits, not memory or overall-output bounds.
Options: --help, --version, --witnesses (source-backed structural candidate sets; no causal alignment). Inputs must be regular UTF-8 JSON files <= 64 MiB each.
"""

VALUE_OPTIONS = ("--config", "--format", "--witness-path", "--witness-source-limit", "--witness-unresolved-limit",
                 "--witness-parent-limit", "--missing-parent-limit", "--upstream-warning-limit", "--group-limit",
                 "--edge-limit", "--comparison-limit", "--observation-limit", "--operation-diff-limit")
LIMIT_OPTIONS = {"--witness-source-limit": "witnessSourceLimit", "--witness-unresolved-limit": "witnessUnresolvedLimit",
                 "--witness-parent-limit": "witnessParentLimit", "--missing-parent-limit": "missingParentLimit",
                 "--upstream-warning-limit": "upstreamWarningLimit", "--group-limit": "groupLimit",
                 "--edge-limit": "edgeLimit", "--comparison-limit": "comparisonLimit",
                 "--observation-limit": "observationLimit", "--operation-diff-limit": "operationDiffLimit"}
_SURROGATE = re.compile("[\ud800-\udfff]")


def _write(stream, text: str) -> None:
    # Lone surrogates (e.g. from undecodable argv bytes) become U+FFFD, as Node writes them.
    data = _SURROGATE.sub("�", text)
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        stream.flush()
        buffer.write(data.encode("utf-8"))
        buffer.flush()
    else:
        stream.write(data)


def _load(path: str):
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise InputError(f"{path}: expected regular file")
        if info.st_size > LIMIT:
            raise InputError(f"{path}: file exceeds 64 MiB")
        # Read at most limit+1 bytes even if another process grows the file after stat.
        wanted = min(info.st_size + 1, LIMIT + 1)
        chunks, used = [], 0
        while used < wanted:
            chunk = os.read(fd, wanted - used)
            if not chunk:
                break
            chunks.append(chunk)
            used += len(chunk)
        if used != info.st_size:
            raise InputError(f"{path}: file changed size while reading")
        try:
            content = b"".join(chunks).decode("utf-8")
        except UnicodeDecodeError:
            raise InputError(f"{path}: invalid UTF-8") from None
        if content.startswith("﻿"):
            content = content[1:]  # TextDecoder('utf-8') strips one leading BOM.
        return parse_json(content, path)
    except InputError:
        raise
    except OSError as error:
        code = errno.errorcode.get(error.errno) if error.errno is not None else None
        raise InputError(f"{path}: {code or error.strerror or error}") from None
    finally:
        if fd is not None:
            os.close(fd)


def _output_format(args: list[str]) -> str:
    # Choose the error encoding before validation can stop at an earlier option.
    # Consume real value tokens as the parser does; never scan inside a value.
    i = 0
    while i < len(args):
        if args[i] in VALUE_OPTIONS:
            value = args[i + 1] if i + 1 < len(args) else None
            if args[i] == "--format":
                return "json" if value == "json" else "text"
            if value and not value.startswith("--"):
                i += 1
        i += 1
    return "text"


def _run(args: list[str]) -> int:
    if len(args) == 1 and args[0] in ("--help", "-h"):
        _write(sys.stdout, HELP)
        return 0
    if len(args) == 1 and args[0] == "--version":
        _write(sys.stdout, f"{__version__}\n")
        return 0
    paths: list[str] = []
    seen: set[str] = set()
    values: dict[str, str] = {}
    limits: dict[str, int] = {}
    witnesses = False
    fmt = "text"
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in VALUE_OPTIONS:
            if arg in seen:
                raise InputError(f"duplicate option {arg}")
            seen.add(arg)
            i += 1
            value = args[i] if i < len(args) else None
            if not value or value.startswith("--"):
                raise InputError(f"{arg} requires a value")
            if arg in LIMIT_OPTIONS:
                limit = parse_json(value, arg)
                if not is_safe_integer(limit) or limit <= 0:
                    raise InputError(f"{arg} requires a positive safe integer")
                limits[LIMIT_OPTIONS[arg]] = int(limit)
            elif arg == "--format":
                if value not in ("text", "json"):
                    raise InputError("--format must be text or json")
                fmt = value
            else:
                values[arg] = value
        elif arg == "--witnesses":
            if witnesses:
                raise InputError("duplicate option --witnesses")
            witnesses = True
        elif arg.startswith("-"):
            raise InputError(f"unsupported option {arg}")
        else:
            paths.append(arg)
        i += 1
    if len(paths) != 2:
        raise InputError("expected BASELINE.json and CANDIDATE.json; use --help")
    if values.get("--witness-path") and not witnesses:
        raise InputError("--witness-path requires --witnesses")
    for option in ("--witness-source-limit", "--witness-unresolved-limit", "--witness-parent-limit"):
        if option in seen and not witnesses:
            raise InputError(f"{option} requires --witnesses")
    options: dict = {"witnesses": witnesses}
    if "--witness-path" in values:
        options["witnessPath"] = _load(values["--witness-path"])
    config = _load(values["--config"]) if "--config" in values else {}
    baseline, candidate = _load(paths[0]), _load(paths[1])
    options.update(limits)
    try:
        report = compare(baseline, candidate, config, options)
    except Exception as error:  # noqa: BLE001 - every comparison failure is an input error (exit 2)
        message = error.message if isinstance(error, InputError) else str(error)
        raise InputError(f"comparison ({paths[0]} -> {paths[1]}): {message}") from None
    _write(sys.stdout, f"{stringify(report, 2)}\n" if fmt == "json" else render_text(report))
    return 1 if report["gate"]["failed"] else 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    fmt = _output_format(args)
    try:
        return _run(args)
    except BrokenPipeError:
        try:
            sys.stdout = open(os.devnull, "w")  # noqa: SIM115 - silence the interpreter's flush at exit
        except OSError:
            pass
        return 2
    except Exception as error:  # noqa: BLE001 - usage/input/config errors all exit 2
        message = error.message if isinstance(error, InputError) else str(error)
        if fmt == "json":
            _write(sys.stderr, stringify({"error": {"code": "INPUT_ERROR", "message": message}}) + "\n")
        else:
            _write(sys.stderr, f"{PROG}: {message}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
