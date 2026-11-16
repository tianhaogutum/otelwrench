# SPDX-License-Identifier: Apache-2.0
"""Replay, compare, saved-report and grid entry points (the library API)."""
from __future__ import annotations

import math
from typing import Any

from ._js import is_safe_integer, js_eq, js_number, number_to_string
from .capture_identity import capture_identity
from .checkpoint import checkpoint_rule, read_checkpoint
from .grid_csv import format_grid_csv
from .join import join_reports
from .matrix import parse_matrix
from .report import ABSENT, MAX_REPORT_EVALUATIONS, describe_report, equal, verify_report
from .report_set_summary_csv import format_report_set_summary_csv
from .rule_identity import rule_identity
from .validation import COMPARE_OPS, config, duration, fail, keys

# Bound the size of the expanded series x evaluation timeline before allocating.
MAX_EVALUATIONS = 1_000_000


class _Summary(dict):
    """A summary dict carrying exact integer milliseconds privately.

    Converting JSON seconds back to milliseconds can add/drop 1 ms at large
    safe-integer values, so deltas use these exact values. The attribute is
    never serialized and copies of the dict do not inherit it.
    """

    exact: tuple


def _measured(summary: dict, first_firing_ms: Any, firing_duration_ms: float) -> _Summary:
    result = summary if isinstance(summary, _Summary) else _Summary(summary)
    result.exact = (first_firing_ms, firing_duration_ms)
    return result


def _exact(summary: dict) -> tuple:
    return summary.exact  # AttributeError mirrors the JS TypeError on foreign input


def _seconds(ms: Any) -> Any:
    return None if ms is None else ms / 1000


def _prepare(matrix: dict, cfg: dict) -> dict:
    start = cfg["startMs"] if cfg["startMs"] is not None else matrix["minMs"]
    end = cfg["endMs"] if cfg["endMs"] is not None else matrix["maxMs"]
    if not math.isfinite(start) or not math.isfinite(end):
        fail("range: no samples; supply explicit start and end")
    span = end - start
    interval = cfg["intervalMs"]
    if not is_safe_integer(span) or math.fmod(span, interval) != 0:
        fail("range: end - start must be an exact multiple of evaluationInterval")
    count = max(1, len(matrix["series"]))
    if not is_safe_integer(span * count):
        fail("range: aggregate duration exceeds safe integer milliseconds")
    steps = span / interval + 1
    if steps * count > MAX_EVALUATIONS:
        fail(f"range: expanded timeline exceeds {MAX_EVALUATIONS} series-evaluations")
    for series in matrix["series"]:
        for t in series["samples"]:
            if t < start or t > end:
                fail(f"series {series['id']}: sample {number_to_string(t / 1000)} outside configured range; refusing to discard it")
            if math.fmod(t - start, interval) != 0:
                fail(f"series {series['id']}: timestamp {number_to_string(t / 1000)} incompatible with evaluationInterval")
    return {"start": start, "end": end, "steps": steps}


def _episode(start_ms: float, end_ms: Any, window_start: float, window_end: float, censored: bool) -> dict:
    stop = window_end if censored else end_ms
    episode = {"start": start_ms / 1000, "end": None if censored else end_ms / 1000,
               "durationSeconds": (stop - max(window_start, start_ms)) / 1000, "censored": censored}
    if start_ms < window_start:
        episode["continued"] = True
    return episode


