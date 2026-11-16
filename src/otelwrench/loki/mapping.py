"""Attribute placement explanations, grouping, and explicit-profile comparison."""

import re

from .config import DEFAULTS
from .otlp import flatten, parse_logs
from .strict import PreviewError, fail

WARNINGS = [
    "Attribute-mapping subset only: service-name discovery, distributor overrides, tenant limits and live ingestion validation are not simulated.",
    "Stream counts describe only the supplied records; they are not production cardinality or billing predictions.",
    "Push JSON contains strings; AnyValue types and nested attribute structure are not recoverable from that projection. See per-record transformations.",
]


def normalize(key):
    result = re.sub(r"[^a-zA-Z0-9_]", "_", key)
    if result[0].isdigit():
        result = "_" + result
    return result


def preview(data, profile):
    parsed, diagnostics = parse_logs(data)
    records, groups, warnings = [], {}, list(WARNINGS)
    for record in parsed:
        labels, metadata, placements, dropped = {}, {}, [], []
        used = {}

        def reserve(key, origin):
            if key in used:
                fail(record["id"], f"normalized key collision {key!r}: {used[key]} and {origin}")
            used[key] = origin

        for level in ("resource", "scope", "log"):
            for original, value in record[level]:
                decision = profile.resolve(level, original)
                action, reason = decision["placement"], decision["reason"]
                for path, text, kind in flatten([original], value):
                    key = normalize("_".join(path))
                    reserve(key, f"{level}:{path!r}")
                    changes = []
                    if len(path) > 1:
                        changes.append("nested map flattened with underscore separators")
                    if len(path) > 1 or key != original:
                        changes.append("key normalized to ASCII label-name subset")
                    if kind != "stringValue":
                        changes.append("AnyValue converted to string; original type is not retained in push JSON")
                    placement = {"source": level, "sourcePath": path, "key": key, "value": text,
                                 "placement": action, "reason": reason, "transformations": changes}
                    placements.append(placement)
                    if action == "drop":
                        dropped.append(placement)
                    elif action == "index_label":
                        if not text:
                            fail(record["id"], f"empty index-label value for {key!r} is unsupported; demote or drop this attribute explicitly; source {level}:{path!r}; rule {decision['ruleRef']}")
                        labels[key] = text
                    else:
                        metadata[key] = text
        for key, value in record["fixed"].items():
            reserve(key, "fixed OTLP field " + key)
            metadata[key] = value
            placements.append({"source": "field", "sourcePath": [key], "key": key, "value": value,
                               "placement": "structured_metadata", "reason": "fixed OTLP field; attribute rules do not apply",
                               "transformations": ["OTLP field represented as a metadata string"]})
        transformations = []
        if record["bodyType"] not in ("stringValue",):
            transformations.append("body converted from " + record["bodyType"] + " to a Loki string log line")
        result = {key: record[key] for key in ("id", "timestamp", "timestampSource", "line")}
        result.update({"stream": dict(sorted(labels.items())), "structuredMetadata": dict(sorted(metadata.items())),
                       "placements": placements, "dropped": dropped, "transformations": transformations})
        identity = tuple(sorted(labels.items()))
        groups.setdefault(identity, []).append(result)
        records.append(result)
    streams = [{"stream": dict(identity), "recordIds": [x["id"] for x in group], "recordCount": len(group)}
               for identity, group in sorted(groups.items())]
    if any(not any(stream["stream"].values()) for stream in streams):
        warnings.append("A projected stream has no nonempty index label. Preview is available; --push-json refuses this projection. Configure an explicit resource index label.")
    return {"formatVersion": 1, "profile": profile.describe(), "defaultResourceAttributes": DEFAULTS,
            "summary": {"recordCount": len(records), "streamCount": len(streams),
                        "droppedAttributeCount": sum(len(x["dropped"]) for x in records)},
            "warnings": warnings, "diagnostics": diagnostics, "streams": streams, "records": records}


