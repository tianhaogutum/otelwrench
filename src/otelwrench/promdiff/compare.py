"""Deterministic contracts, policy evaluation and exact expected changes."""
from __future__ import annotations

from fractions import Fraction
import json
import re

from .config import Config, DEFAULT_POLICY, _JsonGrowthLimit
from .parser import InputError, NAME, Snapshot
from .renames import project_baseline, validate_rename_map


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def validate_allowlist(data: object) -> list[dict]:
    if not isinstance(data, list):
        raise InputError("allowlist must be a JSON array of exact change objects")
    seen = set()
    for item in data:
        if not isinstance(item, dict) or set(item) != {"kind", "family", "before", "after"}:
            raise InputError("each allowlist entry requires exactly kind, family, before, after")
        if not isinstance(item["kind"], str) or item["kind"] not in DEFAULT_POLICY:
            raise InputError("unknown allowlist change kind")
        family = item["family"]
        if item["kind"] == "series_growth":
            if family is not None:
                raise InputError("series_growth allowlist family must be null (snapshot-wide gate)")
        elif not isinstance(family, str) or re.fullmatch(NAME, family) is None:
            raise InputError("allowlist family must be an exact legacy metric name; no wildcards")
        try:
            key = canonical(item)
        except (ValueError, TypeError) as exc:
            raise InputError("allowlist entries must contain finite JSON data") from exc
        if key in seen:
            raise InputError("duplicate allowlist entry")
        seen.add(key)
    return data


def _observed_diff(baseline: Snapshot, candidate: Snapshot, config: Config) -> tuple[list[dict], dict]:
    """Collect sorted observations without policy or allowlist decoration."""
    changes: list[dict] = []

    def change(kind, family, before, after):
        changes.append({"kind": kind, "family": family, "before": before, "after": after})

    family_stats = []
    left, right = baseline.families, candidate.families
    for name in sorted(left.keys() | right.keys()):
        old, new = left.get(name), right.get(name)
        old_series = old.series if old else set()
        new_series = new.series if new else set()
        added, missing = len(new_series - old_series), len(old_series - new_series)
        family_stats.append({
            "family": name, "baseline_present": old is not None, "candidate_present": new is not None,
            "baseline": len(old_series), "candidate": len(new_series), "new": added, "missing": missing,
        })
        if old is None:
            change("family_added", name, None, new.contract())
        elif new is None:
            change("family_removed", name, old.contract(), None)
        else:
            if old.type != new.type:
                change("type_changed", name, old.type, new.type)
            if old.help != new.help:
                change("help_changed", name, old.help, new.help)
            if old.schema() != new.schema():
                change("label_schema_changed", name, old.schema(), new.schema())
            if added:
                change("series_added", name, 0, added)
            if missing:
                change("series_missing", name, missing, 0)
    before, after = len(baseline.series), len(candidate.series)
    # Interpret the configured decimal spelling exactly. Rational arithmetic
    # and half-even display rounding never depend on the caller's Decimal
    # context. Zero->positive exceeds any limit; display rounding is not a gate.
    configured = config.max_series_growth_percent
    limit = configured.ratio if isinstance(configured, _JsonGrowthLimit) else Fraction(str(configured))
    growth = Fraction(max(0, after - before) * 100, before) if before else (Fraction(0) if not after else None)
    exceeds = after > 0 if before == 0 else growth > (limit if limit is not None else 0)
    if exceeds:
        change("series_growth", None, before, after)
    changes.sort(key=lambda event: (event["family"] or "", event["kind"], canonical(event)))
    stats = {
        "baseline": before, "candidate": after,
        "new": len(candidate.series - baseline.series),
        "missing": len(baseline.series - candidate.series),
        "net_growth_percent": float(round(growth, 6)) if growth is not None else None,
        "zero_baseline_growth": before == 0 and after > 0,
        "max_growth_percent": configured.display if isinstance(configured, _JsonGrowthLimit) else configured,
        "growth_limit_exceeded": exceeds,
        "by_family": family_stats,
    }
    if isinstance(configured, _JsonGrowthLimit):
        stats["max_growth_percent_exact"] = configured.token
    return changes, stats


def compare(baseline: Snapshot, candidate: Snapshot, config: Config | None = None,
            allowlist: list[dict] | None = None, *, rename_map: dict | None = None,
            include_raw_changes: bool = False) -> dict:
    """Gate on projected changes; optionally attach unprojected wire evidence.

    raw_changes contains only kind/family/before/after. These observations never
    consume allowlist entries or contribute to the gate's summary or status.
    """
    config = config or Config()
    # Validate even library callers' explicit Config instances.
    config = Config.from_dict({"policy": config.policy,
                               "max_series_growth_percent": config.max_series_growth_percent})
    allowlist = validate_allowlist([] if allowlist is None else allowlist)
    original_baseline = baseline
    if rename_map is not None:
        rename_map = validate_rename_map(rename_map)
        baseline = project_baseline(baseline, candidate, rename_map)
    changes, observed_series = _observed_diff(baseline, candidate, config)
    left, right = baseline.families, candidate.families
    expected = {canonical(item) for item in allowlist}
    used = set()
    for event in changes:
        key = canonical(event)
        event["severity"] = config.policy[event["kind"]]
        event["allowed"] = key in expected
        if event["allowed"]:
            used.add(key)
    if expected - used:
        raise InputError(f"{len(expected - used)} unused allowlist entry/entries: expected changes must match exactly")
    failures = sum(not e["allowed"] and e["severity"] == "fail" for e in changes)
    warnings = sum(not e["allowed"] and e["severity"] == "warn" for e in changes)
    report = {
        "report_version": 1,
        "status": "fail" if failures else ("warn" if warnings else "pass"),
        "baseline": baseline.summary(),
        "candidate": candidate.summary(),
        "contracts": {
            "baseline": {name: original_baseline.families[name].contract()
                         for name in sorted(original_baseline.families)},
            "candidate": {name: right[name].contract() for name in sorted(right)},
        },
        "observed_series": observed_series,
        "policy": dict(sorted(config.policy.items())),
        "changes": changes,
        "summary": {"failures": failures, "warnings": warnings, "allowed": len(used)},
    }
    if rename_map is not None:
        report["declared_renames"] = rename_map
        report["contracts"]["comparison_baseline"] = {name: left[name].contract() for name in sorted(left)}
    if include_raw_changes:
        report["raw_changes"], _ = _observed_diff(original_baseline, candidate, config)
    return report
