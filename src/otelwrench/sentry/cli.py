"""Offline command line interface. Does not import or execute input code."""
import argparse
import json
from pathlib import Path
import sys

from . import __version__
from .compression import DEFAULT_MAX_BYTES, MAX_BYTE_BUDGET, read_bounded, validate_byte_budget
from .mapping import LOSS_CATEGORIES, convert_bytes
from .output import write_output
from .parser import ConversionError


def byte_budget(text):
    try:
        return validate_byte_budget(int(text))
    except ConversionError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def read_binary_stdin(limit):
    # Closed descriptor 0 can leave sys.stdin as None. Embedders may instead
    # supply a text-only stream; do not silently re-encode it as binary input.
    handle = getattr(sys.stdin, "buffer", None)
    if handle is None:
        raise ConversionError("binary stdin is unavailable")
    if not callable(getattr(handle, "read", None)):
        raise ConversionError("binary stdin must provide a callable read method")
    try:
        # stdin belongs to the caller; reading must not close it.
        return read_bounded(handle, limit)
    except (OSError, ValueError) as exc:
        # Closed binary streams raise ValueError rather than OSError. Keep this
        # translation local to reading stdin so conversion bugs remain visible.
        raise ConversionError(f"cannot read binary stdin: {exc}") from exc


def main(argv=None):
    parser = argparse.ArgumentParser(prog="otelwrench sentry", description="Convert saved Sentry SDK envelopes or event JSON to offline OTLP fixtures.")
    parser.add_argument("--version", action="version", version=f"otelwrench sentry {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    convert = sub.add_parser("convert", help="validate and convert a local file or stdin; no network calls")
    convert.add_argument("input", help="local input path, or - to read binary stdin")
    convert.add_argument("--output-dir", type=Path, help="new directory under an existing parent (Linux or macOS)")
    convert.add_argument("--input-format", choices=("auto", "json", "envelope"), default="auto")
    convert.add_argument("--input-compression", choices=("none", "gzip"), default="none",
                         help="explicit input compression; decoded before format detection (default: none)")
    convert.add_argument("--skip-unsupported", action="store_true", help="record each unsupported envelope item as skipped")
    convert.add_argument("--fail-on-trace-conflicts", action="store_true",
                         help="reject conflicting duplicate span identities or confirmed cross-item cycles before output")
    convert.add_argument("--fail-on-loss", action="store_true",
                         help="reject any enumerated conversion loss after validation and before output")
    convert.add_argument("--fail-on-loss-category", action="append", choices=LOSS_CATEGORIES, default=[],
                         help="reject losses in this category after validation; repeat to select more categories")
    convert.add_argument("--dry-run", action="store_true", help="validate and print manifest; create no output files")
    convert.add_argument("--stdout-document", choices=("logs", "traces", "manifest"),
                         help="validate and print one converted document; excludes --output-dir and --dry-run")
    convert.add_argument("--error-format", choices=("text", "json"), default="text",
                         help="conversion error diagnostics on stderr (default: text); usage errors stay text")
    convert.add_argument("--max-input-bytes", type=byte_budget, default=DEFAULT_MAX_BYTES,
                         help=f"maximum file or stdin bytes, compressed when using gzip (1..{MAX_BYTE_BUDGET}; default: 64 MiB)")
    convert.add_argument("--max-decompressed-bytes", type=byte_budget, default=DEFAULT_MAX_BYTES,
                         help=f"maximum total gzip expansion bytes (1..{MAX_BYTE_BUDGET}; default: 64 MiB)")
    args = parser.parse_args(argv)
    if args.stdout_document is not None and (args.output_dir is not None or args.dry_run):
        parser.error("--stdout-document cannot be combined with --output-dir or --dry-run")
    if not args.dry_run and args.output_dir is None and args.stdout_document is None:
        parser.error("--output-dir is required unless --dry-run or --stdout-document is selected")
    try:
        # Preserve the argument spelling: ./- is a file, only bare - is stdin.
        if args.input == "-":
            data = read_binary_stdin(args.max_input_bytes)
        else:
            with Path(args.input).open("rb") as handle:
                data = read_bounded(handle, args.max_input_bytes)
        if len(data) > args.max_input_bytes:
            raise ConversionError(f"input exceeds --max-input-bytes={args.max_input_bytes}")
        documents = convert_bytes(data, input_format=args.input_format, skip_unsupported=args.skip_unsupported,
                                  input_compression=args.input_compression, max_input_bytes=args.max_input_bytes,
                                  max_decompressed_bytes=args.max_decompressed_bytes,
                                  fail_on_trace_conflicts=args.fail_on_trace_conflicts,
                                  fail_on_loss=args.fail_on_loss,
                                  fail_on_loss_categories=args.fail_on_loss_category)
        manifest = documents["manifest.json"]
        if args.stdout_document is not None or args.dry_run:
            selected = documents[f"{args.stdout_document}.json"] if args.stdout_document is not None else manifest
            # Serialize fully after conversion/policies, before emitting bytes.
            print(json.dumps(selected, ensure_ascii=False, allow_nan=False, indent=2))
        else:
            output = write_output(documents, args.output_dir)
            print(json.dumps({"outputDir": str(output), **manifest["summary"]}))
        return 0
    except (ConversionError, OSError, RecursionError) as exc:
        if args.error_format == "json":
            # Classify at the existing exception boundary, never from message
            # prose. Preserve wrapping already performed by readers/converters.
            kind = ("conversion_error" if isinstance(exc, ConversionError) else
                    "io_error" if isinstance(exc, OSError) else "recursion_error")
            print(json.dumps({"schemaVersion": 1, "kind": kind, "message": str(exc)},
                             ensure_ascii=True, allow_nan=False), file=sys.stderr)
        else:
            print(f"otelwrench sentry: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
