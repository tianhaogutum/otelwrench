# SPDX-License-Identifier: Apache-2.0
"""Versioned SHA-256 identity of one exact alert rule (``alert-flapsim-rule-v1``)."""
from __future__ import annotations

import hashlib

from ._js import node_utf8, number_to_string, stringify
from .validation import config


# Private: called only for freshly regenerated reports after the entire saved
# set passes validation/preflight. Imported identity claims are never accepted.
def rule_identity(report: dict) -> dict:
    encoding = "alert-flapsim-rule-v1"
    cfg = config(report["checkpoint"]["config"])
    text = stringify([encoding, number_to_string(cfg["intervalMs"]), number_to_string(cfg["forMs"]),
                      number_to_string(cfg["holdMs"]), cfg["op"], cfg["threshold"], cfg["missingData"]])
    # Finite thresholds use ECMAScript JSON number serialization. In particular,
    # -0 becomes 0, matching the evaluator's comparison semantics for all ops.
    return {"schemaVersion": 1, "algorithm": "sha256", "encoding": encoding,
            "digest": hashlib.sha256(node_utf8(text)).hexdigest()}
