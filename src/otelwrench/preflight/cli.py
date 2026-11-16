"""``otelwrench preflight`` command line. Exit 0 clean, 1 findings at/above --fail-on, 2 usage/input error."""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from ..promdiff.config import Config
from ..promdiff.parser import InputError
from .core import build, load_side
from .queries import load_queries

EPILOG = """\
exit status: 0 nothing at or above --fail-on, 1 findings at or above it, 2 usage or
input error. Read-only: nothing is written; --emit prints a file for promdiff to stdout.

examples:
  otelwrench preflight before.prom after.prom
  otelwrench preflight before.prom after.prom --queries dashboard.json --queries alerts.yaml
  otelwrench preflight before.prom after.prom --emit rename-map > renames.json
  otelwrench preflight before.prom after.prom --format json --fail-on warning
"""

GROUPS = {
    "renamed": "Renamed by the OTLP-to-Prometheus naming rules",
    "label_renamed": "Label renamed",
    "family_removed": "Family removed",
    "type_changed": "Type changed",
    "label_schema_changed": "Label set changed",
    "series_growth": "Series growth over budget",
}
KIND_NAMES = {"panel": "dashboard panels", "alert": "alerts", "record": "recording rules", "query": "queries"}


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # usage errors exit 2, like the other commands
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}\n")


def parser() -> argparse.ArgumentParser:
    cli = Parser(prog="otelwrench preflight", formatter_class=argparse.RawDescriptionHelpFormatter,
                 epilog=EPILOG,
                 description="One CI verdict on a before/after Prometheus /metrics pair: removed or renamed "
                             "families, type and label changes, series growth, and the dashboards and "
                             "alerts they break, with the follow-up command for each finding.")
    cli.add_argument("--version", action="version", version=f"otelwrench preflight {__version__}")
    cli.add_argument("before", help="baseline /metrics capture (Prometheus text 0.0.4)")
    cli.add_argument("after", help="candidate /metrics capture, e.g. after an exporter or SDK upgrade")
    cli.add_argument("--queries", action="append", default=[], metavar="FILE",
                     help="Grafana dashboard JSON, Prometheus rule YAML, or one PromQL expression per line "
                          "(optionally 'name: expr'); repeatable")
    cli.add_argument("--config", metavar="POLICY.json", help="promdiff policy and growth budget (JSON)")
    cli.add_argument("--max-growth", type=float, metavar="PERCENT",
                     help="series growth budget in percent (default 20, or the --config value)")
    cli.add_argument("--fail-on", choices=("error", "warning", "none"), default="error",
                     help="lowest severity that makes the exit status 1 (default: error)")
    cli.add_argument("--format", choices=("text", "json"), default="text")
    cli.add_argument("--emit", choices=("rename-map", "allowlist"),
                     help="print the inferred promdiff --rename-map, or a promdiff --allowlist that accepts "
                          "every remaining change, instead of the report")
    cli.add_argument("--no-color", action="store_true", help="disable color (also: NO_COLOR, non-TTY stdout)")
    return cli


class Style:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.enabled and text else text

    def bold(self, t): return self._wrap("1", t)
    def dim(self, t): return self._wrap("2", t)
    def red(self, t): return self._wrap("31", t)
    def green(self, t): return self._wrap("32", t)
    def yellow(self, t): return self._wrap("33", t)
    def magenta(self, t): return self._wrap("35", t)
    def cyan(self, t): return self._wrap("36", t)


