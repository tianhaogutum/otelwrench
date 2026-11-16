# SPDX-License-Identifier: Apache-2.0
"""Strict named candidate-file v1/v2 input and shared candidate bounds."""

from dataclasses import dataclass
import json
from pathlib import Path
import re

from .parser import InputError, LABEL, METRIC

MAX_CANDIDATES = 64
MAX_LABELS = 8


@dataclass(frozen=True)
class Candidate:
    name: str
    drop_labels: tuple[str, ...]
    metrics: tuple[str, ...] | None = None


def validate_metrics(metrics):
    if (not isinstance(metrics, (list, tuple)) or not metrics
            or any(not isinstance(metric, str) or not re.fullmatch(METRIC, metric)
                   for metric in metrics)):
        raise InputError("candidate metrics must be a nonempty array of exact sample metric-name strings")
    if len(metrics) != len(set(metrics)):
        raise InputError("candidate metrics must contain distinct exact sample names")
    return tuple(sorted(metrics))


def resolve_plan_metrics(plans, metrics, plan_metrics):
    if plan_metrics is None:
        return (metrics,) * len(plans)
    if metrics is not None:
        raise InputError("--metric cannot be combined with per-candidate metrics (candidate-file v2)")
    if not isinstance(plan_metrics, (list, tuple)) or len(plan_metrics) != len(plans):
        raise InputError("plan_metrics requires exactly one metric scope per candidate")
    scopes = tuple(validate_metrics(scope) for scope in plan_metrics)
    validate_candidates(plans, "scoped candidates", scopes)
    return scopes


def validate_labels(drop_labels):
    drop_labels = tuple(drop_labels)
    if not drop_labels or any(not isinstance(label, str) for label in drop_labels):
        raise InputError("a plan must contain a nonempty array of label-name strings")
    labels = tuple(sorted(drop_labels))
    if len(labels) != len(set(labels)):
        raise InputError("a plan must contain at least one label with no repeated labels")
    for label in labels:
        if not re.fullmatch(LABEL, label):
            raise InputError(f"invalid label name {label!r}")
        if label in {"le", "quantile"} or label.startswith("__"):
            raise InputError(f"protected label {label!r} cannot be removed")
    return labels


def validate_candidates(plans, context, plan_metrics=None):
    """Bound explicit candidates before projection; never truncate or deduplicate."""
    if not plans:
        raise InputError(f"{context} requires at least one candidate")
    if len(plans) > MAX_CANDIDATES:
        raise InputError(f"{context} allows at most {MAX_CANDIDATES} candidates")
    seen = set()
    for index, labels in enumerate(plans):
        if len(labels) > MAX_LABELS:
            raise InputError(f"{context} allows at most {MAX_LABELS} labels per candidate")
        labels_key = validate_labels(labels)
        key = labels_key if plan_metrics is None else (labels_key, validate_metrics(plan_metrics[index]))
        if key in seen:
            raise InputError(f"duplicate candidate combination: {key!r}")
        seen.add(key)


def resolve_plan_names(plans, names, plan_metrics=None):
    if names is None:
        return tuple(f"plan-{i}" for i in range(1, len(plans) + 1))
    if isinstance(names, str) or len(names) != len(plans):
        raise InputError("plan_names requires exactly one name per candidate")
    seen = set()
    for name in names:
        if (not isinstance(name, str) or not 1 <= len(name) <= 64
                or not name.isprintable() or name != name.strip()):
            raise InputError("candidate name must be 1–64 printable characters without surrounding whitespace")
        if name in seen:
            raise InputError(f"duplicate candidate name {name!r}")
        seen.add(name)
    validate_candidates(plans, "named candidates", plan_metrics)
    return tuple(names)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputError(f"duplicate JSON field {key!r}")
        result[key] = value
    return result


def _reject_constant(value):
    raise InputError(f"nonstandard JSON constant {value!r}")


def load_candidates(path):
    """Load all candidates or fail; labels' existence is checked against scrapes."""
    if str(path) == "-":
        raise InputError("candidate-file must name a local file; stdin is unsupported")
    try:
        data = json.loads(Path(path).read_bytes().decode("utf-8"),
                          object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as exc:
        raise InputError(f"invalid candidate-file JSON: {exc}") from exc
    if (not isinstance(data, dict) or set(data) != {"schema_version", "candidates"}
            or type(data["schema_version"]) is not int or data["schema_version"] not in (1, 2)):
        raise InputError("candidate-file requires only schema_version: 1 or 2 and candidates")
    scoped = data["schema_version"] == 2
    entries = data["candidates"]
    if not isinstance(entries, list) or not entries:
        raise InputError("candidates must be a nonempty ordered array")
    if len(entries) > MAX_CANDIDATES:
        raise InputError(f"candidate-file allows at most {MAX_CANDIDATES} candidates")
    for entry in entries:
        fields = {"name", "drop_labels", "metrics"} if scoped else {"name", "drop_labels"}
        if not isinstance(entry, dict) or set(entry) != fields:
            raise InputError("each v2 candidate requires only name, drop_labels and metrics" if scoped
                             else "each v1 candidate requires only name and drop_labels")
        if not isinstance(entry["drop_labels"], list):
            raise InputError("candidate drop_labels must be an array of label-name strings")
        if scoped and not isinstance(entry["metrics"], list):
            raise InputError("candidate metrics must be an array of exact sample metric-name strings")
    labels = [entry["drop_labels"] for entry in entries]
    names = [entry["name"] for entry in entries]
    scopes = [entry["metrics"] for entry in entries] if scoped else None
    resolve_plan_metrics(labels, None, scopes)
    resolve_plan_names(labels, names, scopes)
    return [Candidate(entry["name"], tuple(entry["drop_labels"]),
                      tuple(entry["metrics"]) if scoped else None) for entry in entries]
