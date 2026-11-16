# SPDX-License-Identifier: Apache-2.0
"""Opt-in count and error-fraction observation gates."""
from __future__ import annotations

from .budget import exceeds, rational
from .jsontext import is_finite, is_safe_integer, js_sorted
from .validate import check, object_

_THRESHOLDS = {"countGate": "maxChangePercent", "errorFractionGate": "maxIncreasePercent"}
OBSERVATION_GATE_KEYS = list(_THRESHOLDS)


def _absolute_key(key: str) -> str:
    return "maxChangeCount" if key == "countGate" else "maxIncreasePercentagePoints"


def validate_observation_gate(value, key: str) -> dict:
    path, threshold = f"config.{key}", _THRESHOLDS[key]
    absolute_key = _absolute_key(key)
    object_(value, path, [threshold, "minSamples", absolute_key])
    policy = {"minSamples": 20, **value}
    t = policy.get(threshold)
    check(is_finite(t) and t >= 0, f"{path}.{threshold}", "expected finite nonnegative number")
    if absolute_key in value:
        a = policy[absolute_key]
        if key == "countGate":
            check(is_safe_integer(a) and a >= 0, f"{path}.{absolute_key}", "expected nonnegative safe integer")
        else:
            check(is_finite(a) and a >= 0, f"{path}.{absolute_key}", "expected finite nonnegative number")
    m = policy["minSamples"]
    check(is_safe_integer(m) and m >= 1, f"{path}.minSamples", "expected positive safe integer")
    return policy


def compare_observation_gate(baseline: dict, candidate: dict, key: str, policy: dict) -> list:
    count_gate = key == "countGate"
    budget = rational(policy[_THRESHOLDS[key]])
    absolute_key = _absolute_key(key)
    change_key = "changeCount" if count_gate else "changePercentagePoints"
    absolute_budget = rational(policy[absolute_key]) if absolute_key in policy else None
    keys = js_sorted(dict.fromkeys([*baseline["groups"], *candidate["groups"]]))
    rows = []
    for k in keys:
        b, c = baseline["groups"].get(k), candidate["groups"].get(k)
        if count_gate:
            baseline_value = b["count"] if b is not None else 0
            candidate_value = c["count"] if c is not None else 0
        else:
            baseline_value = b["errorCount"] / b["count"] if b is not None else None
            candidate_value = c["errorCount"] / c["count"] if c is not None else None
        entry = {"group": (b if b is not None else c)["group"],
                 "baselineCount": b["count"] if b is not None else 0,
                 "candidateCount": c["count"] if c is not None else 0,
                 "baselineValue": baseline_value, "candidateValue": candidate_value,
                 "changePercent": None, "relativeState": "unpaired", "eligible": False, "failed": False,
                 **({change_key: None, "absoluteExceeded": False, "relativeExceeded": False}
                    if absolute_budget is not None else {})}
        # Absence is not a measured zero error fraction or a paired count change.
        if b is None or c is None:
            rows.append({**entry, "reason": "new-group" if b is None else "removed-group"})
            continue
        bn, cn = b["count"], c["count"]
        numerator = cn - bn if count_gate else c["errorCount"] * bn - b["errorCount"] * cn
        denominator = bn if count_gate else b["errorCount"] * cn
        zero_baseline = denominator == 0
        relative_state = ("infinite" if numerator > 0 else "zero-to-zero") if zero_baseline else "finite"
        change_percent = (None if numerator > 0 else 0) if zero_baseline \
            else float(numerator) / float(denominator) * 100
        eligible = b["count"] >= policy["minSamples"] and c["count"] >= policy["minSamples"]
        # Count budgets bound change in either direction. Error budgets only bound
        # increases in the fraction; cross multiplication accounts for unequal n.
        magnitude = -numerator if count_gate and numerator < 0 else numerator
        over = relative_state == "infinite" or (not zero_baseline and exceeds(magnitude * 100, denominator, budget))
        equal = not zero_baseline and magnitude * 100 * budget[1] == budget[0] * denominator
        if absolute_budget is not None:
            # Counts bound the integer magnitude; error fractions bound the signed
            # percentage-point increase. Keep both exact until report conversion.
            absolute_numerator = magnitude if count_gate else numerator * 100
            absolute_denominator = 1 if count_gate else bn * cn
            absolute_exceeded = exceeds(absolute_numerator, absolute_denominator, absolute_budget)
            absolute_equal = absolute_numerator * absolute_budget[1] == absolute_budget[0] * absolute_denominator
            entry[change_key] = numerator if count_gate \
                else float(numerator) / float(absolute_denominator) * 100
            entry["absoluteExceeded"] = absolute_exceeded
            entry["relativeExceeded"] = over
            # A boundary is decisive only when the other budget is met or exceeded.
            equal = ((over or equal) and absolute_equal) or (equal and absolute_exceeded)
            over = over and absolute_exceeded
        if not eligible:
            reason = "insufficient-samples"
        elif relative_state == "zero-to-zero":
            reason = "zero-to-zero"
        elif over:
            reason = "zero-baseline-increase" if relative_state == "infinite" else "threshold-exceeded"
        else:
            reason = "at-threshold" if equal else "within-budget"
        rows.append({**entry, "changePercent": change_percent, "relativeState": relative_state,
                     "eligible": eligible, "failed": eligible and over, "reason": reason})
    return rows
