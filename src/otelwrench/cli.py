"""Top-level ``otelwrench`` command: dispatches to one tool per subcommand.

Each tool lives in its own subpackage exposing ``cli.main(argv) -> int``. Tools
are imported lazily, so ``otelwrench --help`` stays fast and a missing optional
dependency only affects the command that needs it.
"""
from __future__ import annotations

from dataclasses import dataclass
import difflib
import importlib
import importlib.util
import sys

from . import __version__

PROG = "otelwrench"
REPO_URL = "https://github.com/tianhaogutum/otelwrench"


@dataclass(frozen=True)
class Command:
    name: str
    module: str
    summary: str
    group: str
    extra: str | None = None
    requires: tuple[str, ...] = ()


GROUPS = ("Metrics", "Traces", "Logs", "Alerts", "Migration")

COMMANDS: tuple[Command, ...] = (
    Command("preflight", "otelwrench.preflight.cli",
            "One CI verdict on a before/after /metrics pair: renames, removals, type and label changes, growth",
            "Metrics"),
    Command("prom-names", "otelwrench.promnames.cli",
            "Preview OTLP-to-Prometheus metric names, units and name collisions for gauges and sums",
            "Metrics"),
    Command("promdiff", "otelwrench.promdiff.cli",
            "Gate two Prometheus exposition snapshots for breaking schema changes and series growth",
            "Metrics"),
    Command("cardinality", "otelwrench.cardinality.cli",
            "What-if label removal on saved Prometheus scrapes: series saved and collisions created",
            "Metrics"),
    Command("tracepack", "otelwrench.tracepack.cli",
            "Convert saved traces between OTLP, Jaeger and Zipkin JSON with loss reports and bundles",
            "Traces"),
    Command("tracediff", "otelwrench.tracediff.cli",
            "Diff two saved OTLP/Jaeger trace corpora by operation latency and topology, with CI budgets",
            "Traces"),
    Command("spanmetrics", "otelwrench.spanmetrics.cli",
            "Derive deterministic Prometheus RED metrics from OTLP JSON traces; compare dimension sets",
            "Traces"),
    Command("tailsim", "otelwrench.tailsim.cli",
            "Replay tail-sampling policies on saved OTLP traces and compare what each policy keeps",
            "Traces"),
    Command("loki", "otelwrench.loki.cli",
            "Preview how Loki maps OTLP log attributes to stream labels and structured metadata",
            "Logs"),
    Command("alert-flapsim", "otelwrench.flapsim.cli",
            "Replay alert rules over a saved query_range matrix and compare thresholds and for durations",
            "Alerts"),
    Command("sentry", "otelwrench.sentry.cli",
            "Convert saved Sentry SDK envelopes to OTLP JSON logs and traces with a loss manifest",
            "Migration"),
)
BY_NAME = {command.name: command for command in COMMANDS}

# Former standalone tool names, used only to point users at the new subcommand.
# tracepack, tracediff, tailsim and alert-flapsim kept their names as subcommands.
LEGACY = {
    "sentry2otlp": "sentry", "otlp2loki-preview": "loki", "otlp-prom-namecheck": "prom-names",
    "spanmetrics-offline": "spanmetrics", "promdiff-guard": "promdiff", "promcard-trim": "cardinality",
    "tracepack": "tracepack", "tracediff": "tracediff", "tailsim": "tailsim", "alert-flapsim": "alert-flapsim",
    "flapsim": "alert-flapsim",
}


def _present(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _installed(command: Command) -> bool:
    return _present(command.module.rpartition(".")[0])


def _missing_requirements(command: Command) -> bool:
    return any(not _present(name) for name in command.requires)


def _install_hint(command: Command) -> str:
    return (f"{PROG} {command.name}: missing optional dependencies; "
            f"install them with: pip install '{PROG}[{command.extra}]'")


def _help_without_extra(command: Command) -> str:
    """A usage summary that needs only the command's package, not its optional extra."""
    lines = [f"usage: {PROG} {command.name} <subcommand> [args...]", "", command.summary + "."]
    try:
        subcommands = getattr(importlib.import_module(command.module.rpartition(".")[0]), "SUBCOMMANDS", {})
    except ImportError:
        subcommands = {}
    if subcommands:
        width = max(map(len, subcommands)) + 2
        lines += ["", "subcommands:", *(f"  {name.ljust(width)}{text}" for name, text in subcommands.items())]
    return "\n".join(lines) + "\n\n"


def overview() -> str:
    width = max(len(command.name) for command in COMMANDS) + 2
    lines = [
        f"{PROG} {__version__} - offline checks for OpenTelemetry and Prometheus pipelines",
        "",
        f"usage: {PROG} <command> [args...]",
        f"       {PROG} <command> --help",
    ]
    for group in GROUPS:
        lines += ["", f"{group}:"]
        for command in COMMANDS:
            if command.group != group:
                continue
            note = ""
            if not _installed(command):
                note = "  (coming soon)"
            elif command.extra and _missing_requirements(command):
                note = f"  (needs {PROG}[{command.extra}])"
            lines.append(f"  {command.name.ljust(width)}{command.summary}{note}")
    lines += [
        "",
        "options:",
        "  -h, --help     show this overview",
        "  -V, --version  show the version and exit",
        "",
        f"Docs and issues: {REPO_URL}",
    ]
    return "\n".join(lines) + "\n"


def _unknown(name: str) -> int:
    message = f"{PROG}: unknown command {name!r}"
    if name in LEGACY:
        message += f"; {name} is now '{PROG} {LEGACY[name]}'"
    else:
        match = difflib.get_close_matches(name, [*BY_NAME, *LEGACY], n=1, cutoff=0.6)
        if match:
            suggestion = LEGACY.get(match[0], match[0])
            message += f"; did you mean '{suggestion}'?"
    sys.stderr.write(message + f"\nRun '{PROG} --help' to list commands.\n")
    return 2


def run(command: Command, argv: list[str]) -> int:
    if not _installed(command):
        sys.stderr.write(f"{PROG}: command {command.name!r} is not available in "
                         f"{PROG} {__version__} yet; see {REPO_URL}\n")
        return 2
    if command.extra and _missing_requirements(command):
        if any(arg in ("-h", "--help") for arg in argv):
            sys.stdout.write(_help_without_extra(command))
            sys.stdout.write(_install_hint(command) + "\n")
            return 0
        sys.stderr.write(_install_hint(command) + "\n")
        return 2
    try:
        module = importlib.import_module(command.module)
    except ModuleNotFoundError as exc:
        missing = exc.name or ""
        if command.extra and any(req == missing or req.startswith(missing + ".") for req in command.requires):
            sys.stderr.write(_install_hint(command) + "\n")
            return 2
        raise
    result = module.main(argv)
    return 0 if result is None else result


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        sys.stdout.write(overview())
        return 0
    first, rest = args[0], args[1:]
    if first in ("-V", "--version"):
        sys.stdout.write(f"{PROG} {__version__}\n")
        return 0
    if first == "help":
        if not rest:
            sys.stdout.write(overview())
            return 0
        first, rest = rest[0], ["--help"]
    if first.startswith("-"):
        sys.stderr.write(f"{PROG}: unknown option {first!r}\nRun '{PROG} --help' for usage.\n")
        return 2
    command = BY_NAME.get(first)
    if command is None:
        return _unknown(first)
    return run(command, rest)


if __name__ == "__main__":
    raise SystemExit(main())
