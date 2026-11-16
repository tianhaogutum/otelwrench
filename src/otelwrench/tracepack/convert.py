# SPDX-License-Identifier: Apache-2.0
"""Format detection and conversion with an explicit source-path loss report."""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from .errors import StrictLossError
from .formats import validate_formats
from .jsonfmt import UNDEFINED, parse_json
from .model import LossCollector, fail, normalize
from .readers import read_jaeger, read_otlp, read_zipkin
from .writers import write_jaeger, write_otlp, write_zipkin

READERS = {"otlp": read_otlp, "jaeger": read_jaeger, "zipkin": read_zipkin}
WRITERS = {"otlp": write_otlp, "jaeger": write_jaeger, "zipkin": write_zipkin}

# Python keyword spellings accepted in addition to the original camelCase option keys.
_KEYWORDS = {"from_": "from", "strict_loss": "strictLoss", "zipkin_error_tag": "zipkinErrorTag",
             "to": "to", "source": "source", "report": "report"}


def merge_options(options: Any, keywords: dict[str, Any]) -> Any:
    """Combine a camelCase options mapping with snake_case keyword arguments."""
    if not keywords:
        return options
    merged: dict[str, Any] = dict(options) if isinstance(options, Mapping) else {}
    for key, value in keywords.items():
        if key not in _KEYWORDS:
            raise TypeError(f"unexpected keyword argument {key!r}")
        merged[_KEYWORDS[key]] = value
    return merged


def option(options: Mapping[str, Any], key: str, default: Any) -> Any:
    """Destructuring with a default: absent or ``UNDEFINED`` values use ``default``.

    The value is read exactly once (``options[key]``), so mapping subclasses that
    observe reads see the same access pattern as the original implementation.
    """
    if key not in options:
        return default
    value = options[key]
    return default if value is UNDEFINED else value


def js_truthy(value: Any) -> bool:
    if value is None or value is UNDEFINED or value is False:
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return not (value == 0 or (isinstance(value, float) and math.isnan(value)))
    if isinstance(value, str):
        return value != ""
    return True


def detect(source: Any) -> str:
    if isinstance(source, list):
        return "zipkin"
    if isinstance(source, Mapping):
        formats = [k for k in ("resourceSpans", "data") if k in source]
        if len(formats) == 1:
            return "jaeger" if formats[0] == "data" else "otlp"
    fail("$", "cannot detect format; expected resourceSpans, Jaeger {data:[...]}, or Zipkin v2 array")


def convert(text: str, options: Mapping[str, Any] | None = None, **keywords: Any) -> dict[str, Any]:
    """Convert one saved trace document.

    ``options`` uses the original keys (``from``, ``to``, ``strictLoss``,
    ``zipkinErrorTag``); keyword spellings ``from_``, ``to``, ``strict_loss`` and
    ``zipkin_error_tag`` are also accepted. Returns ``{"output": ..., "report": ...}``.
    """
    opts = merge_options(options, keywords)
    if not isinstance(opts, Mapping):
        opts = {}
    from_ = option(opts, "from", "auto")
    to = option(opts, "to", UNDEFINED)
    strict_loss = option(opts, "strictLoss", False)
    zipkin_error_tag = option(opts, "zipkinErrorTag", False)
    validate_formats(from_, to)
    if zipkin_error_tag is not True and zipkin_error_tag is not False:
        fail("options.zipkinErrorTag", "expected boolean")
    source = parse_json(text)
    detected = detect(source) if from_ == "auto" else from_
    if zipkin_error_tag and (detected not in ("otlp", "jaeger") or to != "zipkin"):
        fail("--zipkin-error-tag", "only supported for OTLP or Jaeger to Zipkin")
    loss = LossCollector()
    read = READERS[detected](source, loss)
    if zipkin_error_tag:
        # Validate every source span before duplicate collapse, including dropped tags.
        for s in read:
            error = s["attrs"].get("error")
            if s["status"]["code"] == 2 and s["paths"].get("errorAttribute") and (error is None or error["type"] != "string"):
                fail(s["paths"]["errorAttribute"], "nonstring error attribute collides with --zipkin-error-tag mapping")
    spans = normalize(read, loss)
    output = WRITERS[to](spans, loss, zipkin_error_tag=zipkin_error_tag)
    report = {"from": detected, "to": to, "spanCount": len(spans), "losses": loss.list()}
    if js_truthy(strict_loss) and report["losses"]:
        raise StrictLossError(report)
    return {"output": output, "report": report}
