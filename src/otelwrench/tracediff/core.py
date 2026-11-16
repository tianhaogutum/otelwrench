# SPDX-License-Identifier: Apache-2.0
"""Corpus comparison, latency budget gate and text rendering."""
from __future__ import annotations

import math

from .budget import exceeds, rational
from .jsontext import UNDEFINED, is_finite, is_safe_integer, js_sorted, js_str, stringify
from .normalize import group_key, normalize, sorted_canonical
from .observation_gates import OBSERVATION_GATE_KEYS, compare_observation_gate, validate_observation_gate
from .validate import check, object_
from .witnesses import render_witnesses, structural_witnesses, validate_witness_path

DEFAULT_POLICY = {"absoluteP95Ms": 20, "relativeP95Percent": 20, "minSamples": 20, "failOnStructure": False}
WITNESS_LIMITS = ("witnessSourceLimit", "witnessUnresolvedLimit", "witnessParentLimit")
ORDINARY_LIMITS = ("missingParentLimit", "upstreamWarningLimit", "groupLimit", "edgeLimit", "comparisonLimit",
                   "observationLimit", "operationDiffLimit")
OPTION_KEYS = ["witnesses", "witnessPath", *WITNESS_LIMITS, *ORDINARY_LIMITS]

MISSING_PARENT_LIMIT_NOTICE = (
    "Caps ordinary missing-parent diagnostic entries independently per capture in existing canonical order; totals "
    "count absent CHILD_OF relations after span deduplication. Witnesses, aggregate evidence and all gates use the "
    "complete corpus. This is a diagnostic-list output cap, not a memory or overall-output bound; path-expansion "
    "budgets still apply.")
UPSTREAM_WARNING_LIMIT_NOTICE = (
    "Caps ordinary upstream-warning diagnostic entries independently per capture in existing canonical order; totals "
    "count warning string occurrences at trace/span source paths before span deduplication, including repeated "
    "messages. Witnesses, aggregate evidence and all gates use the complete corpus. This is a diagnostic-list output "
    "cap, not a memory or overall-output bound; path-expansion budgets still apply.")
GROUP_LIMIT_NOTICE = (
    "Caps ordinary per-capture group-statistics rows in existing canonical order; totals count distinct "
    "service/operation/kind groups after span deduplication. Comparisons, diffs, diagnostics, witnesses and all gates "
    "use the complete corpus. This is a group-row output cap, not a memory or overall-output bound; path-expansion "
    "budgets still apply.")
EDGE_LIMIT_NOTICE = (
    "Caps ordinary per-capture edge-statistics rows in existing canonical order; totals count distinct "
    "parent-group/child-group rows after span deduplication, while each edge.count retains full relation "
    "multiplicity. Comparisons, diffs, diagnostics, witnesses and all gates use the complete corpus. This is an "
    "edge-row output cap, not a memory or overall-output bound; path-expansion budgets still apply.")
COMPARISON_LIMIT_NOTICE = (
    "Caps ordinary paired-group latency comparison rows in existing canonical group-key order; totals include "
    "insufficient-samples rows and exclude new/removed groups. All gates and counters, observation rows, diffs, "
    "per-capture statistics, diagnostics and witnesses use the complete corpus under their own options. This is a "
    "comparison-row output cap, not a memory or overall-output bound; path-expansion budgets still apply.")
OBSERVATION_LIMIT_NOTICE = (
    "Caps ordinary observation rows independently per enabled count/error-fraction gate in existing canonical "
    "group-key order; totals include unpaired and insufficient-samples rows. All gates and counters, comparisons, "
    "diffs, per-capture statistics, diagnostics and witnesses use the complete corpus under their own options. This "
    "is an observation-row output cap, not a memory or overall-output bound; path-expansion budgets still apply.")
