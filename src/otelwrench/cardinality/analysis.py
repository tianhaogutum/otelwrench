# SPDX-License-Identifier: Apache-2.0
"""Canonical-key projections; no sample aggregation or rewriting."""

from collections import defaultdict
from itertools import chain
import math
import re

from .candidates import (MAX_CANDIDATES, MAX_LABELS, resolve_plan_names,
                        resolve_plan_metrics, validate_candidates, validate_labels)
from .parser import InputError, METRIC, canonical_key

WARNINGS = [
    "Safety is observed-only: no guarantee for future scrapes, other targets, target labels, or existing relabel rules.",
    "Projected keys are hypothetical; no samples are summed, deduplicated, rewritten, or exported.",
    "Empty label values are equivalent to absent labels for identity; explicit empty values remain in label statistics and witnesses.",
    "A collision-free removal preserves observed series count; fewer unique keys caused by merging is unsafe, even for equal values or different timestamps.",
]


def key_object(key):
    metric, labels = key
    return {"metric": metric, "labels": dict(labels)}


def summarize(scrape):
    families = defaultdict(list)
    for sample in scrape.samples:
        families[sample.family].append(sample)
    result = []
    for family, samples in sorted(families.items()):
        values = defaultdict(set)
        present = defaultdict(int)
        empty = defaultdict(int)
        for sample in samples:
            for label, value in sample.labels:
                values[label].add(value)
                present[label] += 1
                empty[label] += value == ""
        result.append({
            "name": family,
            "type": scrape.types.get(family, "untyped"),
            "help": scrape.help.get(family),
            "observed_series": len(samples),
            "metric_names": sorted({sample.metric for sample in samples}),
            "labels": {label: {
                "distinct_value_count": len(distinct),
                "values": sorted(distinct),
                "present_series": present[label],
                "absent_series": len(samples) - present[label],
                "empty_value_series": empty[label],
            } for label, distinct in sorted(values.items())},
        })
    return {
        "observed_series": len(scrape.samples),
        "families": result,
        "declared_metadata": {"types": dict(sorted(scrape.types.items())), "help": dict(sorted(scrape.help.items()))},
    }


def _witness(sample):
    return {
        "original_key": key_object(sample.key),
        "original_labels": dict(sample.labels),
        "value": sample.value,
        "timestamp_ms": sample.timestamp_ms,
        "line": sample.line,
    }


def _validate_plan(samples, drop_labels, metrics):
    samples = tuple(samples)
    labels = validate_labels(drop_labels)
    observed_metrics = {sample.metric for sample in samples}
    selected = set(metrics) if metrics is not None else observed_metrics
    for metric in selected:
        if not re.fullmatch(METRIC, metric) or metric not in observed_metrics:
            raise InputError(f"requested exact metric {metric!r} is invalid or absent")
    if not selected:
        raise InputError("metric scope is empty")
    available = {k for s in samples if s.metric in selected for k, _ in s.labels}
    missing = set(labels) - available
    if missing:
        raise InputError(f"requested label(s) absent from selected metrics: {', '.join(sorted(missing))}")

    return labels, selected


def _project_labels(sample, labels, selected):
    return tuple((k, v) for k, v in sample.labels if k not in labels) if sample.metric in selected else sample.labels


def _value_relation(samples):
    numeric = [float(s.value) for s in samples]
    if any(math.isnan(value) for value in numeric):
        return "indeterminate_nan"
    return "equal" if all(value == numeric[0] for value in numeric) else "different"


def analyze_plan(scrape, drop_labels, metrics=None, name="plan-1"):
    labels, selected = _validate_plan(scrape.samples, drop_labels, metrics)
    return _analyze_validated(scrape, labels, selected, name)


