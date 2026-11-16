"""Combine promdiff, prom-names and cardinality into one list of findings.

Every rule comes from those commands' libraries:

- ``promdiff.compare`` decides what changed and how severe it is (its policy and
  growth budget), after projecting the renames below with ``--rename-map`` semantics.
- ``promnames.core.metric_name`` is the OTLP-to-Prometheus naming rule. A removed
  family and an added family of the same type count as a rename only when that rule
  turns one name into the other (unit suffix, ``_total``, or exact ms-to-s renaming).
- ``cardinality.analysis.summarize`` gives per-label distinct values; it confirms a
  label rename (identical value sets) and names the label behind series growth.
  ``cardinality.analysis.analyze_plan`` then checks whether dropping that label by
  relabeling would merge series.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import shlex

from ..cardinality.analysis import analyze_plan, summarize
from ..cardinality.parser import InputError as ScrapeError, parse as parse_scrape
from ..promdiff._files import MAX_INPUT_BYTES, read_text
from ..promdiff.compare import compare
from ..promdiff.config import Config
from ..promdiff.parser import InputError, Snapshot, parse_snapshot
from ..promdiff.renames import validate_rename_map
from ..promnames.core import UNITS, WITH, WITHOUT, ConversionError, metric_name
from . import SCHEMA, __version__
from .queries import Query, identifiers

COMPOSITE_LABELS = {"le", "quantile"}
SEVERITY = {"fail": "error", "warn": "warning"}
RANK = {"none": 3, "error": 2, "warning": 1}
ORDER = ["renamed", "label_renamed", "family_removed", "type_changed", "label_schema_changed",
         "series_growth", "family_added", "help_changed", "series_added", "series_missing"]
SOURCE = {"renamed": "prom-names", "label_renamed": "promdiff + cardinality", "series_growth": "promdiff + cardinality"}


@dataclass
class Side:
    path: str
    snapshot: Snapshot
    scrape: object | None
    families: dict[str, dict]  # cardinality summarize() rows by family name
    note: str | None = None


def load_side(path: str) -> Side:
    """Parse one exposition with both parsers; promdiff's strict parse must succeed."""
    text = read_text(path, max_bytes=MAX_INPUT_BYTES)
    snapshot = parse_snapshot(text, path)
    try:
        scrape = parse_scrape(text)
    except ScrapeError as exc:
        return Side(path, snapshot, None, {}, f"{path}: cardinality checks skipped: {exc}")
    return Side(path, snapshot, scrape, {row["name"]: row for row in summarize(scrape)["families"]})


def explain_rename(old: str, new: str, counter: bool) -> dict | None:
    """The prom-names naming rule that maps one family name onto the other, if any."""
    for unit in UNITS:
        for rescaled in ((False, True) if unit == "s" else (False,)):
            try:
                name, steps = metric_name(old, unit, counter, WITH, convert_milliseconds=rescaled)
            except ConversionError:
                continue
            if name == new and steps:
                return {"direction": "suffixes_added", "strategy": WITH, "otlp_unit": unit,
                        "rescaled": rescaled, "steps": steps}
    for unit in UNITS:
        try:
            name, steps = metric_name(new, unit, counter, WITH)
        except ConversionError:
            continue
        if name == old and steps:
            return {"direction": "suffixes_removed", "strategy": WITHOUT, "otlp_unit": unit,
                    "rescaled": False, "steps": steps}
    return None


def describe_rename(why: dict) -> str:
    if why["direction"] == "suffixes_removed":
        return f"suffixes dropped ({WITHOUT})"
    parts = []
    for step in why["steps"]:
        if step["operation"] == "unit_suffix":
            parts.append(f"OTLP unit \"{step['unit']}\" suffix")
        elif step["operation"] == "counter_suffix":
            parts.append("counter _total suffix")
        elif step["operation"] == "converted_unit_name":
            parts.append("ms to s: values x0.001")
    return ", ".join(parts) or "OTLP name normalization"


def _labels(family) -> set[str]:
    return {key for schema in family.schemas.values() for key in schema} - COMPOSITE_LABELS


def _wire_names(family) -> set[str]:
    return {family.name, *family.schemas}