OPERATION_DIFF_LIMIT_NOTICE = (
    "Caps ordinary added/removed operation-diff rows independently in existing canonical whole-object order; totals "
    "count distinct normalized service/operation/kind groups present exclusively in one capture, independent of "
    "sample eligibility. All gates and counters, edge diffs, comparisons, observations, statistics, diagnostics and "
    "witnesses use the complete corpus under their own options. This is an operation-diff output cap, not a memory "
    "or overall-output bound; path-expansion budgets still apply.")
PROJECTION_NOTICE = (
    "IDs and absolute start times establish identity/parents only. Metadata listed in omittedFields is not compared. "
    "Edge counts are reported; only edge presence is structural. Missing parents indicate incomplete capture, not "
    "broken instrumentation.")
PERCENTILE_NOTICE = "nearest-rank: sorted durations[ceil(p*n)-1], including median (p=0.5); no interpolation"
PRECISION_NOTICE = ("Timestamp arithmetic uses integer nanoseconds; duration <= 9007199254740991 ns. Output "
                    "milliseconds use IEEE-754 numbers.")


def validate_policy(config=UNDEFINED) -> dict:
    if config is UNDEFINED:
        config = {}
    object_(config, "config", [*DEFAULT_POLICY, *OBSERVATION_GATE_KEYS])
    policy = {**DEFAULT_POLICY, **config}
    for key in ("absoluteP95Ms", "relativeP95Percent"):
        check(is_finite(policy[key]) and policy[key] >= 0, f"config.{key}", "expected finite nonnegative number")
    check(is_safe_integer(policy["minSamples"]) and policy["minSamples"] >= 1, "config.minSamples",
          "expected positive safe integer")
    check(isinstance(policy["failOnStructure"], bool), "config.failOnStructure", "expected boolean")
    for key in OBSERVATION_GATE_KEYS:
        if key in config:
            policy[key] = validate_observation_gate(config[key], key)
    return policy


def _milliseconds(ns: int) -> float:
    # Number(BigInt) / 1e6: round to the nearest double first, then divide.
    return float(ns) / 1e6


def _stats(g: dict) -> dict:
    d = g["durations"]
    return {"group": g["group"], "count": g["count"], "errorCount": g["errorCount"],
            "errorFraction": g["errorCount"] / g["count"],
            "durationMs": {"min": _milliseconds(d[0]), "median": _milliseconds(d[math.ceil(g["count"] / 2) - 1]),
                           "p95": _milliseconds(g["p95Ns"]), "max": _milliseconds(d[-1])}}


def _list_summary(total: int, listed: int) -> dict:
    return {"total": total, "listed": listed, "omitted": total - listed, "truncated": listed < total}


def _summary(capture: dict, missing_parent_limit, upstream_warning_limit, group_limit, edge_limit) -> dict:
    diagnostics = capture["diagnostics"]
    if missing_parent_limit is not None:
        total = len(diagnostics["missingParents"])
        listed = min(total, missing_parent_limit)
        diagnostics = {**diagnostics, "missingParents": diagnostics["missingParents"][:listed],
                       "missingParentLimit": missing_parent_limit,
                       "missingParentLimitNotice": MISSING_PARENT_LIMIT_NOTICE,
                       "missingParentSummary": _list_summary(total, listed)}
    if upstream_warning_limit is not None:
        total = len(diagnostics["upstreamWarnings"])
        listed = min(total, upstream_warning_limit)
        diagnostics = {**diagnostics, "upstreamWarnings": diagnostics["upstreamWarnings"][:listed],
                       "upstreamWarningLimit": upstream_warning_limit,
                       "upstreamWarningLimitNotice": UPSTREAM_WARNING_LIMIT_NOTICE,
                       "upstreamWarningSummary": _list_summary(total, listed)}
    groups = sorted_canonical([_stats(g) for g in capture["groups"].values()])
    total = len(capture["groups"])
    listed = total if group_limit is None else min(total, group_limit)
    edges = sorted_canonical(capture["edges"].values())
    edge_total = len(capture["edges"])
    edge_listed = edge_total if edge_limit is None else min(edge_total, edge_limit)
    return {"format": capture["format"], "traceCount": capture["traceCount"], "spanCount": capture["spanCount"],
            "inputSpanCount": capture["inputSpanCount"], "duplicateSpanCount": capture["duplicateSpanCount"],
            "groups": groups if group_limit is None else groups[:listed],
            **({} if group_limit is None else {"groupLimit": group_limit, "groupLimitNotice": GROUP_LIMIT_NOTICE,
                                               "groupSummary": _list_summary(total, listed)}),
            "edges": edges if edge_limit is None else edges[:edge_listed],
            **({} if edge_limit is None else {"edgeLimit": edge_limit, "edgeLimitNotice": EDGE_LIMIT_NOTICE,
                                              "edgeSummary": _list_summary(edge_total, edge_listed)}),
            "diagnostics": diagnostics}