def _run(matrix: dict, cfg: dict, checkpoint: Any = ABSENT) -> dict:
    prepared = _prepare(matrix, cfg)
    start, end, steps = prepared["start"], prepared["end"], prepared["steps"]
    resume = None if checkpoint is ABSENT else read_checkpoint(checkpoint, cfg, matrix, start)
    interval = cfg["intervalMs"]
    compare_op = COMPARE_OPS[cfg["op"]]
    threshold = cfg["threshold"]
    final_states = []
    all_series = []
    total_steps = int(steps)
    for input_series in matrix["series"]:
        initial = resume["states"].get(input_series["id"]) if resume else None
        state = initial["state"] if initial else "inactive"
        pending_since = initial["pendingSince"] if initial else None
        hold_since = initial["holdSince"] if initial else None
        episode_start = initial["episodeStart"] if initial else None
        timeline = []
        transitions = []
        episodes = []
        first_firing_ms = episode_start
        firing_duration_ms = 0.0
        resolutions = 0
        missing_samples = 0
        nan_samples = 0
        held_evaluations = 0
        samples = input_series["samples"]
        for i in range(total_steps):
            t = start + i * interval
            sample = samples.get(t)
            if sample is None and cfg["missingData"] == "error":
                fail(f"series {input_series['id']}: missing sample at {number_to_string(t / 1000)} under missingData=error")
            if sample is None:
                missing_samples += 1
            is_nan = sample is not None and math.isnan(sample["value"])
            if is_nan:
                nan_samples += 1
            condition = compare_op(sample["value"], threshold) if sample is not None else False
            before = state
            if condition:
                hold_since = None
                if state == "inactive":
                    pending_since = t
                    state = "pending"
                if state == "pending" and t - pending_since >= cfg["forMs"]:
                    state = "firing"
                    episode_start = t
                    if first_firing_ms is None:
                        first_firing_ms = t
                if state == "firing":
                    reason = "condition_true" if before == "firing" else "for_elapsed"
                else:
                    reason = "waiting_for"
            else:
                reason = "condition_false" if sample is not None else "missing_as_false"
                if state == "firing":
                    if hold_since is None:
                        hold_since = t
                    if t - hold_since < cfg["holdMs"]:
                        held_evaluations += 1
                        reason = "keep_firing_for"
                    else:
                        episodes.append(_episode(episode_start, t, start, end, False))
                        episode_start = None
                        resolutions += 1
                        state = "inactive"
                        pending_since = None
                        hold_since = None
                        reason = "resolved"
                else:
                    state = "inactive"
                    pending_since = None
            if before != state:
                transitions.append({"timestamp": t / 1000, "from": before, "to": state, "reason": reason})
            timeline.append({"timestamp": t / 1000,
                             "value": sample["raw"] if sample is not None else None,
                             "sampleKind": ("nan" if is_nan else "value") if sample is not None else "missing",
                             "condition": condition, "state": state,
                             "pendingSince": _seconds(pending_since),
                             "holdSince": _seconds(hold_since), "reason": reason})
            # Observed exposure is [start,end), never an invented interval after end.
            if state == "firing" and i < total_steps - 1:
                firing_duration_ms += interval
        if episode_start is not None:
            episodes.append(_episode(episode_start, None, start, end, True))
        final_states.append({"labels": input_series["labels"], "state": state,
                             "pendingSince": _seconds(pending_since),
                             "holdSince": _seconds(hold_since),
                             "episodeStart": _seconds(episode_start)})
        summary = _measured({"firstFiring": _seconds(first_firing_ms),
                             "firingDurationSeconds": firing_duration_ms / 1000,
                             "firingEpisodes": len(episodes), "resolutions": resolutions,
                             "openAtEnd": state == "firing", "pendingAtEnd": state == "pending",
                             "missingSamples": missing_samples, "nanSamples": nan_samples,
                             "heldEvaluations": held_evaluations}, first_firing_ms, firing_duration_ms)
        all_series.append({"id": input_series["id"], "labels": input_series["labels"],
                           "timeline": timeline, "transitions": transitions, "episodes": episodes,
                           "summary": summary})
    summary: dict = {"series": len(all_series), "firstFiring": None, "firingDurationSeconds": 0,
                     "firingEpisodes": 0, "resolutions": 0, "openAtEnd": 0, "pendingAtEnd": 0,
                     "missingSamples": 0, "nanSamples": 0, "heldEvaluations": 0}
    aggregate_firing_ms = 0.0
    aggregate_first_ms = None
    for s in all_series:
        exact_first, exact_duration = _exact(s["summary"])
        aggregate_firing_ms += exact_duration
        if exact_first is not None:
            aggregate_first_ms = exact_first if aggregate_first_ms is None else min(aggregate_first_ms, exact_first)
        for key in summary:
            if key not in ("series", "firstFiring", "firingDurationSeconds"):
                summary[key] += js_number(s["summary"][key])
    summary["firingDurationSeconds"] = aggregate_firing_ms / 1000
    summary["firstFiring"] = _seconds(aggregate_first_ms)
    summary = _measured(summary, aggregate_first_ms, aggregate_firing_ms)
    return {
        "schemaVersion": 1, "command": "replay",
        "config": {"evaluationIntervalSeconds": interval / 1000,
                   "threshold": {"op": cfg["op"], "value": cfg["threshold"]},
                   "forSeconds": cfg["forMs"] / 1000, "keepFiringForSeconds": cfg["holdMs"] / 1000,
                   "missingData": cfg["missingData"]},
        "window": {"start": start / 1000, "end": end / 1000, "evaluations": steps,
                   "rangeSource": "sample-extrema" if cfg["startMs"] is None else "config"},
        "assumptions": [
            ("Initial alert state is supplied by an explicit contiguous checkpoint; no state before its originStart is inferred."
             if resume else "Each label set starts inactive; pre-capture alert state is unknown."),
            "Firing duration is observed exposure in [start,end); the final evaluation adds no duration. Open episodes are right-censored.",
            "No interpolation or lookback: absent grid samples follow missingData. JSON NaN is numeric NaN, not a distinguishable stale marker.",
            "Labels (including __name__) are preserved; no PromQL, alert label rewriting or Alertmanager behavior is simulated.",
        ],
        "sourceWarnings": matrix["warnings"], "sourceInfos": matrix["infos"],
        "summary": summary, "series": all_series,
        "checkpoint": {"schemaVersion": 1, "kind": "alert-flapsim/checkpoint", "config": checkpoint_rule(cfg),
                       "originStart": (resume["originStart"] if resume else start) / 1000,
                       "evaluatedThrough": end / 1000, "series": final_states},
    }


