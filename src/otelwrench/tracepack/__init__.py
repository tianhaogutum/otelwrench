# SPDX-License-Identifier: Apache-2.0
"""tracepack: offline, precision-safe saved-trace conversion with explicit loss reports.

Converts saved OTLP JSON, Jaeger UI exports and Zipkin v2 JSON into each other and
creates self-contained ``tracepack-fixture`` bundles that replay offline.
"""
from .bundle import BUNDLE_LIMIT, SOURCE_LIMIT, create_bundle, extract_bundle, verify_bundle
from .convert import convert, detect
from .errors import JSONSyntaxError, StrictLossError, TracepackError
from .jsonfmt import UNDEFINED, DecimalToken, JSBigInt, canonical_json, parse_json, stringify

__all__ = [
    "BUNDLE_LIMIT", "SOURCE_LIMIT", "UNDEFINED", "DecimalToken", "JSBigInt", "JSONSyntaxError", "StrictLossError",
    "TracepackError", "canonical_json", "convert", "create_bundle", "detect", "extract_bundle", "parse_json",
    "stringify", "verify_bundle",
]