def _edge_shape(e: dict) -> dict:
    return {"parent": e["parent"], "child": e["child"]}


def compare(baseline_input, candidate_input, config=UNDEFINED, options=UNDEFINED) -> dict:
    """Compare two parsed captures; raises ``InputError`` for unsupported input or options."""
    if options is UNDEFINED:
        options = {}
    object_(options, "options", OPTION_KEYS)
    if "witnesses" in options:
        check(isinstance(options["witnesses"], bool), "options.witnesses", "expected boolean")
    witnesses = options.get("witnesses") is True
    witness_path = None
    if "witnessPath" in options:
        check(witnesses, "options.witnessPath", "requires witnesses: true")
        witness_path = validate_witness_path(options["witnessPath"])
    limits: dict = {}
    for key in WITNESS_LIMITS:
        if key in options:
            check(witnesses, f"options.{key}", "requires witnesses: true")
            check(is_safe_integer(options[key]) and options[key] > 0, f"options.{key}", "expected positive safe integer")
            limits[key] = int(options[key])
    for key in ORDINARY_LIMITS:
        if key in options:
            check(is_safe_integer(options[key]) and options[key] > 0, f"options.{key}", "expected positive safe integer")
            limits[key] = int(options[key])
    policy = validate_policy(config)
    graph_options = {"retainSpans": witnesses}
    baseline = normalize(baseline_input, graph_options)
    candidate = normalize(candidate_input, graph_options)
    absolute_budget, relative_budget = rational(policy["absoluteP95Ms"]), rational(policy["relativeP95Percent"])
    added, removed, comparisons = [], [], []
    for key, g in candidate["groups"].items():
        if key not in baseline["groups"]:
            added.append(g["group"])
    for key, b in baseline["groups"].items():
        c = candidate["groups"].get(key)
        if c is None:
            removed.append(b["group"])
            continue
        delta_ns = c["p95Ns"] - b["p95Ns"]
        if b["p95Ns"] == 0:
            relative_state = "infinite" if c["p95Ns"] > 0 else "zero-to-zero"
            relative_percent = None if c["p95Ns"] > 0 else 0
        else:
            relative_state = "finite"
            relative_percent = float(delta_ns) / float(b["p95Ns"]) * 100
        eligible = b["count"] >= policy["minSamples"] and c["count"] >= policy["minSamples"]
        absolute_exceeded = exceeds(delta_ns, 1000000, absolute_budget)
        relative_exceeded = relative_state == "infinite" or (
            b["p95Ns"] > 0 and exceeds(delta_ns * 100, b["p95Ns"], relative_budget))
        regression = eligible and absolute_exceeded and relative_exceeded
        comparisons.append({
            "group": b["group"], "baselineCount": b["count"], "candidateCount": c["count"],
            "baselineP95Ms": _milliseconds(b["p95Ns"]), "candidateP95Ms": _milliseconds(c["p95Ns"]),
            "deltaP95Ms": _milliseconds(delta_ns), "relativeP95Percent": relative_percent,
            "relativeState": relative_state, "eligible": eligible, "absoluteExceeded": absolute_exceeded,
            "relativeExceeded": relative_exceeded, "regression": regression,
            "reason": "insufficient-samples" if not eligible else "both-thresholds-exceeded" if regression
            else "within-budget"})
    added_edges, removed_edges, count_changed = [], [], []
    for key, edge in baseline["edges"].items():
        c = candidate["edges"].get(key)
        if c is None:
            removed_edges.append(_edge_shape(edge))
        elif edge["count"] != c["count"]:
            count_changed.append({**_edge_shape(edge), "baselineCount": edge["count"], "candidateCount": c["count"]})
    for key, edge in candidate["edges"].items():
        if key not in baseline["edges"]:
            added_edges.append(_edge_shape(edge))
    structural_changes = len(added) + len(removed) + len(added_edges) + len(removed_edges)
    latency_regressions = sum(1 for c in comparisons if c["regression"])
    observation_gates: dict = {}
    observation_failures: dict = {}
    observation_summary: dict = {}
    observation_limit = limits.get("observationLimit")
    for key, name in (("countGate", "count"), ("errorFractionGate", "errorFraction")):
        if key not in policy:
            continue
        rows = compare_observation_gate(baseline, candidate, key, policy[key])
        observation_failures[f"{key}Failures"] = sum(1 for e in rows if e["failed"])
        if observation_limit is not None:
            listed = min(len(rows), observation_limit)
            observation_summary[name] = _list_summary(len(rows), listed)
            rows = rows[:listed]
        observation_gates[name] = rows
    failed = latency_regressions > 0 or (policy["failOnStructure"] and structural_changes > 0) \
        or any(n > 0 for n in observation_failures.values())
    comparisons = js_sorted(comparisons, key=lambda c: group_key(c["group"]))
    comparison_limit = limits.get("comparisonLimit")
    total = len(comparisons)
    listed = total if comparison_limit is None else min(total, comparison_limit)
    operations: dict = {"added": sorted_canonical(added), "removed": sorted_canonical(removed)}
    operation_diff_limit = limits.get("operationDiffLimit")
    if operation_diff_limit is not None:
        operation_diff_summary = {}
        for action in ("added", "removed"):
            action_total = len(operations[action])
            action_listed = min(action_total, operation_diff_limit)
            operation_diff_summary[action] = _list_summary(action_total, action_listed)
            operations[action] = operations[action][:action_listed]
        operations.update({"operationDiffLimit": operation_diff_limit,
                           "operationDiffLimitNotice": OPERATION_DIFF_LIMIT_NOTICE,
                           "operationDiffSummary": operation_diff_summary})
    report: dict = {"schemaVersion": 1, "policy": policy, "regression": failed}
    if witnesses:
        report["structuralWitnesses"] = structural_witnesses(
            baseline, candidate, witness_path, limits.get("witnessSourceLimit"), limits.get("witnessUnresolvedLimit"),
            limits.get("witnessParentLimit"))
    report["gate"] = {"failed": failed, "latencyRegressions": latency_regressions,
                      "structuralChanges": structural_changes,
                      "insufficientSampleGroups": sum(1 for c in comparisons if not c["eligible"]),
                      **observation_failures}
    if observation_limit is not None:
        report.update({"observationLimit": observation_limit, "observationLimitNotice": OBSERVATION_LIMIT_NOTICE,
                       "observationSummary": observation_summary})
    if observation_gates:
        report["observationGates"] = observation_gates
    report["normalization"] = {
        "compared": ["service", "operation", "kind", "duration", "error", "CHILD_OF parent-operation edges"],
        "projectionNotice": PROJECTION_NOTICE, "percentile": PERCENTILE_NOTICE, "precision": PRECISION_NOTICE}
    summary_limits = (limits.get("missingParentLimit"), limits.get("upstreamWarningLimit"), limits.get("groupLimit"),
                      limits.get("edgeLimit"))
    report["baseline"] = _summary(baseline, *summary_limits)
    report["candidate"] = _summary(candidate, *summary_limits)
    report["diff"] = {"operations": operations,
                      "edges": {"added": sorted_canonical(added_edges), "removed": sorted_canonical(removed_edges),
                                "countChanged": sorted_canonical(count_changed)}}
    if comparison_limit is not None:
        report.update({"comparisonLimit": comparison_limit, "comparisonLimitNotice": COMPARISON_LIMIT_NOTICE,
                       "comparisonSummary": _list_summary(total, listed)})
    report["comparisons"] = comparisons if comparison_limit is None else comparisons[:listed]
    return report