def replay(raw_matrix: Any, raw_config: Any, checkpoint: Any = ABSENT) -> dict:
    """Replay one alert rule over a saved matrix, optionally resuming a checkpoint."""
    return _run(parse_matrix(raw_matrix), config(raw_config), checkpoint)


def join(reports: Any) -> dict:
    """Verify 2..256 ordered checkpoint-chained replay reports and replay the whole capture."""
    return join_reports(reports, replay)


def _delta(a: dict, b: dict) -> dict:
    exact_a, exact_b = _exact(a), _exact(b)
    return {
        "firstFiringDelaySeconds": (None if exact_a[0] is None or exact_b[0] is None
                                    else (exact_b[0] - exact_a[0]) / 1000),
        "firingDurationSeconds": (exact_b[1] - exact_a[1]) / 1000,
        "firingEpisodes": b["firingEpisodes"] - a["firingEpisodes"],
        "resolutions": b["resolutions"] - a["resolutions"],
        "openAtEnd": js_number(b["openAtEnd"]) - js_number(a["openAtEnd"]),
    }


def compare(raw_matrix: Any, baseline_config: Any, candidate_config: Any) -> dict:
    """Replay two explicit configurations over one capture and report candidate-minus-baseline deltas."""
    matrix = parse_matrix(raw_matrix)
    baseline_cfg = config(baseline_config)
    candidate_cfg = config(candidate_config)
    a = _prepare(matrix, baseline_cfg)
    b = _prepare(matrix, candidate_cfg)
    if a["start"] != b["start"] or a["end"] != b["end"] or baseline_cfg["intervalMs"] != candidate_cfg["intervalMs"]:
        fail("compare: baseline and candidate must use identical evaluation grids and windows")
    baseline = _run(matrix, baseline_cfg)
    candidate = _run(matrix, candidate_cfg)
    return _comparison(baseline, candidate, "compare")