def _count(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _mark(severity: str, s: Style) -> str:
    return s.red("✗") if severity == "error" else s.yellow("!")


def _growth_lines(finding: dict, s: Style) -> list[str]:
    out = []
    for d in finding["drivers"]:
        line = f"    {d['family']} {d['series_before']} -> {d['series_after']} series"
        if d["label"]:
            line += f": {s.bold(d['label'])} {d['values_before']} -> {d['values_after']} values"
        out.append(line)
        check = d["drop_check"]
        if check and not check["safe"]:
            out.append(s.dim(f"      dropping {d['label']} by relabeling merges {check['series_before']} series into "
                             f"{check['series_after']} ({_count(check['collision_groups'], 'collision group')}): "
                             "aggregate it away at the source (SDK view or Collector processor)"))
        elif check:
            out.append(s.dim(f"      dropping {d['label']} is collision-free on this capture: "
                             "--emit-config prints the metric_relabel_configs rule"))
    return out


def render_text(doc: dict, s: Style) -> str:
    summary, findings = doc["summary"], doc["findings"]
    out = [s.bold("otelwrench preflight") + "  " + f"{doc['before']} -> {doc['after']}"]
    growth = summary["growth_percent"]
    facts = [f"{summary['series_before']} -> {summary['series_after']} series"
             + (f" ({growth:+.1f}%)" if growth else ""),
             f"{summary['families_before']} -> {summary['families_after']} families"]
    if summary["queries"]:
        facts.append(f"{summary['queries']} dashboard and alert queries")
    out.append("  " + s.dim(" · ".join(facts)))

    previous: list[str] = []
    for kind, heading in GROUPS.items():
        group = [f for f in findings if f["kind"] == kind]
        if not group:
            continue
        severity = "error" if any(f["severity"] == "error" for f in group) else "warning"
        out += ["", f"{_mark(severity, s)} {s.bold(heading)}  {s.dim(group[0]['source'])}"]
        if kind == "series_growth":
            out.append(f"    {group[0]['title']}")
            out += _growth_lines(group[0], s)
        else:
            width = max(len(f["title"]) for f in group)
            fixes = [f["fix"] for f in group]
            shared = max(set(fixes), key=fixes.count)
            for f in group:
                detail = f"  {s.dim(f['detail'])}" if f["detail"] else ""
                out.append(f"    {f['title']:<{width}}{detail}")
                if f["queries"]:
                    out.append(f"      {s.red('breaks')}  " + " · ".join(f["queries"]))
                if f["fix"] != shared:
                    out.append(f"      {s.yellow('note')}    {f['fix']}")
            out.append(f"    {s.dim('fix')}     {shared}")
        commands = list(dict.fromkeys(c for f in group for c in f["next"]))
        if commands == previous:
            out.append(f"    {s.dim('next')}    {s.dim('same commands as above')}")
        for n, command in enumerate(commands if commands != previous else []):
            out.append(f"    {s.dim('next' if n == 0 else '    ')}    {s.cyan(command)}")
        previous = commands

    if not findings:
        out += ["", s.green("No removed, renamed or retyped families, label changes or growth over budget.")]
    rest = [f for f in findings if f["kind"] not in GROUPS]
    if rest:
        severity = "error" if any(f["severity"] == "error" for f in rest) else "warning"
        items = " · ".join(f"{f['title']} {f['detail']}" for f in rest)
        out += ["", f"{_mark(severity, s)} {s.bold(_count(len(rest), 'more change'))}  {s.dim(items)}"]
        out.append(f"    {s.dim('next')}    {s.cyan(rest[0]['next'][0])}")

    out.append("")
    if summary["queries"]:
        parts = [f"{v['hit']} of {v['total']} {KIND_NAMES.get(k, k)}" for k, v in summary["queries_by_kind"].items()]
        hit = summary["queries_hit"]
        head = (s.red if hit else s.green)(f"{hit} of {summary['queries']} queries")
        out.append(f"{head} read something that changed: {', '.join(parts)}.")
    else:
        out.append(s.dim("Add --queries dashboard.json --queries rules.yaml to see which panels and alerts break."))
    out.append(s.dim("Both captures parse as Prometheus text 0.0.4, so the scrape itself still succeeds."))
    for note in doc["notes"]:
        out.append(s.yellow(f"note: {note}"))
    verdict = s.red("FAIL (exit 1)") if summary["failed"] else s.green("pass (exit 0)")
    counts = f"{_count(summary['errors'], 'error')}, {_count(summary['warnings'], 'warning')}"
    out.append(f"{verdict}: {counts}  {s.dim('--fail-on ' + summary['fail_on'])}")
    return "\n".join(out) + "\n"


def _use_color(args) -> bool:
    if args.no_color or args.format != "text" or os.environ.get("NO_COLOR"):
        return False
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        config = Config.from_file(args.config) if args.config else Config()
        if args.max_growth is not None:
            budget = int(args.max_growth) if args.max_growth.is_integer() else args.max_growth
            config = config.with_overrides({"max_series_growth_percent": budget})
        queries = [query for path in args.queries for query in load_queries(path)]
        doc = build(load_side(args.before), load_side(args.after), queries, config, args.fail_on)
    except (InputError, OSError, UnicodeError, RecursionError) as exc:
        print(f"otelwrench preflight: {exc}", file=sys.stderr)
        return 2
    try:
        if args.emit == "rename-map":
            sys.stdout.write(json.dumps(doc["rename_map"], indent=2, sort_keys=True) + "\n")
            return 0
        if args.emit == "allowlist":
            sys.stdout.write(json.dumps(doc["allowlist"], indent=2, sort_keys=True, ensure_ascii=False) + "\n")
            return 0
        if args.format == "json":
            sys.stdout.write(json.dumps(doc, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
        else:
            sys.stdout.write(render_text(doc, Style(_use_color(args))))
        sys.stdout.flush()
    except BrokenPipeError:
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass
    return 1 if doc["summary"]["failed"] else 0


__all__ = ["main", "parser", "render_text"]

if __name__ == "__main__":
    raise SystemExit(main())
