# SPDX-License-Identifier: Apache-2.0
"""Explicit alert-state checkpoints for contiguous resumed replays."""
from __future__ import annotations

import math
from typing import Any

from ._js import is_safe_integer, js_eq, number_to_string
from .matrix import label_identity
from .validation import config, fail, keys, timestamp

RULE_FIELDS = ["evaluationInterval", "threshold", "for", "keep_firing_for", "missingData"]


def checkpoint_rule(cfg: dict) -> dict:
    return {"evaluationInterval": f"{number_to_string(cfg['intervalMs'])}ms",
            "threshold": {"op": cfg["op"], "value": cfg["threshold"]},
            "for": f"{number_to_string(cfg['forMs'])}ms",
            "keep_firing_for": f"{number_to_string(cfg['holdMs'])}ms",
            "missingData": cfg["missingData"]}


# State is an explicit assertion from a prior replay, never inferred from new samples.
def read_checkpoint(raw: Any, cfg: dict, matrix: dict, start: float) -> dict:
    fields = ["schemaVersion", "kind", "config", "originStart", "evaluatedThrough", "series"]
    keys(raw, fields, fields, "checkpoint")
    if not js_eq(raw["schemaVersion"], 1) or not js_eq(raw["kind"], "alert-flapsim/checkpoint"):
        fail("checkpoint: unsupported kind or schemaVersion")
    keys(raw["config"], RULE_FIELDS, RULE_FIELDS, "checkpoint.config")
    prior = config(raw["config"])
    for field in ("intervalMs", "forMs", "holdMs", "op", "threshold", "missingData"):
        if not js_eq(prior[field], cfg[field]):
            fail("checkpoint: alert settings must match, including evaluationInterval")
    origin_start = timestamp(raw["originStart"], "checkpoint.originStart")
    through = timestamp(raw["evaluatedThrough"], "checkpoint.evaluatedThrough")
    span = through - origin_start
    if not is_safe_integer(span) or span < 0 or math.fmod(span, cfg["intervalMs"]) != 0:
        fail("checkpoint: incompatible origin/evaluation grid")
    if cfg["startMs"] is None:
        fail("checkpoint: resume requires explicit start and end")
    if start - through != cfg["intervalMs"]:
        fail("checkpoint: next window must be contiguous (start = evaluatedThrough + evaluationInterval); "
             "gaps and overlaps are rejected")
    # Keep timer arithmetic exact across the entire chain, not just the new window.
    if not is_safe_integer(cfg["endMs"] - origin_start):
        fail("checkpoint: total capture span exceeds safe integer milliseconds")
    if not isinstance(raw["series"], list):
        fail("checkpoint.series: expected an array")
    states: dict[str, dict] = {}
    for i, entry in enumerate(raw["series"]):
        where = f"checkpoint.series[{i}]"
        entry_fields = ["labels", "state", "pendingSince", "holdSince", "episodeStart"]
        keys(entry, entry_fields, entry_fields, where)
        sid = label_identity(entry["labels"], f"{where}.labels")["id"]
        if sid in states:
            fail(f"{where}: duplicate canonical label set")
        timers: dict[str, Any] = {}
        for field in ("pendingSince", "holdSince", "episodeStart"):
            t = None if entry[field] is None else timestamp(entry[field], f"{where}.{field}")
            if t is not None and (t < origin_start or t > through or math.fmod(t - origin_start, cfg["intervalMs"]) != 0):
                fail(f"{where}.{field}: timer outside known capture or incompatible grid")
            timers[field] = t
        pending, hold, episode = timers["pendingSince"], timers["holdSince"], timers["episodeStart"]
        state = entry["state"]
        valid = False
        if js_eq(state, "inactive"):
            valid = pending is None and hold is None and episode is None
        if js_eq(state, "pending"):
            valid = pending is not None and hold is None and episode is None and through - pending < cfg["forMs"]
        if js_eq(state, "firing") and pending is not None and episode is not None:
            wait = episode - pending
            valid = (wait >= cfg["forMs"] and wait - cfg["intervalMs"] < cfg["forMs"]
                     and (hold is None or (hold > episode and through - hold < cfg["holdMs"])))
        if not valid:
            fail(f"{where}: malformed or inconsistent alert state/timers")
        states[sid] = {"state": state, **timers}
    if len(states) != len(matrix["series"]) or any(s["id"] not in states for s in matrix["series"]):
        fail("checkpoint: exact label sets must match the new capture (including __name__); "
             "no added or omitted series")
    return {"originStart": origin_start, "states": states}
