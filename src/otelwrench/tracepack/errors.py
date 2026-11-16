# SPDX-License-Identifier: Apache-2.0
"""Error types shared by the tracepack library and CLI."""
from __future__ import annotations

from typing import Any


class TracepackError(Exception):
    """A validation, verification or I/O failure; ``str(error)`` is the message."""

    @property
    def message(self) -> str:
        return str(self.args[0]) if self.args else ""


class JSONSyntaxError(TracepackError, ValueError):
    """Malformed or unsafe JSON text."""


class StrictLossError(TracepackError):
    """``strictLoss`` refused a conversion, bundle or verified bundle with losses."""

    code = "STRICT_LOSS"

    def __init__(self, report: dict[str, Any]):
        super().__init__(f"strict-loss rejected {len(report['losses'])} loss(es)")
        self.report = report
