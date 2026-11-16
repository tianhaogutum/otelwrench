# SPDX-License-Identifier: Apache-2.0
"""Offline per-label-set alert state replay and config comparison for captured Prometheus matrices.

Library API (all inputs are parsed JSON values; outputs are JSON-ready dicts or CSV text):
``replay``, ``join``, ``compare``, ``compare_csv``, ``compare_reports``, ``compare_reports_csv``,
``compare_report_set``, ``compare_report_set_csv``, ``compare_report_set_summary_csv``,
``grid``, ``grid_csv``, ``parse_matrix``, ``parse_json``. Use ``to_json`` for byte-identical
serialization of reports (JavaScript number formatting and key order).
"""
from __future__ import annotations

from ._js import stringify as to_json
from .api import (
    compare,
    compare_csv,
    compare_report_set,
    compare_report_set_csv,
    compare_report_set_summary_csv,
    compare_reports,
    compare_reports_csv,
    grid,
    grid_csv,
    join,
    replay,
)
from .jsonparse import parse_json
from .matrix import parse_matrix
from .validation import FlapsimError

__all__ = [
    "FlapsimError", "compare", "compare_csv", "compare_report_set", "compare_report_set_csv",
    "compare_report_set_summary_csv", "compare_reports", "compare_reports_csv", "grid", "grid_csv",
    "join", "parse_json", "parse_matrix", "replay", "to_json",
]