def _analyze_validated(scrape, labels, selected, name):
    groups = defaultdict(list)
    affected = set()
    projected_by_family = defaultdict(set)
    projected_by_metric = defaultdict(set)
    before_by_metric = defaultdict(int)
    label_pairs_before = 0
    label_pairs_after = 0
    for sample in scrape.samples:
        before_by_metric[sample.metric] += 1
        label_pairs_before += len(sample.labels)
        projected_labels = _project_labels(sample, labels, selected)
        if projected_labels != sample.labels:
            affected.add(sample.metric)
        label_pairs_after += len(projected_labels)
        key = canonical_key(sample.metric, projected_labels)
        groups[key].append(sample)
        projected_by_family[sample.family].add(key)
        projected_by_metric[sample.metric].add(key)
    collisions = []
    for key, originals in sorted(groups.items()):
        if len(originals) < 2:
            continue
        collisions.append({
            "projected_key": key_object(key),
            "sample_value_relation": _value_relation(originals),
            "original_series_count": len(originals),
            "originals": [_witness(s) for s in sorted(originals, key=lambda s: s.key)],
        })
    before, after = len(scrape.samples), len(groups)
    return {
        "name": name,
        "drop_labels": list(labels),
        "scope_metric_names": sorted(selected),
        "affected_metric_names": sorted(affected),
        "observed_keys_before": before,
        "projected_keys_after": after,
        "observed_cardinality_delta": after - before,
        "observed_reduction_fraction": (before - after) / before,
        "label_pairs_before": label_pairs_before,
        "label_pairs_after": label_pairs_after,
        "label_pairs_removed": label_pairs_before - label_pairs_after,
        "safe": not collisions,
        "unsafe": bool(collisions),
        "status": "unsafe" if collisions else "observed_safe",
        "collision_group_count": len(collisions),
        "colliding_original_series": sum(c["original_series_count"] for c in collisions),
        "collision_metric_names": sorted({c["projected_key"]["metric"] for c in collisions}),
        "projected_series_by_family": {k: len(v) for k, v in sorted(projected_by_family.items())},
        "metric_counts": {metric: {"before": before_by_metric[metric], "after": len(keys)}
                          for metric, keys in sorted(projected_by_metric.items())},
        "collisions": collisions,
    }


def emit_config(plan):
    """YAML using metric-scoped replace-to-empty rules, never global labeldrop."""
    if plan["unsafe"]:
        raise InputError("unsafe plan: refusing to emit metric_relabel_configs")
    metrics = plan["affected_metric_names"]
    if any(metric == "up" or metric.startswith("scrape_") for metric in metrics):
        raise InputError("config emission for up/scrape_* is unsupported: automatically generated scrape metrics bypass metric relabeling")
    # Legacy metric names contain no regex metacharacters or YAML quote marks.
    regex = "^(?:" + "|".join(metrics) + ")$"
    lines = [
        "# Observed-only candidate; review target labels and existing rules before use.",
        "# Safety does not extend to future samples or other targets.",
        "metric_relabel_configs:",
    ]
    for label in plan["drop_labels"]:
        lines += [
            "  - source_labels: [__name__]",
            f"    regex: '{regex}'",
            "    action: replace",
            f"    target_label: '{label}'",
            "    replacement: ''",
        ]
    return "\n".join(lines) + "\n"


def _rank_safe_plans(results):
    safe = sorted((plan for plan in results if plan["safe"]),
                  key=lambda plan: (-plan["label_pairs_removed"], tuple(plan["drop_labels"])))
    return {
        "limits": {"max_candidates": MAX_CANDIDATES, "max_labels_per_candidate": MAX_LABELS},
        "evaluated_candidates": len(results),
        "excluded_unsafe_candidates": len(results) - len(safe),
        "objective": "label_pairs_removed_descending",
        "tie_break": "drop_labels_lexicographic",
        "ranked_safe_plans": [
            {"rank": rank, "name": plan["name"], "drop_labels": plan["drop_labels"],
             "label_pairs_removed": plan["label_pairs_removed"]}
            for rank, plan in enumerate(safe, 1)
        ],
    }