def _comparison(baseline: dict, candidate: dict, command: str) -> dict:
    return {"schemaVersion": 1, "command": command,
            "deltaConvention": "candidate minus baseline; null delay if either never fires",
            "baseline": baseline, "candidate": candidate,
            "delta": _delta(baseline["summary"], candidate["summary"]),
            "seriesDeltas": [{"id": s["id"], "labels": s["labels"],
                              **_delta(s["summary"], candidate["series"][i]["summary"])}
                             for i, s in enumerate(baseline["series"])]}


# Describe and preflight the entire group before regenerating any replay.
# Baseline is verified once; every candidate must share its exact raw capture.
def _verify_report_group(baseline_report: Any, candidate_reports: list, command: str) -> dict:
    a = describe_report(baseline_report, f"{command} baseline")
    candidates = [describe_report(raw, f"{command} candidate{'' if command == 'compare-reports' else f'[{i}]'}")
                  for i, raw in enumerate(candidate_reports)]
    for b in candidates:
        if a["start"] != b["start"] or a["end"] != b["end"] or a["cfg"]["intervalMs"] != b["cfg"]["intervalMs"]:
            fail(f"{command}: baseline and candidate must use identical evaluation grids and windows")
        equal([s["id"] for s in a["series"]], [s["id"] for s in b["series"]], f"{command} exact label sets")
    evaluations = 0
    for w in [a, *candidates]:
        raw_checkpoint, raw_window = w["raw"]["checkpoint"], w["raw"]["window"]
        if not js_eq(raw_checkpoint["originStart"], raw_window["start"]):
            fail(f"{w['where']}: report must start at checkpoint originStart; missing origin history")
        evaluations += w["steps"] * max(1, len(w["series"]))
        if evaluations > MAX_REPORT_EVALUATIONS:
            fail(f"{command}: combined timeline exceeds {MAX_REPORT_EVALUATIONS} series-evaluations")
        if not is_safe_integer((w["end"] - w["start"]) * max(1, len(w["series"]))):
            fail(f"{command}: aggregate duration exceeds safe integer milliseconds")
    baseline = verify_report(a, replay)["report"]
    reports = []
    for b in candidates:
        candidate = verify_report(b, replay)["report"]
        for i, s in enumerate(baseline["series"]):
            for j, point in enumerate(s["timeline"]):
                if point["value"] != candidate["series"][i]["timeline"][j]["value"]:
                    fail(f"{command}: identical raw samples required for label {s['id']} at {number_to_string(point['timestamp'])}")
        reports.append(candidate)
    return {"baseline": baseline, "candidates": reports, "evaluations": evaluations}


def compare_reports(baseline_report: Any, candidate_report: Any) -> dict:
    """Verify two complete-origin saved replay reports and compare them."""
    group = _verify_report_group(baseline_report, [candidate_report], "compare-reports")
    return _comparison(group["baseline"], group["candidates"][0], "compare-reports")


_NO_OPTIONS: Any = object()


def compare_report_set(baseline_report: Any, candidate_reports: Any, options: Any = _NO_OPTIONS) -> dict:
    """Verify one baseline and 1..255 candidate saved reports against one raw capture."""
    if options is _NO_OPTIONS:
        options = {}
    keys(options, ["detail"], [], "compare-report-set options")
    detail = options["detail"] if "detail" in options else "full"
    if not isinstance(detail, str) or detail not in ("full", "summary"):
        fail("compare-report-set options.detail: expected full or summary")
    if not isinstance(candidate_reports, list) or len(candidate_reports) < 1 or len(candidate_reports) > 255:
        fail("compare-report-set: expected an array of 1 to 255 candidate replay reports")
    group = _verify_report_group(baseline_report, candidate_reports, "compare-report-set")
    baseline, candidates = group["baseline"], group["candidates"]
    result_candidates = []
    for candidate_index, report in enumerate(candidates):
        comparison = _comparison(baseline, report, "compare-report-set")
        result_candidates.append({"candidateIndex": candidate_index, "report": report,
                                  "delta": comparison["delta"], "seriesDeltas": comparison["seriesDeltas"]})
    result = {
        "schemaVersion": 1, "command": "compare-report-set",
        "deltaConvention": "candidate minus baseline; null delay if either never fires",
        "budget": {"maxReports": 256, "reports": len(candidates) + 1, "candidates": len(candidates),
                   "maxEvaluations": MAX_REPORT_EVALUATIONS, "requiredEvaluations": group["evaluations"]},
        "baseline": baseline,
        "candidates": result_candidates,
    }
    return _report_set_summary(result) if detail == "summary" else result


