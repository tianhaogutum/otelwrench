# SPDX-License-Identifier: Apache-2.0
"""Offline OTLP and Jaeger corpus comparison with operation-level latency budgets.

Python port of the original ``tracediff`` tool. Reports (JSON field order,
number formatting, text lines) and exit codes are unchanged; only the program
name in usage and error messages is now ``otelwrench tracediff``.
"""
from .core import DEFAULT_POLICY, compare, render_text, validate_policy
from .jsontext import InputError, parse_json
from .normalize import normalize

__version__ = "1.0.0"
__all__ = ["DEFAULT_POLICY", "InputError", "compare", "normalize", "parse_json", "render_text", "validate_policy"]