def explain(data, profile, *, record_id, key=None):
    """Trace a selected record/output key only after the entire preview validates.

    Reuse its exact projection, including collision and empty-label rejection;
    selection cannot conceal an invalid record elsewhere in the capture.
    """
    return _explain_report(preview(data, profile), profile, record_id=record_id, key=key)


def _explain_report(report, profile, *, record_id, key, command="explain"):
    """Build selector traces from an already validated, complete projection."""
    record = next((r for r in report["records"] if r["id"] == record_id), None)
    if record is None:
        fail(command, f"record ID not found: {record_id!r}")
    placements = [p for p in record["placements"] if key is None or p["key"] == key]
    if key is not None and not placements:
        fail(record_id, f"normalized output key not found: {key!r}")
    explanations = []
    for placement in placements:
        source, path = placement["source"], placement["sourcePath"]
        if source == "field":
            selection = {"placement": placement["placement"], "reason": placement["reason"],
                         "ruleRef": "builtin:fixed_otlp_field", "rules": []}
        else:
            selection = profile.resolve(source, path[0], trace=True)
        explanations.append({"sourceRef": {"recordId": record_id, "source": source, "sourcePath": path},
                             "normalization": {"joinedKey": "_".join(path), "normalizedKey": placement["key"]},
                             "selection": selection, "final": placement})
    return {"formatVersion": 1, "profile": report["profile"], "defaultResourceAttributes": DEFAULTS,
            "warnings": report["warnings"], "diagnostics": report["diagnostics"],
            "record": record, "explanations": explanations}


def push_json(report):
    groups = {}
    for record in report["records"]:
        if int(record["timestamp"]) >= 2**63:
            fail(record["id"], "cannot emit push JSON: timestamp exceeds Loki signed int64 nanoseconds")
        labels = record["stream"]
        if not any(labels.values()):
            fail(record["id"], "cannot emit push JSON for a stream without a nonempty index label")
        groups.setdefault(tuple(sorted(labels.items())), []).append(
            [record["timestamp"], record["line"], record["structuredMetadata"]])
    return {"streams": [{"stream": dict(identity), "values": sorted(values, key=lambda x: int(x[0]))}
                        for identity, values in sorted(groups.items())]}


def _stream_topology(left, right):
    """Join validated stream partitions by record ID, never by line or timestamp.

    Preview already sorts stream label sets and retains input-order membership.
    A nonempty intersection is one transition; fan-in/out detects regrouping
    even when the two partitions have the same number of streams.
    """
    before, after = left["streams"], right["streams"]
    indexes = [{record_id: index for index, stream in enumerate(streams) for record_id in stream["recordIds"]}
               for streams in (before, after)]
    groups = {}
    for record in left["records"]:
        pair = (indexes[0][record["id"]], indexes[1][record["id"]])
        groups.setdefault(pair, []).append(record["id"])
    outgoing = [set() for _ in before]
    incoming = [set() for _ in after]
    transitions = []
    for (old, new), record_ids in sorted(groups.items()):
        outgoing[old].add(new)
        incoming[new].add(old)
        transitions.append({"beforeStreamIndex": old, "afterStreamIndex": new,
                            "recordIds": record_ids, "recordCount": len(record_ids)})
    merges = [{"afterStreamIndex": index, "beforeStreamIndexes": sorted(sources)}
              for index, sources in enumerate(incoming) if len(sources) > 1]
    splits = [{"beforeStreamIndex": index, "afterStreamIndexes": sorted(targets)}
              for index, targets in enumerate(outgoing) if len(targets) > 1]
    return {"before": before, "after": after, "transitions": transitions,
            "merges": merges, "splits": splits, "membershipChanged": bool(merges or splits)}