# Project only after the complete group has passed replay/capture validation and
# exact-ms deltas have been calculated. This is output, never an import format.
def _report_set_summary(result: dict) -> dict:
    def compact(report: dict) -> dict:
        series = [{"id": s["id"], "labels": s["labels"],
                   "summary": {**s["summary"], "transitionCount": len(s["transitions"]),
                               "censoredEpisodes": sum(1 for e in s["episodes"] if e["censored"])}}
                  for s in report["series"]]
        return {"schemaVersion": 1, "command": "replay-summary", "config": report["config"],
                "exactConfig": report["checkpoint"]["config"], "ruleIdentity": rule_identity(report),
                "window": report["window"],
                "origin": {"originStart": report["checkpoint"]["originStart"],
                           "evaluatedThrough": report["checkpoint"]["evaluatedThrough"]},
                "assumptions": report["assumptions"], "sourceWarnings": report["sourceWarnings"],
                "sourceInfos": report["sourceInfos"],
                "summary": {**report["summary"],
                            "transitionCount": sum(s["summary"]["transitionCount"] for s in series),
                            "censoredEpisodes": sum(s["summary"]["censoredEpisodes"] for s in series)},
                "series": series}

    def extra_delta(d: dict, a: dict, b: dict) -> dict:
        return {**d, "pendingAtEnd": js_number(b["pendingAtEnd"]) - js_number(a["pendingAtEnd"]),
                "transitionCount": b["transitionCount"] - a["transitionCount"],
                "censoredEpisodes": b["censoredEpisodes"] - a["censoredEpisodes"]}

    baseline = compact(result["baseline"])
    candidates = []
    for c in result["candidates"]:
        report = compact(c["report"])
        candidates.append({"candidateIndex": c["candidateIndex"], "report": report,
                           "delta": extra_delta(c["delta"], baseline["summary"], report["summary"]),
                           "seriesDeltas": [extra_delta(d, baseline["series"][i]["summary"], report["series"][i]["summary"])
                                            for i, d in enumerate(c["seriesDeltas"])]})
    return {**result, "detail": "summary", "captureIdentity": capture_identity(result["baseline"]),
            "baseline": baseline, "candidates": candidates}


MAX_GRID_COMBINATIONS = 256
MAX_GRID_EVALUATIONS = 1_000_000


def _timer_grid(raw: Any) -> dict:
    fields = ["for", "keep_firing_for", "maxEvaluations"]
    keys(raw, fields, fields, "grid")
    budget = raw["maxEvaluations"]
    if not is_safe_integer(budget) or budget < 1 or budget > MAX_GRID_EVALUATIONS:
        fail(f"grid.maxEvaluations: expected an integer from 1 to {MAX_GRID_EVALUATIONS}")
    for field in ("for", "keep_firing_for"):
        if not isinstance(raw[field], list) or len(raw[field]) < 1 or len(raw[field]) > MAX_GRID_COMBINATIONS:
            fail(f"grid.{field}: expected an array of 1 to {MAX_GRID_COMBINATIONS} durations")
    combinations = len(raw["for"]) * len(raw["keep_firing_for"])
    if combinations > MAX_GRID_COMBINATIONS:
        fail(f"grid: Cartesian product exceeds {MAX_GRID_COMBINATIONS} combinations")

    def values(field: str) -> list:
        result = []
        seen: set[float] = set()
        for i, value in enumerate(raw[field]):
            ms = duration(value, f"grid.{field}[{i}]")
            if ms in seen:
                fail(f"grid.{field}: duplicate normalized duration {number_to_string(ms)}ms")
            seen.add(ms)
            result.append(ms)
        return result

    return {"forValues": values("for"), "holdValues": values("keep_firing_for"),
            "combinations": combinations, "budget": budget}


