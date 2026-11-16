"""Local CLI. No HTTP clients, command execution, or implicit output files."""

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

import yaml

from . import __version__
from .config import load_profile
from .mapping import compare, explain, preview, push_json
from .strict import PreviewError, read_json


def encoded(data):
    return json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def write_new_json(path, data):
    """Publish a fully written file atomically, never replace an existing path."""
    path = Path(path)
    content = encoded(data)
    fd, temporary = tempfile.mkstemp(prefix=".otlp2loki-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        os.unlink(temporary)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="otelwrench loki", description="Offline, strict OTLP log mapping preview; never sends logs")
    parser.add_argument("--version", action="version", version=f"otelwrench loki {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("preview", help="explain one explicit mapping profile as JSON on stdout")
    p.add_argument("input", type=Path, help="one camelCase OTLP resourceLogs JSON file")
    p.add_argument("--config", required=True, type=Path, help="strict limits_config.otlp_config YAML profile")
    p.add_argument("--push-json", type=Path, help="write push projection to a NEW local file; never sends it")
    e = commands.add_parser("explain", help="trace selector precedence and normalization for one record/output key")
    e.add_argument("input", type=Path)
    e.add_argument("--config", required=True, type=Path)
    e.add_argument("--record-id", required=True, help="exact record ID from preview; quote brackets in the shell")
    e.add_argument("--key", help="normalized output key, including dropped keys; omit to trace all placements")
    c = commands.add_parser("compare", help="compare observed streams and placements under two explicit profiles")
    c.add_argument("input", type=Path)
    c.add_argument("--before", required=True, type=Path)
    c.add_argument("--after", required=True, type=Path)
    c.add_argument("--include-records", action="store_true",
                   help="include every record's full before/after projection, including unchanged placements and dropped values")
    c.add_argument("--record-id", help="exact record ID to trace under both profiles; quote brackets in the shell")
    c.add_argument("--key", help="normalized output key to trace; requires --record-id; omit for all placements")
    c.add_argument("--decision-source", choices=("resource", "scope", "log", "field"),
                   help="select a source for local decisions and pre-view counts; requires --record-id; full trace values remain")
    filters = c.add_mutually_exclusive_group()
    filters.add_argument("--changed-decisions-only", action="store_true",
                   help="keep only winner changes in selected decisions; requires --record-id; full trace values remain")
    filters.add_argument("--placement-decisions-only", action="store_true",
                   help="keep only placement changes in selected decisions; requires --record-id; full trace values remain")
    filters.add_argument("--reason-decisions-only", action="store_true",
                   help="keep only winner changes with unchanged placement in selected decisions; requires --record-id; full trace values remain")
    filters.add_argument("--selector-decisions-only", action="store_true",
                   help="keep only literal winning-selector changes in selected decisions; requires --record-id; full trace values remain")
    filters.add_argument("--rule-ref-decisions-only", action="store_true",
                   help="keep only literal winning rule-reference changes in selected decisions; requires --record-id; full trace values remain")
    filters.add_argument("--unchanged-decisions-only", action="store_true",
                   help="keep only unchanged literal winners in selected decisions; requires --record-id; full trace values remain")
    args = parser.parse_args(argv)
    if args.command == "compare" and args.key is not None and args.record_id is None:
        parser.error("--key requires --record-id")
    if args.command == "compare" and args.decision_source is not None and args.record_id is None:
        parser.error("--decision-source requires --record-id")
    if args.command == "compare" and args.changed_decisions_only and args.record_id is None:
        parser.error("--changed-decisions-only requires --record-id")
    if args.command == "compare" and args.placement_decisions_only and args.record_id is None:
        parser.error("--placement-decisions-only requires --record-id")
    if args.command == "compare" and args.reason_decisions_only and args.record_id is None:
        parser.error("--reason-decisions-only requires --record-id")
    if args.command == "compare" and args.selector_decisions_only and args.record_id is None:
        parser.error("--selector-decisions-only requires --record-id")
    if args.command == "compare" and args.rule_ref_decisions_only and args.record_id is None:
        parser.error("--rule-ref-decisions-only requires --record-id")
    if args.command == "compare" and args.unchanged_decisions_only and args.record_id is None:
        parser.error("--unchanged-decisions-only requires --record-id")
    try:
        data = read_json(args.input)
        if args.command == "preview":
            report = preview(data, load_profile(args.config))
            if args.push_json is not None:
                write_new_json(args.push_json, push_json(report))
        elif args.command == "explain":
            report = explain(data, load_profile(args.config), record_id=args.record_id, key=args.key)
        else:
            report = compare(data, load_profile(args.before), load_profile(args.after),
                             include_records=args.include_records, record_id=args.record_id, key=args.key,
                             changed_decisions_only=args.changed_decisions_only,
                             placement_decisions_only=args.placement_decisions_only,
                             reason_decisions_only=args.reason_decisions_only,
                             selector_decisions_only=args.selector_decisions_only,
                             rule_ref_decisions_only=args.rule_ref_decisions_only,
                             unchanged_decisions_only=args.unchanged_decisions_only,
                             decision_source=args.decision_source)
        sys.stdout.write(encoded(report))
        return 0
    except (PreviewError, OSError, UnicodeError, ValueError, yaml.YAMLError, RecursionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