def _label(g: dict) -> str:
    return stringify([g["service"], g["operation"], g["kind"]])


def _counts(s: dict) -> str:
    return f"listed={s['listed']} total={s['total']} omitted={s['omitted']} truncated={js_str(s['truncated'])}"


def render_text(report: dict) -> str:
    gate, policy = report["gate"], report["policy"]
    lines = [f"tracediff: {'FAIL' if gate['failed'] else 'PASS'}",
             f"latency regressions={gate['latencyRegressions']}; structural changes={gate['structuralChanges']}; "
             f"insufficient samples={gate['insufficientSampleGroups']}",
             f"budget: p95 delta > {js_str(policy['absoluteP95Ms'])} ms AND > {js_str(policy['relativeP95Percent'])}%; "
             f"minSamples={js_str(policy['minSamples'])}; failOnStructure={js_str(policy['failOnStructure'])}"]
    for side in ("baseline", "candidate"):
        capture = report[side]
        lines.append(f"{side}: {capture['format']}, {capture['traceCount']} traces, {capture['spanCount']} spans, "
                     f"{capture['duplicateSpanCount']} exact duplicates removed")
        if "groupSummary" in capture:
            lines.append(f"  group limit: {js_str(capture['groupLimit'])}. {capture['groupLimitNotice']}")
            lines.append(f"  groups: {_counts(capture['groupSummary'])}")
        for g in capture["groups"]:
            durations = "/".join(js_str(v) for v in g["durationMs"].values())
            lines.append(f"  {_label(g['group'])} count={g['count']} errors={g['errorCount']} "
                         f"errorFraction={js_str(g['errorFraction'])} min/median/p95/max(ms)={durations}")
        if "edgeSummary" in capture:
            lines.append(f"  edge limit: {js_str(capture['edgeLimit'])}. {capture['edgeLimitNotice']}")
            lines.append(f"  edges: {_counts(capture['edgeSummary'])}")
        for e in capture["edges"]:
            lines.append(f"  edge {_label(e['parent'])} -> {_label(e['child'])} count={e['count']}")
        d = capture["diagnostics"]
        missing_total = d["missingParentSummary"]["total"] if "missingParentSummary" in d else len(d["missingParents"])
        warning_total = d["upstreamWarningSummary"]["total"] if "upstreamWarningSummary" in d \
            else len(d["upstreamWarnings"])
        lines.append(f"  diagnostics: {missing_total} missing parents (incomplete capture); "
                     f"{warning_total} upstream warnings")
        if "missingParentSummary" in d:
            lines.append(f"  missing parent limit: {js_str(d['missingParentLimit'])}. {d['missingParentLimitNotice']}")
            lines.append(f"  missing parents: {_counts(d['missingParentSummary'])}")
        for m in d["missingParents"]:
            lines.append(f"    missing parent {m['parentTraceId']}/{m['parentSpanId']} of {m['traceId']}/{m['spanId']}")
        if "upstreamWarningSummary" in d:
            lines.append(f"  upstream warning limit: {js_str(d['upstreamWarningLimit'])}. "
                         f"{d['upstreamWarningLimitNotice']}")
            lines.append(f"  upstream warnings: {_counts(d['upstreamWarningSummary'])}")
        for w in d["upstreamWarnings"]:
            lines.append(f"    warning {stringify(w['message'])} at {w['path']}")
        for o in d["omittedFields"]:
            lines.append(f"    not compared: {o['path']} ({o['occurrences']} occurrences)")
    operations = report["diff"]["operations"]
    if "operationDiffSummary" in operations:
        lines.append(f"operation diff limit: {js_str(operations['operationDiffLimit'])}. "
                     f"{operations['operationDiffLimitNotice']}")
    for action in ("added", "removed"):
        if "operationDiffSummary" in operations:
            lines.append(f"{action} operations: {_counts(operations['operationDiffSummary'][action])}")
        for g in operations[action]:
            lines.append(f"{action} operation {_label(g)}")
        for e in report["diff"]["edges"][action]:
            lines.append(f"{action} edge {_label(e['parent'])} -> {_label(e['child'])}")
    for e in report["diff"]["edges"]["countChanged"]:
        lines.append(f"edge count changed {_label(e['parent'])} -> {_label(e['child'])}: "
                     f"{e['baselineCount']} -> {e['candidateCount']}")
    if "comparisonSummary" in report:
        lines.append(f"comparison limit: {js_str(report['comparisonLimit'])}. {report['comparisonLimitNotice']}")
        lines.append(f"comparisons: {_counts(report['comparisonSummary'])}")
    for c in report["comparisons"]:
        relative = "infinite" if c["relativeState"] == "infinite" else f"{js_str(c['relativeP95Percent'])}%"
        lines.append(f"{'REGRESSION' if c['regression'] else c['reason']} {_label(c['group'])} "
                     f"p95={js_str(c['baselineP95Ms'])}->{js_str(c['candidateP95Ms'])}ms "
                     f"delta={js_str(c['deltaP95Ms'])}ms relative={relative}")
    if "observationSummary" in report:
        lines.append(f"observation limit: {js_str(report['observationLimit'])}. {report['observationLimitNotice']}")
    if "observationGates" in report:
        lines.append("Count/error-fraction gates are observational corpus differences; they do not establish causal "
                     "regressions.")
        for name, entries in report["observationGates"].items():
            count = name == "count"
            key = "countGate" if count else "errorFractionGate"
            threshold = "maxChangePercent" if count else "maxIncreasePercent"
            absolute_key = "maxChangeCount" if count else "maxIncreasePercentagePoints"
            change_key = "changeCount" if count else "changePercentagePoints"
            unit = "spans" if count else "pp"
            gate_policy = policy[key]
            absolute = absolute_key in gate_policy
            allowance = f" AND {absolute_key}={js_str(gate_policy[absolute_key])} {unit}" if absolute else ""
            lines.append(f"{name} gate: {threshold}={js_str(gate_policy[threshold])}%{allowance}; "
                         f"minSamples={js_str(gate_policy['minSamples'])}")
            if "observationSummary" in report:
                lines.append(f"{name} rows: {_counts(report['observationSummary'][name])}")
            for e in entries:
                evidence = ""
                if absolute:
                    change = e[change_key]
                    evidence = f" delta={'unpaired' if change is None else f'{js_str(change)} {unit}'}"
                    if count:
                        evidence += f" absoluteExceeded={js_str(e['absoluteExceeded'])} " \
                                    f"relativeExceeded={js_str(e['relativeExceeded'])}"
                relative = f"{js_str(e['changePercent'])}%" if e["relativeState"] in ("finite", "zero-to-zero") \
                    else e["relativeState"]
                lines.append(f"{name} {'GATE-FAIL ' if e['failed'] else ''}{e['reason']} {_label(e['group'])} "
                             f"value={js_str(e['baselineValue'])}->{js_str(e['candidateValue'])} "
                             f"relative={relative}{evidence}")
    if "structuralWitnesses" in report:
        lines.extend(render_witnesses(report["structuralWitnesses"]))
    n = report["normalization"]
    lines.extend([n["projectionNotice"], n["percentile"], n["precision"]])
    return "\n".join(lines) + "\n"