def _with_transitions(summary: dict, transition_count: int) -> _Summary:
    copy = _Summary({**summary, "transitionCount": transition_count})
    copy.exact = _exact(summary)
    return copy


def _grid_summary(report: dict) -> dict:
    series = [{"id": s["id"], "labels": s["labels"],
               "summary": _with_transitions(s["summary"], len(s["transitions"])),
               "transitions": s["transitions"], "episodes": s["episodes"]}
              for s in report["series"]]
    return {"config": report["config"],
            "summary": _with_transitions(report["summary"], sum(s["summary"]["transitionCount"] for s in series)),
            "series": series}


def _grid_delta(a: dict, b: dict) -> dict:
    return {**_delta(a, b), "transitionCount": b["transitionCount"] - a["transitionCount"]}


# Compare only explicit timer choices; never rank, tune, or infer an optimum.
def grid(raw_matrix: Any, baseline_config: Any, raw_grid: Any) -> dict:
    """Replay the Cartesian product of explicit for/keep_firing_for values against a baseline."""
    timers = _timer_grid(raw_grid)
    matrix = parse_matrix(raw_matrix)
    cfg = config(baseline_config)
    steps = _prepare(matrix, cfg)["steps"]
    # Charge even an empty matrix for its window; the baseline is an extra replay,
    # including when its timers also appear in the Cartesian product.
    combinations, budget = timers["combinations"], timers["budget"]
    evaluations_per_replay = steps * max(1, len(matrix["series"]))
    requested = evaluations_per_replay * (combinations + 1)
    if requested > budget:
        fail(f"grid: requires {number_to_string(requested)} series-evaluations including baseline; "
             f"exceeds budget {number_to_string(budget)}")
    # Validate the entire budget before evaluating any alert state.
    baseline_report = _run(matrix, cfg)
    baseline = _grid_summary(baseline_report)
    candidates = []
    for for_ms in timers["forValues"]:
        for hold_ms in timers["holdValues"]:
            candidate = _grid_summary(_run(matrix, {**cfg, "forMs": for_ms, "holdMs": hold_ms}))
            candidate["delta"] = _grid_delta(baseline["summary"], candidate["summary"])
            for i, s in enumerate(candidate["series"]):
                s["delta"] = _grid_delta(baseline["series"][i]["summary"], s["summary"])
            candidates.append(candidate)
    return {"schemaVersion": 1, "command": "grid",
            "deltaConvention": "candidate minus explicit baseline; null delay if either never fires",
            "window": baseline_report["window"],
            "budget": {"maxEvaluations": budget, "hardMaxEvaluations": MAX_GRID_EVALUATIONS,
                       "maxCombinations": MAX_GRID_COMBINATIONS, "combinations": combinations,
                       "replays": combinations + 1, "evaluationsPerReplay": evaluations_per_replay,
                       "requestedEvaluations": requested},
            "assumptions": [*baseline_report["assumptions"],
                            "Finite-capture descriptive tradeoffs only: no statistical or production-performance claim and no universal optimum. Candidates retain user order; none is selected.",
                            "Transition counts include all state changes, including pending; first-fire deltas compare observed first firings, not unseen pre-capture activation."],
            "sourceWarnings": matrix["warnings"], "sourceInfos": matrix["infos"],
            "baseline": baseline, "candidates": candidates}


