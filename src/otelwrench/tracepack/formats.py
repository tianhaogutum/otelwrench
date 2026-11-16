# SPDX-License-Identifier: Apache-2.0
"""Source-independent format checks shared by library conversion and CLI preflight."""
from __future__ import annotations

from typing import Any

from .model import fail

SOURCES = ("auto", "otlp", "jaeger", "zipkin")
TARGETS = ("otlp", "jaeger", "zipkin")


def _one_of(value: Any, allowed: tuple[str, ...]) -> bool:
    return isinstance(value, str) and value in allowed


def validate_formats(from_: Any, to: Any) -> None:
    if not _one_of(from_, SOURCES):
        fail("--from", "expected auto, otlp, jaeger or zipkin")
    if not _one_of(to, TARGETS):
        fail("--to", "expected otlp, jaeger or zipkin")