def infer_metric_renames(before: Snapshot, after: Snapshot) -> list[tuple[str, str, dict]]:
    removed = sorted(before.families.keys() - after.families.keys())
    added = sorted(after.families.keys() - before.families.keys())
    pairs = []
    for old in removed:
        for new in added:
            if before.families[old].type != after.families[new].type:
                continue
            why = explain_rename(old, new, before.families[old].type == "counter")
            if why:
                pairs.append((old, new, why))
    olds, news = Counter(p[0] for p in pairs), Counter(p[1] for p in pairs)
    return [p for p in pairs if olds[p[0]] == 1 and news[p[1]] == 1]


def infer_label_renames(before: Side, after: Side, metrics: dict[str, str]) -> list[tuple[str, str, str, int]]:
    """One label out, one label in, with exactly the same observed values: a rename."""
    found = []
    for name, family in sorted(before.snapshot.families.items()):
        target = metrics.get(name, name)
        other = after.snapshot.families.get(target)
        if other is None:
            continue
        gone, new = _labels(family) - _labels(other), _labels(other) - _labels(family)
        if len(gone) != 1 or len(new) != 1:
            continue
        (old_label,), (new_label,) = gone, new
        left = before.families.get(name, {}).get("labels", {}).get(old_label, {}).get("values")
        right = after.families.get(target, {}).get("labels", {}).get(new_label, {}).get("values")
        if left and left == right:
            found.append((name, old_label, new_label, len(left)))
    return found


def _growth_drivers(report: dict, before: Side, after: Side, reverse: dict[str, str],
                    label_reverse: dict[str, dict[str, str]], limit: int = 3) -> list[dict]:
    rows = [row for row in report["observed_series"]["by_family"] if row["candidate"] > row["baseline"]]
    rows.sort(key=lambda row: (row["baseline"] - row["candidate"], row["family"]))
    drivers = []
    for row in rows[:limit]:
        name = row["family"]
        old_name = reverse.get(name, name)
        entry = {"family": name, "series_before": row["baseline"], "series_after": row["candidate"],
                 "label": None, "values_before": None, "values_after": None, "metrics": [], "drop_check": None}
        drivers.append(entry)
        now, then = after.families.get(name), before.families.get(old_name, {"labels": {}})
        if now is None:
            continue
        deltas = []
        for label, stats in now["labels"].items():
            if label in COMPOSITE_LABELS:
                continue
            old_label = label_reverse.get(old_name, {}).get(label, label)
            was = then["labels"].get(old_label, {}).get("distinct_value_count", 0)
            deltas.append((stats["distinct_value_count"] - was, label, was, stats["distinct_value_count"]))
        deltas.sort(key=lambda d: (-d[0], d[1]))
        if not deltas or deltas[0][0] <= 0:
            continue
        _, label, was, now_count = deltas[0]
        entry.update(label=label, values_before=was, values_after=now_count, metrics=now["metric_names"])
        if after.scrape is None:
            continue
        try:
            plan = analyze_plan(after.scrape, [label], metrics=now["metric_names"])
        except ScrapeError:
            continue
        counts = plan["metric_counts"]
        entry["drop_check"] = {
            "safe": plan["safe"],
            "series_before": sum(counts[m]["before"] for m in now["metric_names"] if m in counts),
            "series_after": sum(counts[m]["after"] for m in now["metric_names"] if m in counts),
            "collision_groups": plan["collision_group_count"],
        }
    return drivers


def _commands(before: str, after: str) -> dict[str, str]:
    b, a = shlex.quote(before), shlex.quote(after)
    return {"preflight": f"otelwrench preflight {b} {a}", "promdiff": f"otelwrench promdiff {b} {a}",
            "after": a}