# Accept original inputs so CSV shares all grid validation, rather than trusting
# saved/mutated summaries. Read exact timer milliseconds from duration strings;
# normalized seconds in JSON can lose precision at the safe-integer-ms limit.
def grid_csv(raw_matrix: Any, baseline_config: Any, raw_grid: Any) -> str:
    """Grid comparison as CRLF CSV with baseline/candidate aggregate and per-label rows."""
    report = grid(raw_matrix, baseline_config, raw_grid)
    baseline_timers = [duration(baseline_config["for"], "config.for"),
                       duration(baseline_config["keep_firing_for"], "config.keep_firing_for")]
    candidate_timers = [[duration(f, "grid.for"), duration(h, "grid.keep_firing_for")]
                        for f in raw_grid["for"] for h in raw_grid["keep_firing_for"]]
    return format_grid_csv(report, duration(baseline_config["evaluationInterval"], "config.evaluationInterval", True),
                           baseline_timers, candidate_timers)


def _config_timers(cfg: dict, prefix: str) -> list:
    return [duration(cfg["for"], f"{prefix}.for"), duration(cfg["keep_firing_for"], f"{prefix}.keep_firing_for")]


# Compare the original inputs first so CSV has the same grid/window constraints
# and atomic replay errors as JSON. Keep each role's own rule/range context.
def compare_csv(raw_matrix: Any, baseline_config: Any, candidate_config: Any) -> str:
    """``compare`` as CSV."""
    report = compare(raw_matrix, baseline_config, candidate_config)
    return _comparison_csv(report, duration(baseline_config["evaluationInterval"], "config.evaluationInterval", True),
                           _config_timers(baseline_config, "config"), _config_timers(candidate_config, "config"))


# Saved reports must pass the complete-origin/capture checks and combined bounds
# before formatting. Verified checkpoints retain exact integer-ms rule identity.
def compare_reports_csv(baseline_report: Any, candidate_report: Any) -> str:
    """``compare_reports`` as CSV."""
    report = compare_reports(baseline_report, candidate_report)
    baseline = report["baseline"]["checkpoint"]["config"]
    candidate = report["candidate"]["checkpoint"]["config"]
    return _comparison_csv(report, duration(baseline["evaluationInterval"], "checkpoint.config.evaluationInterval", True),
                           _config_timers(baseline, "checkpoint.config"), _config_timers(candidate, "checkpoint.config"))


# Separate opt-in schema; reuse full-set verification and exact summary deltas.
def compare_report_set_summary_csv(baseline_report: Any, candidate_reports: Any) -> str:
    """Compact report-set summary as the separately versioned v1 summary CSV."""
    return format_report_set_summary_csv(compare_report_set(baseline_report, candidate_reports, {"detail": "summary"}))


def compare_report_set_csv(baseline_report: Any, candidate_reports: Any) -> str:
    """Full report-set comparison as CSV (same schema as compare/grid CSV)."""
    report = compare_report_set(baseline_report, candidate_reports)
    base = report["baseline"]["checkpoint"]["config"]
    return _report_set_csv(report["baseline"], [c["report"] for c in report["candidates"]],
                           duration(base["evaluationInterval"], "checkpoint.config.evaluationInterval", True),
                           _config_timers(base, "checkpoint.config"),
                           [_config_timers(c["report"]["checkpoint"]["config"], "checkpoint.config")
                            for c in report["candidates"]])


def _comparison_csv(report: dict, interval_ms: float, baseline_timers: list, candidate_timers: list) -> str:
    return _report_set_csv(report["baseline"], [report["candidate"]], interval_ms, baseline_timers, [candidate_timers])


def _report_set_csv(baseline_report: dict, candidate_reports: list, interval_ms: float,
                    baseline_timers: list, candidate_timers: list) -> str:
    baseline = {**_grid_summary(baseline_report), "window": baseline_report["window"]}
    candidates = []
    for report in candidate_reports:
        candidate = {**_grid_summary(report), "window": report["window"]}
        candidate["delta"] = _grid_delta(baseline["summary"], candidate["summary"])
        for i, s in enumerate(candidate["series"]):
            s["delta"] = _grid_delta(baseline["series"][i]["summary"], s["summary"])
        candidates.append(candidate)
    return format_grid_csv({"baseline": baseline, "candidates": candidates}, interval_ms, baseline_timers, candidate_timers)
