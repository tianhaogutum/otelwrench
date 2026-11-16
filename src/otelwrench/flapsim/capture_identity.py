# SPDX-License-Identifier: Apache-2.0
"""Versioned SHA-256 identity of one raw capture (``alert-flapsim-capture-v1``)."""
from __future__ import annotations

import hashlib
import json

from ._js import node_utf8, number_to_string, stringify
from .validation import duration, timestamp


# Private: only a freshly regenerated, canonical complete-origin report after
# the ENTIRE comparison group passes validation. Never trust an imported hash.
# Version 1 bytes are documented; arrays avoid object-key ordering, integer
# strings avoid seconds precision loss, and null is distinct from NaN.
def capture_identity(report: dict) -> dict:
    encoding = "alert-flapsim-capture-v1"
    header = [encoding, number_to_string(timestamp(report["window"]["start"])),
              number_to_string(timestamp(report["window"]["end"])),
              number_to_string(duration(report["checkpoint"]["config"]["evaluationInterval"])),
              number_to_string(report["window"]["evaluations"])]
    digest = hashlib.sha256()
    digest.update(node_utf8(stringify(header)[:-1] + ",["))
    for i, series in enumerate(report["series"]):
        if i:
            digest.update(b",")
        # Canonical id already contains UTF-16-sorted label pairs, including numeric
        # label names. Reports are sorted by that id during verified replay.
        digest.update(node_utf8(stringify([json.loads(series["id"]), [p["value"] for p in series["timeline"]]])))
    digest.update(b"]]")
    return {"schemaVersion": 1, "algorithm": "sha256", "encoding": encoding, "digest": digest.hexdigest()}
