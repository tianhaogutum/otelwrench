# SPDX-License-Identifier: Apache-2.0
"""File-only command line interface; validation completes before output writes."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

from .core import ConversionError, STRATEGIES, WITH, convert, load_json
from .bundle import atomic_write_bundle, build_bundle, inspect_bundle, verify_bundle


def atomic_write(path: Path, content: str):
    """Each file is atomic; a pair of files is not a filesystem transaction."""
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="otelwrench prom-names", description="Offline OTLP gauge/sum snapshot and name collision check")
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("convert")
    command.add_argument("input", type=Path)
    command.add_argument("--strategy", choices=STRATEGIES, default=WITH)
    destination = command.add_mutually_exclusive_group(required=True)
    destination.add_argument("--output", type=Path, help="legacy separate exposition and mapping files")
    destination.add_argument("--bundle", type=Path, help="atomically replace one ZIP with exposition, mapping and hash manifest")
    command.add_argument("--mapping", type=Path, help="default: OUTPUT.mapping.json")
    command.add_argument("--promote-resource", action="append", default=[], metavar="EXACT_KEY")
    command.add_argument("--allow-nonfinite", action="store_true")
    command.add_argument("--convert-unit", choices=("ms:s",),
                         help="opt in to exact millisecond-to-second sample/unit/name conversion; other supported units stay unscaled")
    command.add_argument("--convert-metric", action="append", metavar="EXACT_NAME",
                         help="limit ms:s conversion to these exact raw metric names (repeatable); every name must match unit ms")
    command.add_argument("--dry-run", action="store_true", help="validate and print mapping; write no files")
    verify = commands.add_parser("verify", help="check bundle hashes; optionally reproduce from original input")
    verify.add_argument("bundle", type=Path)
    verify.add_argument("--input", type=Path, help="reproduce using exact original input bytes and stored configuration")
    inspect = commands.add_parser("inspect", help="verify bundle hashes and summarize samples, losses and metric names")
    inspect.add_argument("bundle", type=Path)
    inspect.add_argument("--input", type=Path, help="reproduce using exact original input bytes before summarizing")
    inspect.add_argument("--metric", action="append", metavar="EXACT_NAME",
                         help="show only these exact raw metric names (repeatable); whole-bundle validation and totals remain")
    args = parser.parse_args(argv)
    if args.command == "convert" and args.bundle and args.mapping:
        parser.error("--mapping requires --output; bundle member names are fixed")
    try:
        if args.command == "inspect":
            result = inspect_bundle(args.bundle, source=args.input.read_bytes() if args.input else None,
                                    metric_names=args.metric)
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if args.command == "verify":
            result = verify_bundle(args.bundle, source=args.input.read_bytes() if args.input else None)
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        mapping = (args.mapping or Path(str(args.output) + ".mapping.json")) if args.output else None
        destinations = (args.bundle,) if args.bundle else (args.output, mapping)
        all_paths = (args.input, *destinations)
        path_names = "input and bundle" if args.bundle else "input, output and mapping"
        paths = [path.resolve() for path in all_paths]
        if len(set(paths)) != len(paths):
            raise ConversionError(f"{path_names} must be different paths")
        # Also protect input data from hard-link aliases.
        for i, first in enumerate(all_paths):
            for second in all_paths[i + 1:]:
                if first.exists() and second.exists() and first.samefile(second):
                    raise ConversionError(f"{path_names} must not be hard-link aliases")
        source = args.input.read_bytes()
        configuration = dict(strategy=args.strategy, promote_resource=args.promote_resource,
                             allow_nonfinite=args.allow_nonfinite, convert_unit=args.convert_unit,
                             convert_metrics=args.convert_metric)
        if args.bundle:
            members, report = build_bundle(source, **configuration)
        else:
            exposition, report = convert(load_json(source.decode("utf-8")), **configuration)
        encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.dry_run:
            sys.stdout.write(encoded)
        else:
            # Preflight both directories before writing either artifact.
            for path in destinations:
                if not path.parent.is_dir() or path.is_dir():
                    raise ConversionError(f"output path is not a writable file destination: {path}")
            if args.bundle:
                atomic_write_bundle(args.bundle, members)
                print(f"wrote {report['sample_count']} samples in bundle {args.bundle}; losses: {len(report['losses'])}", file=sys.stderr)
            else:
                atomic_write(mapping, encoded)
                atomic_write(args.output, exposition)
                print(f"wrote {report['sample_count']} samples to {args.output}; mapping: {mapping}; losses: {len(report['losses'])}", file=sys.stderr)
        return 0
    except ConversionError as exc:
        # Semantic validation finishes before writes, preserving previous artifacts.
        print(json.dumps(exc.report, ensure_ascii=False, indent=2, sort_keys=True), file=sys.stderr)
        return 2
    except (OSError, UnicodeError, RecursionError) as exc:
        if args.command in ("verify", "inspect"):
            policy = "Read-only bundle verification/inspection; no files are written."
        else:
            policy = ("Bundle publication uses one atomic replacement; a failed staged write leaves the previous bundle intact or no new bundle."
                      if args.bundle else
                      "Each file is replaced atomically; the pair is not transactional. Check both artifacts after an I/O failure.")
        print(json.dumps({"status": "error", "errors": [str(exc)], "collisions": [],
                          "output_policy": policy}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
