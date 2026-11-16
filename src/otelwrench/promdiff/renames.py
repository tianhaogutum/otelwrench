"""User-declared, simultaneous baseline projections; never rename inference."""
from __future__ import annotations

import re

from .parser import Family, InputError, LABEL, NAME, Snapshot


def validate_rename_map(data: object) -> dict:
    if not isinstance(data, dict) or set(data) - {"metrics", "labels"}:
        raise InputError("rename map must be an object with only metrics/labels")

    def name(value, pattern):
        return isinstance(value, str) and re.fullmatch(pattern, value) is not None

    def pairs(value, pattern, context):
        if not isinstance(value, dict):
            raise InputError(f"{context} must be an object of source-to-target names")
        for old, new in value.items():
            if not name(old, pattern) or not name(new, pattern):
                raise InputError(f"{context} requires exact legacy names")
            if pattern == LABEL and (old.startswith("__") or new.startswith("__")):
                raise InputError(f"{context} cannot use reserved label names")
            if old == new:
                raise InputError(f"{context}: identity mappings are not renames")
        if len(set(value.values())) != len(value):
            raise InputError(f"{context} must be one-to-one")
        return dict(sorted(value.items()))

    metrics = pairs(data.get("metrics", {}), NAME, "rename map.metrics")
    labels = data.get("labels", {})
    if not isinstance(labels, dict):
        raise InputError("rename map.labels must be an object keyed by baseline family name")
    normalized_labels = {}
    for family, mapping in labels.items():
        if not name(family, NAME):
            raise InputError("rename map.labels requires exact baseline family names")
        normalized_labels[family] = pairs(mapping, LABEL, f"rename map.labels.{family}")
    return {"metrics": metrics, "labels": dict(sorted(normalized_labels.items()))}


def _namespace(snapshot: Snapshot) -> dict[str, str]:
    """Reserve composite components even for metadata-only families."""
    owners = {}
    for name, family in sorted(snapshot.families.items()):
        names = {name}
        if family.type == "histogram":
            names.update(name + suffix for suffix in ("_bucket", "_sum", "_count"))
        elif family.type == "summary":
            names.update(name + suffix for suffix in ("_sum", "_count"))
        for wire_name in sorted(names):
            if wire_name in owners and owners[wire_name] != name:
                raise InputError(f"rename map: metric/reserved suffix collision for {wire_name}")
            owners[wire_name] = name
    return owners


def project_baseline(baseline: Snapshot, candidate: Snapshot, mapping: dict) -> Snapshot:
    """Copy into candidate naming, rejecting any ambiguous family or label key."""
    metrics, labels = mapping["metrics"], mapping["labels"]
    for source in sorted(metrics.keys() | labels.keys()):
        if source not in baseline.families:
            raise InputError(f"rename map: source family {source} absent from baseline")
    projected = {}
    for name, family in sorted(baseline.families.items()):
        target = metrics.get(name, name)
        if target in projected:
            raise InputError(f"rename map: family name collision for {target}")
        keys = labels.get(name, {})
        observed_keys = {key for schema in family.schemas.values() for key in schema}
        if keys.keys() - observed_keys:
            raise InputError(f"rename map: source label absent from baseline family {name}")
        counterpart = candidate.families.get(target)
        types = {family.type, counterpart.type if counterpart else None}
        reserved = ({"le"} if "histogram" in types else set()) | (
            {"quantile"} if "summary" in types else set())
        if reserved & (keys.keys() | set(keys.values())):
            raise InputError(f"rename map: reserved composite label in family {name}")
        renamed_keys = [keys.get(key, key) for key in sorted(observed_keys)]
        if len(set(renamed_keys)) != len(renamed_keys):
            raise InputError(f"rename map: label key collision in family {name}")

        def sample_name(wire_name):
            # Parsed ownership, not a suffix guess. Ordinary wire names equal
            # their family name; composites carry their parent's exact suffix.
            return target + wire_name[len(name):]

        projected[target] = Family(
            target, family.type, family.help, family.unit,
            {sample_name(wire): tuple(sorted(keys.get(k, k) for k in schema))
             for wire, schema in family.schemas.items()},
            {(sample_name(wire), tuple(sorted((keys.get(k, k), v) for k, v in values)))
             for wire, values in family.series},
        )
    result = Snapshot(projected)
    owners = _namespace(result)
    candidate_owners = _namespace(candidate)
    # A declaration cannot point into another candidate composite's reserved
    # namespace, or reserve names already used by a different candidate family.
    renamed_targets = set(metrics.values())
    for wire, owner in sorted(owners.items()):
        if owner in renamed_targets and wire in candidate_owners and candidate_owners[wire] != owner:
            raise InputError(f"rename map: candidate metric/reserved suffix collision for {wire}")
    return result