def _fix(kind: str, data: dict) -> str:
    if kind == "renamed":
        why = data["why"]
        if why["rescaled"]:
            return ("Values are now seconds, not milliseconds: rename the series in queries and scale "
                    "thresholds by 0.001 (500 becomes 0.5).")
        if why["direction"] == "suffixes_removed":
            return f"Update queries to the new name, or turn suffixes back on in the exporter ({WITH})."
        return f"Update queries to the new name, or turn suffixes off in the exporter ({WITHOUT})."
    if kind == "label_renamed":
        return f"Update by(...) and selector uses of {data['old_label']} to {data['new_label']}."
    if kind == "family_removed":
        return "Restore it in the exporter, or delete the panels and alerts that read it and accept the change."
    if kind == "type_changed":
        if (data["before"], data["after"]) == ("counter", "gauge"):
            return "rate()/increase() over a gauge is meaningless: graph the value or use *_over_time()."
        if (data["before"], data["after"]) == ("gauge", "counter"):
            return "It only goes up now: graph rate() or increase() instead of the raw value."
        return "Functions that depend on the metric type (rate, histogram_quantile) need review."
    if kind == "label_schema_changed":
        parts = []
        if data["removed_labels"]:
            parts.append(f"Queries that group or filter by {', '.join(data['removed_labels'])} break.")
        if data["added_labels"]:
            parts.append(f"New label {', '.join(data['added_labels'])}: check joins, without(...) and recording rules.")
        return " ".join(parts)
    if kind == "series_growth":
        return "Find the label behind the growth before it reaches your series limits."
    return "Not breaking under the current policy; review the full list."