def report(scrape, plans=(), metrics=None, include_config=False, rank_plans=False, *,
           plan_names=None, plan_metrics=None):
    plans = tuple(tuple(labels) for labels in plans)
    scopes = resolve_plan_metrics(plans, metrics, plan_metrics)
    names = resolve_plan_names(plans, plan_names, plan_metrics)
    if rank_plans:
        if include_config:
            raise InputError("--rank-plans cannot be combined with --emit-config; review an explicit plan separately")
        validate_candidates(plans, "--rank-plans", plan_metrics)
    if include_config and len(plans) != 1:
        raise InputError("--emit-config requires exactly one plan")
    if metrics is not None and not plans:
        raise InputError("--metric requires a plan")
    validated = [_validate_plan(scrape.samples, labels, scope) for labels, scope in zip(plans, scopes)]
    results = [_analyze_validated(scrape, labels, selected, name)
               for (labels, selected), name in zip(validated, names)]
    if include_config:
        plan = results[0]
        if plan["unsafe"]:
            plan["config_refusal"] = "unsafe plan: no metric_relabel_configs emitted"
        else:
            plan["metric_relabel_configs_yaml"] = emit_config(plan)
    result = {
        "schema_version": 1,
        "dry_run": True,
        "input_format": "prometheus-text-0.0.4-subset",
        "warnings": WARNINGS.copy(),
        "summary": summarize(scrape),
        "plans": results,
        "unsafe": any(plan["unsafe"] for plan in results),
    }
    if rank_plans:
        result["candidate_ranking"] = _rank_safe_plans(results)
    return result


def _union_plan(scrapes, labels, selected, per_scrape, name):
    """Project distinct identities while retaining every separate observation."""
    groups = defaultdict(list)
    before_by_metric = defaultdict(set)
    after_by_metric = defaultdict(set)
    for index, item in enumerate(scrapes):
        for sample in item.scrape.samples:
            projected = canonical_key(sample.metric, _project_labels(sample, labels, selected))
            groups[projected].append((index, sample))
            before_by_metric[sample.metric].add(sample.key)
            after_by_metric[sample.metric].add(projected)
    collisions = []
    for key, observations in sorted(groups.items()):
        originals = {sample.key for _, sample in observations}
        if len(originals) < 2:
            continue
        by_scrape = defaultdict(set)
        witnesses = []
        for index, sample in sorted(observations, key=lambda pair: (pair[0], pair[1].key)):
            by_scrape[index].add(sample.key)
            item = scrapes[index]
            witnesses.append({**_witness(sample), "scrape_id": item.id,
                              "source": item.source, "captured_at": item.captured_at})
        collisions.append({
            "projected_key": key_object(key),
            "classification": "within_scrape" if any(len(keys) > 1 for keys in by_scrape.values()) else "cross_scrape_only",
            "original_series_count": len(originals),
            "sample_observation_count": len(observations),
            "sample_value_relation": _value_relation([sample for _, sample in observations]),
            "originals": witnesses,
        })
    before = sum(len(keys) for keys in before_by_metric.values())
    after = len(groups)
    cross_count = sum(c["classification"] == "cross_scrape_only" for c in collisions)
    all_safe = all(plan["safe"] for plan in per_scrape)
    return {
        "name": name,
        "drop_labels": list(labels),
        "scope_metric_names": sorted(selected),
        "affected_metric_names": sorted({m for plan in per_scrape for m in plan["affected_metric_names"]}),
        "observed_keys_before": before,
        "projected_keys_after": after,
        "observed_cardinality_delta": after - before,
        "observed_reduction_fraction": (before - after) / before,
        "label_pairs_before": sum(plan["label_pairs_before"] for plan in per_scrape),
        "label_pairs_after": sum(plan["label_pairs_after"] for plan in per_scrape),
        "label_pairs_removed": sum(plan["label_pairs_removed"] for plan in per_scrape),
        "safe": not collisions,
        "unsafe": bool(collisions),
        "status": "unsafe_cross_scrape" if collisions and all_safe else "unsafe" if collisions else "observed_safe",
        "all_scrapes_safe": all_safe,
        "collision_group_count": len(collisions),
        "cross_scrape_collision_group_count": cross_count,
        "colliding_original_series": sum(c["original_series_count"] for c in collisions),
        "collision_metric_names": sorted({c["projected_key"]["metric"] for c in collisions}),
        "metric_counts": {metric: {"before": len(keys), "after": len(after_by_metric[metric])}
                          for metric, keys in sorted(before_by_metric.items())},
        "collisions": collisions,
    }


