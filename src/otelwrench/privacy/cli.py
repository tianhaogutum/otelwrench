from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version as package_version
import json
import os
from pathlib import Path
import platform
import re
import signal as signals
import sys
import tempfile

from . import __version__
from .collector import digest, identify, run_pipeline, run_stage, verify_pin
from .evidence import (
    count_records,
    diagnostics,
    differences,
    ignored_observations,
    inventory,
    type_coverage,
    view,
)
from .expectations import evaluate, validate
from .explain import explain_ignored
from .inputs import ReplayError, bounded_read, load_yaml, sample_messages, selected_pipeline
from .ingestion import audit_ingestion


def write_json(path: Path, obj: object) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}-",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(obj, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def replay(args: argparse.Namespace) -> int:
    config_bytes = bounded_read(args.config, 1024 * 1024)
    config = load_yaml(args.config, config_bytes)
    signal, processors = selected_pipeline(config, args.pipeline)
    expectation_bytes = bounded_read(args.expect, 1024 * 1024) if args.expect else None
    expectations = load_yaml(args.expect, expectation_bytes) if args.expect else None
    if expectations is not None:
        validate(expectations)
    binary = args.collector.resolve(strict=True)
    runtime = verify_pin(binary, args.lock)
    sample_bytes = bounded_read(args.sample)
    messages, content_type = sample_messages(args.sample, signal, args.format, sample_bytes)
    if args.out.exists():
        raise ReplayError(f"output directory already exists: {args.out}; choose a fresh path")
    args.out.mkdir(parents=True)
    started = datetime.now(timezone.utc).isoformat()
    report = {
        "schema_version": 1,
        "tool_version": __version__,
        "started_at": started,
        "status": "running",
        "privacy_verdict": "not_asserted",
        "collector": runtime,
        "environment": {
            "python": platform.python_version(),
            "system": platform.system(),
            "machine": platform.machine(),
        },
        "dependencies": {
            name: package_version(name)
            for name in (
                "PyYAML",
                "jsonpath-ng",
                "jsonpointer",
                "deepdiff",
                "opentelemetry-proto",
                "protobuf",
            )
        },
        "input": {
            "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
            "sample_sha256": hashlib.sha256(sample_bytes).hexdigest(),
            "expectations_sha256": hashlib.sha256(expectation_bytes).hexdigest()
            if expectation_bytes is not None
            else None,
            "content_type": content_type,
            "requests": len(messages),
        },
        "pipeline": args.pipeline,
        "signal": signal,
        "execution_mode": "whole local pipeline result with verified sequential stage attribution",
        "stages": [],
        "scope": {
            "receivers_exporters_extensions": "replaced; original endpoints are never started",
            "other_pipelines": "not executed",
            "untested_signals": sorted({"traces", "logs", "metrics"} - {signal}),
            "rule_attribution": "actual upstream summary attributes and separately labelled observations; "
            "per-regex match tracing is not exposed by the Collector",
            "record_alignment": "positional for records; attribute order is normalized by key",
            "claim": "evidence only for the supplied samples, selected processors, and binary pin",
        },
    }
    write_json(args.out / "report.json", report)
    try:
        baseline = run_stage(
            binary, signal, messages, content_type, args.out / "stages/00-input", None, args.timeout
        )
        input_count = count_records(baseline, signal)
        if input_count == 0:
            raise ReplayError("sample produced zero input records; refusing a vacuous privacy run")
        write_json(args.out / "input.json", baseline)
        before = view(baseline)
        report["ingestion_audit"] = audit_ingestion(messages, content_type, signal, baseline)
        write_json(args.out / "input.view.json", before)
        current = baseline
        current_view = before
        for index, processor in enumerate(processors, 1):
            name = processor[0]
            slug = re.sub(r"[^a-zA-Z0-9_.-]", "-", name)
            output = run_stage(
                binary,
                signal,
                [json.dumps(current).encode()],
                "application/json",
                args.out / f"stages/{index:02d}-{slug}",
                processor,
                args.timeout,
            )
            output_view = view(output)
            stage = {
                "processor": name,
                "input_records": count_records(current, signal),
                "output_records": count_records(output, signal),
                "changes": differences(current_view, output_view),
                "summary_attributes": diagnostics(current_view, output_view),
                "ignored_key_observations": ignored_observations(
                    current_view, output_view, processor[1]
                )
                if name.split("/", 1)[0] == "redaction"
                else [],
            }
            if args.explain_ignored and name.split("/", 1)[0] == "redaction":
                stage["ignored_rule_counterfactual"] = explain_ignored(
                    binary,
                    signal,
                    current,
                    output_view,
                    processor,
                    args.out / f"stages/{index:02d}-{slug}-without-ignored",
                    args.timeout,
                )
            report["stages"].append(stage)
            current, current_view = output, output_view
        if processors:
            actual = run_pipeline(
                binary,
                signal,
                messages,
                content_type,
                args.out / "stages/99-whole-pipeline",
                processors,
                args.timeout,
            )
            actual_view = view(actual)
            discrepancy = differences(current_view, actual_view)
            report["chain_verification"] = {
                "matched": not discrepancy,
                "differences": discrepancy,
                "source": "actual whole pipeline on original requests versus isolated stages",
            }
            if discrepancy:
                write_json(args.out / "output.json", actual)
                write_json(args.out / "output.view.json", actual_view)
                raise ReplayError(
                    "whole-pipeline output differs from sequential attribution; the processor chain "
                    "may be nondeterministic or batch-sensitive; raw actual output is preserved"
                )
            current, current_view = actual, actual_view
        else:
            report["chain_verification"] = {
                "matched": True,
                "source": "no processors; input baseline",
            }
        if digest(binary) != runtime["sha256"]:
            raise ReplayError(
                "Collector executable changed during replay; refusing mixed-runtime evidence"
            )
        write_json(args.out / "output.json", current)
        write_json(args.out / "output.view.json", current_view)
        rows = inventory(before, current_view)
        checks = evaluate(expectations, before, current_view) if expectations is not None else []
        failed = sum(not check["passed"] for check in checks)
        report.update(
            {
                "status": "completed",
                "privacy_verdict": "fail" if failed else "pass" if checks else "not_asserted",
                "input_records": input_count,
                "output_records": count_records(current, signal),
                "checks": checks,
                "failed_checks": failed,
                "inventory": rows,
                "type_coverage": type_coverage(before, current_view),
                "retained_outside_attributes": [
                    row
                    for row in rows
                    if not row["area"].endswith("attributes") and row["status"] == "retained"
                ],
                "outputs": {
                    "raw": "output.json",
                    "view": "output.view.json",
                    "raw_sha256": digest(args.out / "output.json"),
                    "view_sha256": digest(args.out / "output.view.json"),
                },
            }
        )
        print(
            f"{input_count} input {signal} record(s), {report['output_records']} output; "
            f"{len(processors)} processor(s); privacy: {report['privacy_verdict']}"
        )
        if checks:
            print(f"Checks: {len(checks) - failed} passed, {failed} failed")
        print(f"Evidence: {args.out / 'report.json'}")
        return 1 if failed else 0
    except KeyboardInterrupt:
        report.update(status="interrupted", privacy_verdict="not_evaluated")
        raise
    except (ReplayError, OSError, ValueError, RecursionError) as exc:
        report.update(status="error", privacy_verdict="not_evaluated", error=str(exc))
        raise
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_json(args.out / "report.json", report)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="otelwrench privacy",
        description="Replay privacy expectations through a real, SHA-256-pinned local OTel Collector.",
    )
    root.add_argument("--version", action="version", version=f"otelwrench privacy {__version__}")
    sub = root.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="replay one selected local processor pipeline")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument(
        "--sample", type=Path, required=True, help="OTLP JSON, JSONL, or request protobuf"
    )
    run.add_argument("--pipeline", required=True, help="exact service.pipelines key")
    run.add_argument(
        "--expect", type=Path, help="privacy assertions; omission gives observations only"
    )
    run.add_argument(
        "--explain-ignored",
        action="store_true",
        help="separately replay each redaction stage with ignored settings disabled",
    )
    run.add_argument(
        "--collector",
        type=Path,
        default=Path(".tools/otelcol-contrib"),
        help="Collector executable (default: .tools/otelcol-contrib)",
    )
    run.add_argument(
        "--lock",
        type=Path,
        default=Path(".tools/collector.lock.json"),
        help="version/SHA-256 lock the executable must match (default: .tools/collector.lock.json)",
    )
    run.add_argument("--out", type=Path, required=True, help="fresh directory for local evidence")
    run.add_argument("--format", choices=["auto", "json", "protobuf"], default="auto")
    run.add_argument(
        "--timeout", type=float, default=15, help="seconds per bounded Collector stage"
    )
    pin = sub.add_parser("pin", help="record an explicitly chosen binary's version and SHA-256")
    pin.add_argument("--collector", type=Path, required=True)
    pin.add_argument("--version", dest="expected_version", required=True)
    pin.add_argument("--out", type=Path, default=Path(".tools/collector.lock.json"))
    # Listed here for --help only; main() hands its arguments to install.main() unparsed.
    sub.add_parser(
        "install-collector",
        help="download the pinned official Collector archive, verify its SHA-256 and pin it",
        add_help=False,
    )
    return root


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["install-collector"]:
        from .install import main as install_main

        return install_main(argv[1:])
    args = parser().parse_args(argv)
    previous_sigterm = signals.getsignal(signals.SIGTERM)

    def interrupted(_signum, _frame):
        raise KeyboardInterrupt

    signals.signal(signals.SIGTERM, interrupted)
    try:
        if args.command == "pin":
            actual = identify(args.collector)
            if actual["version"] != args.expected_version:
                raise ReplayError(
                    f"expected version {args.expected_version}, found {actual['version']}"
                )
            args.out.parent.mkdir(parents=True, exist_ok=True)
            write_json(args.out, actual)
            print(f"Pinned Collector {actual['version']} ({actual['sha256']}) in {args.out}")
            return 0
        if not 0.2 <= args.timeout <= 60:
            raise ReplayError("timeout must be between 0.2 and 60 seconds per stage")
        return replay(args)
    except (ReplayError, OSError, ValueError, RecursionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted; Collector subprocess was stopped", file=sys.stderr)
        return 130
    finally:
        signals.signal(signals.SIGTERM, previous_sigterm)


if __name__ == "__main__":
    raise SystemExit(main())