def build(before: Side, after: Side, queries: list[Query], config: Config, fail_on: str) -> dict:
    notes = [side.note for side in (before, after) if side.note]
    renames = infer_metric_renames(before.snapshot, after.snapshot)
    metric_map = {old: new for old, new, _ in renames}
    label_renames = infer_label_renames(before, after, metric_map) if before.scrape and after.scrape else []
    rename_map = validate_rename_map({
        "metrics": metric_map,
        "labels": {family: {old: new} for family, old, new, _ in label_renames},
    })
    try:
        report = compare(before.snapshot, after.snapshot, config, rename_map=rename_map)
    except InputError as exc:  # a projection conflict: fall back to the undeclared comparison
        notes.append(f"rename projection skipped: {exc}")
        rename_map = {"metrics": {}, "labels": {}}
        report = compare(before.snapshot, after.snapshot, config)
    reverse = {new: old for old, new in rename_map["metrics"].items()}
    label_reverse = {family: {new: old for old, new in mapping.items()}
                     for family, mapping in rename_map["labels"].items()}
    cmd = _commands(before.path, after.path)
    has_renames = bool(rename_map["metrics"] or rename_map["labels"])
    gate = f"{cmd['promdiff']} --rename-map renames.json" if has_renames else cmd["promdiff"]
    rename_next = [f"{cmd['preflight']} --emit rename-map > renames.json",
                   f"{cmd['promdiff']} --rename-map renames.json"]
    accept_next = [f"{cmd['preflight']} --emit allowlist > allowlist.json",
                   f"{gate} --allowlist allowlist.json"]

    findings: list[dict] = []

    def add(kind, severity, family, title, detail, data, triggers, labels=None, next_=()):
        findings.append({"kind": kind, "severity": severity, "source": SOURCE.get(kind, "promdiff"),
                         "family": family, "title": title, "detail": detail, "fix": _fix(kind, data),
                         "next": list(next_), "_triggers": (triggers, labels), **{k: v for k, v in data.items()
                                                                                  if k != "why"}})

    for old, new, why in renames:
        family = before.snapshot.families[old]
        add("renamed", "error", old, f"{old} -> {new}", f"{family.type}, {describe_rename(why)}",
            {"why": why, "renamed_to": new, "rescaled": why["rescaled"], "otlp_unit": why["otlp_unit"],
             "strategy": why["strategy"], "steps": why["steps"]},
            _wire_names(family), next_=rename_next)
    for family_name, old_label, new_label, count in label_renames:
        family = before.snapshot.families[family_name]
        add("label_renamed", "error", family_name, f"{family_name}: {old_label} -> {new_label}",
            f"same {count} observed value{'s' if count != 1 else ''}",
            {"old_label": old_label, "new_label": new_label}, _wire_names(family), {old_label},
            next_=rename_next)

    growth = None
    for change in report["changes"]:
        if change["allowed"]:
            continue
        kind, name = change["kind"], change["family"]
        severity = SEVERITY[change["severity"]]
        old_family = before.snapshot.families.get(reverse.get(name, name)) if name else None
        if kind == "family_removed":
            add(kind, severity, name, name, f"{old_family.type}, {len(old_family.series)} series", {},
                _wire_names(old_family), next_=accept_next)
        elif kind == "type_changed":
            add(kind, severity, name, f"{name}: {change['before']} -> {change['after']}", "",
                {"before": change["before"], "after": change["after"]}, _wire_names(old_family),
                next_=accept_next)
        elif kind == "label_schema_changed":
            was = {k for s in change["before"] for k in s["label_keys"]} - COMPOSITE_LABELS
            now = {k for s in change["after"] for k in s["label_keys"]} - COMPOSITE_LABELS
            removed, added = sorted(was - now), sorted(now - was)
            parts = [f"-{k}" for k in removed] + [f"+{k}" for k in added]
            add(kind, severity, name, f"{name}: {' '.join(parts) or 'sample layout changed'}", "",
                {"removed_labels": removed, "added_labels": added}, _wire_names(old_family) if removed else set(),
                set(removed), next_=accept_next)
        elif kind == "series_growth":
            growth = change
        else:
            detail = {"family_added": "new family", "help_changed": "HELP text changed",
                      "series_added": f"{change['after']} new series",
                      "series_missing": f"{change['before']} series gone"}[kind]
            add(kind, severity, name, name or "(snapshot)", detail, {}, set(), next_=[gate])
    stats = report["observed_series"]
    if growth is not None:
        drivers = _growth_drivers(report, before, after, reverse, label_reverse)
        pct = stats["net_growth_percent"]
        title = (f"{stats['baseline']} -> {stats['candidate']} series"
                 + (f" (+{pct:.1f}%)" if pct is not None else "") + f", budget {stats['max_growth_percent']}%")
        commands = []
        for d in drivers:
            if d["label"]:
                metric_flags = " ".join(f"--metric {shlex.quote(m)}" for m in d["metrics"])
                safe = d["drop_check"] and d["drop_check"]["safe"]
                commands.append(f"otelwrench cardinality {cmd['after']} --drop-labels {shlex.quote(d['label'])} "
                                f"{metric_flags}" + (" --emit-config" if safe else ""))
        severity = SEVERITY[growth["severity"]]
        add("series_growth", severity, None, title, "", {"drivers": drivers, "growth_percent": pct,
                                                          "budget_percent": stats["max_growth_percent"]},
            set(), next_=commands or [gate])

    findings.sort(key=lambda f: (ORDER.index(f["kind"]), -RANK[f["severity"]], f["family"] or ""))
    for number, finding in enumerate(findings, 1):
        finding["id"] = number

    results = []
    for query in queries:
        names = identifiers(query.expr)
        hits = []
        for finding in findings:
            triggers, labels = finding["_triggers"]
            if triggers & names and (labels is None or labels & names):
                hits.append(finding["id"])
                finding.setdefault("queries", []).append(query.name)
        results.append({**query.record(), "hit": bool(hits), "findings": hits})
    for finding in findings:
        del finding["_triggers"]
        finding.setdefault("queries", [])

    errors = sum(f["severity"] == "error" for f in findings)
    warnings = sum(f["severity"] == "warning" for f in findings)
    worst = max((RANK[f["severity"]] for f in findings), default=0)
    by_kind: dict[str, list[int]] = {}
    for result in results:
        total = by_kind.setdefault(result["kind"], [0, 0])
        total[0] += result["hit"]
        total[1] += 1
    summary = {
        "series_before": stats["baseline"], "series_after": stats["candidate"],
        "growth_percent": stats["net_growth_percent"], "growth_budget_percent": stats["max_growth_percent"],
        "families_before": len(before.snapshot.families), "families_after": len(after.snapshot.families),
        "errors": errors, "warnings": warnings, "findings": len(findings),
        "queries": len(results), "queries_hit": sum(r["hit"] for r in results),
        "queries_by_kind": {kind: {"hit": hit, "total": total} for kind, (hit, total) in sorted(by_kind.items())},
        "fail_on": fail_on, "failed": worst >= RANK[fail_on],
        "promdiff_status": report["status"],
    }
    allowlist = [{key: change[key] for key in ("kind", "family", "before", "after")}
                 for change in report["changes"] if not change["allowed"]]
    return {"schema": SCHEMA, "otelwrench_version": __version__, "before": before.path, "after": after.path,
            "summary": summary, "findings": findings, "queries": results, "rename_map": rename_map,
            "allowlist": allowlist, "next": {"rename_map": rename_next if has_renames else [],
                                             "accept": accept_next},
            "notes": notes}


__all__ = ["Side", "build", "describe_rename", "explain_rename", "infer_label_renames",
           "infer_metric_renames", "load_side"]