def report_scrapes(scrapes, plans=(), metrics=None, include_config=False, rank_plans=False, *,
                   plan_names=None, plan_metrics=None, required_target_labels=(), protected_target_labels=()):
    """Compare plans per scrape and over the observed identity union, never values.

    A label or selected metric may be absent in one scrape, but must occur in the
    selected union. Source/capture metadata is provenance, not an implicit label.
    Optional required_target_labels must be nonempty on every effective sample,
    even outside plan scopes; context and exposition can both supply coverage.
    Protected labels additionally forbid removal by any candidate, regardless of
    scope; protection implies the same nonempty pre-drop coverage requirement.
    """
    from .scrapes import (contextualize, validate_required_target_labels,
                          validate_scrapes, validate_target_label_names)

    scrapes = tuple(scrapes)
    plans = tuple(tuple(labels) for labels in plans)
    validate_scrapes(scrapes)
    # Keep decoded exposition evidence separate from effective pre-drop labels.
    exposition = {item.id: {sample.line: dict(sample.labels) for sample in item.scrape.samples}
                  for item in scrapes if item.target_labels}
    scrapes = tuple(contextualize(item) for item in scrapes)
    required = validate_target_label_names(required_target_labels)
    protected = validate_target_label_names(protected_target_labels, "protected")
    validate_required_target_labels(scrapes, tuple(sorted(set(required) | set(protected))))
    scopes = resolve_plan_metrics(plans, metrics, plan_metrics)
    names = resolve_plan_names(plans, plan_names, plan_metrics)
    if include_config:
        raise InputError("--emit-config is unsupported with --scrape-set; review a single scrape separately")
    if rank_plans:
        validate_candidates(plans, "--rank-plans", plan_metrics)
    if metrics is not None and not plans:
        raise InputError("--metric requires a plan")
    # Validate all candidates against all observations before producing a report.
    validated = [_validate_plan(chain.from_iterable(item.scrape.samples for item in scrapes), labels, scope)
                 for labels, scope in zip(plans, scopes)]
    for index, ((labels, selected), name) in enumerate(zip(validated, names), 1):
        forbidden = sorted(set(labels) & set(protected))
        if forbidden:
            label = forbidden[0]
            raise InputError(
                f"candidate {name!r}: cannot remove protected target label {label!r}",
                details={"code": "protected_target_label_removal", "candidate_name": name,
                         "candidate_index": index, "label": label,
                         "scope_metric_names": sorted(selected)},
            )
    entries = [{**item.identity(), "summary": summarize(item.scrape), "plans": []} for item in scrapes]
    results = []
    for (labels, selected), name in zip(validated, names):
        per_scrape = [_analyze_validated(item.scrape, labels, selected, name) for item in scrapes]
        for entry, plan in zip(entries, per_scrape):
            for collision in plan["collisions"]:
                for witness in collision["originals"]:
                    witness.update(scrape_id=entry["id"], source=entry["source"], captured_at=entry["captured_at"])
            entry["plans"].append(plan)
        results.append(_union_plan(scrapes, labels, selected, per_scrape, name))
    contexts = {item.id: dict(item.target_labels) for item in scrapes if item.target_labels}
    for plan in chain(results, chain.from_iterable(entry["plans"] for entry in entries)):
        for collision in plan["collisions"]:
            for witness in collision["originals"]:
                scrape_id = witness["scrape_id"]
                if scrape_id in contexts:
                    witness["target_labels"] = contexts[scrape_id].copy()
                    witness["exposition_labels"] = exposition[scrape_id][witness["line"]].copy()
    result = {
        "schema_version": 2,
        "dry_run": True,
        "input_format": "prometheus-text-0.0.4-scrape-set-v1",
        "required_target_labels": list(required),
        "protected_target_labels": list(protected),
        "warnings": WARNINGS + [
            "Scrape source/id/captured_at are provenance only, not series labels. Explicit target_labels are merged for analysis; conflicting exposition values and induced duplicate identities are rejected.",
            "Union keys count distinct canonical original identities; repeated observations are not collisions. Label-pair counts and ranking scores count every sample observation.",
        ],
        "summary": {
            "scrape_count": len(scrapes),
            "sample_observations": sum(len(item.scrape.samples) for item in scrapes),
            "observed_series": len({sample.key for item in scrapes for sample in item.scrape.samples}),
        },
        "scrapes": entries,
        "plans": results,
        "unsafe": any(plan["unsafe"] for plan in results),
    }
    if rank_plans:
        result["candidate_ranking"] = _rank_safe_plans(results)
        result["candidate_ranking"]["scoring_scope"] = "sample_observations_across_scrapes"
    return result