def _decision_summary(before, after):
    """Compare only winners; raw explain sides retain all competing rules.

    Profiles cannot remove leaves: drop is a placement too. Align the same
    capture's selected leaves by provenance and normalized key, in before order.
    """
    def identity(entry):
        source = entry["sourceRef"]
        return (source["recordId"], source["source"], tuple(source["sourcePath"]),
                entry["normalization"]["normalizedKey"])

    def winner(entry):
        selection = entry["selection"]
        selector = next((rule["selector"] for rule in selection["rules"]
                         if rule["status"] == "selected"), None)
        return {"ruleRef": selection["ruleRef"], "placement": selection["placement"], "selector": selector}

    right = {identity(entry): entry for entry in after["explanations"]}
    decisions = []
    for entry in before["explanations"]:
        old, new = winner(entry), winner(right[identity(entry)])
        changed_fields = [field for field in ("ruleRef", "placement", "selector") if old[field] != new[field]]
        decisions.append({"sourceRef": entry["sourceRef"], "key": entry["normalization"]["normalizedKey"],
                          "before": old, "after": new, "changedFields": changed_fields,
                          "placementChanged": old["placement"] != new["placement"], "winnerChanged": old != new})
    return decisions


def compare(data, before, after, *, include_records=False, record_id=None, key=None,
            changed_decisions_only=False, placement_decisions_only=False, reason_decisions_only=False,
            selector_decisions_only=False, rule_ref_decisions_only=False, unchanged_decisions_only=False,
            decision_source=None):
    filters = [("--changed-decisions-only", changed_decisions_only),
               ("--placement-decisions-only", placement_decisions_only),
               ("--reason-decisions-only", reason_decisions_only),
               ("--selector-decisions-only", selector_decisions_only),
               ("--rule-ref-decisions-only", rule_ref_decisions_only),
               ("--unchanged-decisions-only", unchanged_decisions_only)]
    requested = [name for name, enabled in filters if enabled]
    if len(requested) > 1:
        fail("compare", "cannot combine " + " and ".join(requested))
    if key is not None and record_id is None:
        fail("compare", "--key requires --record-id")
    if changed_decisions_only and record_id is None:
        fail("compare", "--changed-decisions-only requires --record-id")
    if placement_decisions_only and record_id is None:
        fail("compare", "--placement-decisions-only requires --record-id")
    if reason_decisions_only and record_id is None:
        fail("compare", "--reason-decisions-only requires --record-id")
    if selector_decisions_only and record_id is None:
        fail("compare", "--selector-decisions-only requires --record-id")
    if rule_ref_decisions_only and record_id is None:
        fail("compare", "--rule-ref-decisions-only requires --record-id")
    if unchanged_decisions_only and record_id is None:
        fail("compare", "--unchanged-decisions-only requires --record-id")
    if decision_source is not None:
        if decision_source not in ("resource", "scope", "log", "field"):
            fail("compare", "--decision-source must be one of resource, scope, log, field")
        if record_id is None:
            fail("compare", "--decision-source requires --record-id")
    # Evaluate both explicit policies, even if one cannot project this input.
    # Each uses preview's strict validation, including collisions on dropped keys.
    reports, validation = [], []
    for name, profile in (("before", before), ("after", after)):
        try:
            reports.append(preview(data, profile))
            validation.append(f"{name}: valid")
        except PreviewError as exc:
            reports.append(None)
            validation.append(f"{name}: invalid: {exc}")
    if any(report is None for report in reports):
        fail("comparison validation failed", "; ".join(validation))
    left, right = reports
    changes, stream_changes = [], []
    for old, new in zip(left["records"], right["records"], strict=True):
        if old["stream"] != new["stream"]:
            stream_changes.append({"recordId": old["id"], "before": old["stream"], "after": new["stream"]})
        for a, b in zip(old["placements"], new["placements"], strict=True):
            if a["placement"] != b["placement"]:
                changes.append({"recordId": old["id"], "source": a["source"], "sourcePath": a["sourcePath"],
                                "key": a["key"], "value": a["value"],
                                "before": {"placement": a["placement"], "reason": a["reason"]},
                                "after": {"placement": b["placement"], "reason": b["reason"]}})
    report = {"formatVersion": 1, "countScope": "observed_input_records_only",
            "before": left["summary"], "after": right["summary"],
            "profiles": {"before": left["profile"], "after": right["profile"]},
            "streamCountDelta": right["summary"]["streamCount"] - left["summary"]["streamCount"],
            "streamTopology": _stream_topology(left, right),
            "changedPlacementCount": len(changes), "changedPlacements": changes,
            "changedStreams": stream_changes, "warnings": sorted(set(left["warnings"] + right["warnings"])),
            "diagnostics": left["diagnostics"], "defaultResourceAttributes": DEFAULTS}
    if include_records:
        report["records"] = [{"recordId": old["id"], "before": old, "after": new}
                             for old, new in zip(left["records"], right["records"], strict=True)]
    if record_id is not None:
        selected = {"recordId": record_id}
        for side, projected, profile in (("before", left, before), ("after", right, after)):
            selected[side] = _explain_report(projected, profile, record_id=record_id, key=key, command="compare")
            selected[side + "StreamIndex"] = next(
                index for index, stream in enumerate(projected["streams"]) if record_id in stream["recordIds"])
        selected["decisions"] = _decision_summary(selected["before"], selected["after"])
        if decision_source is not None:
            selected["decisionSource"] = decision_source
            selected["decisions"] = [entry for entry in selected["decisions"]
                                     if entry["sourceRef"]["source"] == decision_source]
        # Totals describe the record/key/source selection before any summary view.
        selected["decisionCounts"] = {
            "selectedLeafCount": len(selected["decisions"]),
            "winnerChangedCount": sum(entry["winnerChanged"] for entry in selected["decisions"]),
            "placementChangedCount": sum(entry["placementChanged"] for entry in selected["decisions"]),
            "reasonChangedCount": sum(entry["winnerChanged"] and not entry["placementChanged"]
                                      for entry in selected["decisions"]),
        }
        selected["decisionFieldCounts"] = {
            field: sum(field in entry["changedFields"] for entry in selected["decisions"])
            for field in ("ruleRef", "placement", "selector")
        }
        selected["decisionCountsBySource"] = {
            source: dict.fromkeys(selected["decisionCounts"], 0)
            for source in ("resource", "scope", "log", "field")
        }
        for entry in selected["decisions"]:
            counts = selected["decisionCountsBySource"][entry["sourceRef"]["source"]]
            counts["selectedLeafCount"] += 1
            counts["winnerChangedCount"] += entry["winnerChanged"]
            counts["placementChangedCount"] += entry["placementChanged"]
            counts["reasonChangedCount"] += entry["winnerChanged"] and not entry["placementChanged"]
        selected["decisionFilter"] = "all"
        if changed_decisions_only:
            selected["decisionFilter"] = "winner_changed"
            selected["decisions"] = [entry for entry in selected["decisions"] if entry["winnerChanged"]]
        elif placement_decisions_only:
            selected["decisionFilter"] = "placement_changed"
            selected["decisions"] = [entry for entry in selected["decisions"] if entry["placementChanged"]]
        elif reason_decisions_only:
            selected["decisionFilter"] = "reason_changed"
            selected["decisions"] = [entry for entry in selected["decisions"]
                                     if entry["winnerChanged"] and not entry["placementChanged"]]
        elif selector_decisions_only:
            selected["decisionFilter"] = "selector_changed"
            selected["decisions"] = [entry for entry in selected["decisions"] if "selector" in entry["changedFields"]]
        elif rule_ref_decisions_only:
            selected["decisionFilter"] = "rule_ref_changed"
            selected["decisions"] = [entry for entry in selected["decisions"] if "ruleRef" in entry["changedFields"]]
        elif unchanged_decisions_only:
            selected["decisionFilter"] = "unchanged"
            selected["decisions"] = [entry for entry in selected["decisions"] if entry["changedFields"] == []]
        report["selectedRecordTrace"] = selected
    return report
