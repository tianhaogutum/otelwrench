# SPDX-License-Identifier: Apache-2.0
"""Self-contained reproducibility fixtures (``tracepack-fixture`` v1 bundles).

A bundle is a reproducibility fixture, not a signature or source attestation. The
on-disk format is unchanged from the original tool: ``converter.name`` stays
``"tracepack"`` and ``converter.version`` stays ``"0.1.0"`` so existing bundles verify.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import re
from collections.abc import Mapping
from typing import Any

from .convert import convert, merge_options, option
from .errors import StrictLossError, TracepackError
from .jsonfmt import UNDEFINED, canonical_json, js_keys, parse_json, utf16_len
from .model import fail, object_

SOURCE_LIMIT = 64 * 1024 * 1024
BUNDLE_LIMIT = 256 * 1024 * 1024
FORMAT = "tracepack-fixture"
VERSION = 1
NORMALIZATION_VERSION = 1
CONVERTER_NAME = "tracepack"
CONVERTER_VERSION = "0.1.0"
_DIGEST = re.compile(r"[0-9a-f]{64}")
_INVALID_UTF8 = "The encoded data was not valid for encoding utf-8"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_hash(value: Any) -> str:
    return _sha(canonical_json(value).encode("utf-8"))


def _converter() -> dict[str, Any]:
    return {"name": CONVERTER_NAME, "version": CONVERTER_VERSION, "normalizationVersion": NORMALIZATION_VERSION}


def _same(a: Any, b: Any) -> bool:
    """JavaScript ``===`` for JSON scalars (``1 !== true``, ``1 !== 1n``)."""
    return type(a) is type(b) and a == b


def _exact_keys(value: Any, keys: list[str], path: str) -> None:
    object_(value, path)
    for key in keys:
        if key not in value:
            fail(path, f"missing {key}")
    for key in js_keys(value):
        if key not in keys:
            fail(path, f"unexpected {key}")


def _is_uint8(value: Any) -> bool:
    if isinstance(value, (bytes, bytearray)):
        return True
    return isinstance(value, memoryview) and value.format == "B" and value.ndim == 1


def source_bytes(source: Any) -> bytes:
    if isinstance(source, str):
        try:
            source = source.encode("utf-8")
        except UnicodeEncodeError:
            try:  # adjacent surrogate code points form a well-formed pair in JavaScript
                source = source.encode("utf-16-le", "surrogatepass").decode("utf-16-le").encode("utf-8")
            except UnicodeError:
                fail("source", "expected well-formed UTF-8 text")
    if not _is_uint8(source):
        fail("source", "expected UTF-8 text or Uint8Array")
    data = bytes(source)
    if len(data) > SOURCE_LIMIT:
        fail("source", "input exceeds 64 MiB limit")
    return data


def decode_utf8(data: bytes) -> str:
    """``new TextDecoder('utf-8', {fatal: true}).decode(data)``: strict, strips one BOM."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise TracepackError(_INVALID_UTF8) from None


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8", "surrogatepass"))


def create_bundle(source: str | bytes, options: Mapping[str, Any] | None = None, **keywords: Any) -> dict[str, Any]:
    """Create a bundle from UTF-8 text or bytes; options ``from`` and ``strictLoss``."""
    opts = merge_options(options, keywords)
    if not isinstance(opts, Mapping):
        opts = {}
    from_ = option(opts, "from", "auto")
    strict_loss = option(opts, "strictLoss", False)
    if strict_loss is not True and strict_loss is not False:
        fail("options.strictLoss", "expected boolean")
    data = source_bytes(source)
    result = convert(decode_utf8(data), {"from": from_, "to": "otlp", "strictLoss": strict_loss})
    effective = {"from": result["report"]["from"], "to": "otlp", "strictLoss": strict_loss}
    bundle = {
        "format": FORMAT, "version": VERSION, "converter": _converter(),
        "source": {"encoding": "base64", "data": base64.b64encode(data).decode("ascii")}, "options": effective,
        "normalized": result["output"], "lossReport": result["report"],
        "sha256": {"source": _sha(data), "options": _json_hash(effective), "normalized": _json_hash(result["output"]),
                   "lossReport": _json_hash(result["report"])},
    }
    if _utf8_len(canonical_json(bundle) + "\n") > BUNDLE_LIMIT:
        fail("bundle", "output exceeds 256 MiB limit")
    return bundle


def _decode_base64(data: str) -> bytes:
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        fail("bundle.source.data", "expected canonical base64")
    if base64.b64encode(raw).decode("ascii") != data:
        fail("bundle.source.data", "expected canonical base64")
    return raw


def verified_bundle(text: Any) -> tuple[bytes, dict[str, Any], dict[str, Any]]:
    """Fully verify bundle text; returns ``(source_bytes, replay_report, summary)``."""
    if not isinstance(text, str):
        fail("bundle", "expected JSON text")
    if _utf8_len(text) > BUNDLE_LIMIT:
        fail("bundle", "input exceeds 256 MiB limit")
    bundle = parse_json(text)
    _exact_keys(bundle, ["format", "version", "converter", "source", "options", "normalized", "lossReport", "sha256"],
                "bundle")
    if not _same(bundle["format"], FORMAT):
        fail("bundle.format", f"expected {FORMAT}")
    if not _same(bundle["version"], VERSION):
        fail("bundle.version", f"unsupported version; expected {VERSION}")
    _exact_keys(bundle["converter"], ["name", "version", "normalizationVersion"], "bundle.converter")
    for key, value in _converter().items():
        if not _same(bundle["converter"][key], value):
            fail(f"bundle.converter.{key}", f"version/name mismatch; expected {value}")
    _exact_keys(bundle["source"], ["encoding", "data"], "bundle.source")
    if not _same(bundle["source"]["encoding"], "base64"):
        fail("bundle.source.encoding", "expected base64")
    data = bundle["source"]["data"]
    if not isinstance(data, str) or utf16_len(data) > 4 * -(-SOURCE_LIMIT // 3):
        fail("bundle.source.data", "expected base64 source up to 64 MiB")
    raw = _decode_base64(data)
    source_bytes(raw)
    options = bundle["options"]
    _exact_keys(options, ["from", "to", "strictLoss"], "bundle.options")
    if not (isinstance(options["from"], str) and options["from"] in ("otlp", "jaeger", "zipkin")) \
            or not _same(options["to"], "otlp"):
        fail("bundle.options", "expected effective from and to=otlp")
    if options["strictLoss"] is not True and options["strictLoss"] is not False:
        fail("bundle.options.strictLoss", "expected boolean")
    _exact_keys(bundle["sha256"], ["source", "options", "normalized", "lossReport"], "bundle.sha256")
    for member in ("source", "options", "normalized", "lossReport"):
        digest = bundle["sha256"][member]
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            fail(f"bundle.{member}", "expected lowercase SHA-256 digest")
        actual = _sha(raw) if member == "source" else _json_hash(bundle[member])
        if digest != actual:
            fail(f"bundle.{member}", "SHA-256 mismatch")
    # Recomputing a digest alone cannot conceal inconsistent fixture members.
    try:
        replay = convert(decode_utf8(raw), options)
    except Exception as error:  # noqa: BLE001 - every replay failure is a verification failure
        fail("bundle", f"offline conversion replay failed: {error}")
    for member, expected in (("normalized", replay["output"]), ("lossReport", replay["report"])):
        if canonical_json(bundle[member]) != canonical_json(expected):
            fail(f"bundle.{member}", "does not match offline conversion replay")
    summary = {"valid": True, "format": FORMAT, "version": VERSION, "sourceSha256": bundle["sha256"]["source"],
               "spanCount": replay["report"]["spanCount"], "lossCount": len(replay["report"]["losses"]),
               "guarantee": "reproducibility-only"}
    return raw, replay["report"], summary


def verify_bundle(text: Any, options: Any = UNDEFINED, **keywords: Any) -> dict[str, Any]:
    """Fully verify, compare an optional source, apply loss policy, optionally include the report.

    ``options`` may contain only ``source`` (bytes-like), ``strictLoss`` and ``report``;
    it is inspected only after the complete verification succeeded.
    """
    raw, report, summary = verified_bundle(text)
    options = merge_options(options, keywords)
    if options is UNDEFINED:
        options = {}
    object_(options, "options")
    for key in js_keys(options):
        if key not in ("source", "strictLoss", "report"):
            fail("options", f"unexpected {key}")
    strict_loss = option(options, "strictLoss", False)
    if strict_loss is not True and strict_loss is not False:
        fail("options.strictLoss", "expected boolean")
    include_report = option(options, "report", False)
    if include_report is not True and include_report is not False:
        fail("options.report", "expected boolean")
    source = option(options, "source", UNDEFINED)
    if source is not UNDEFINED:
        if not _is_uint8(source):
            fail("options.source", "expected Uint8Array")
        if len(source) > SOURCE_LIMIT:
            fail("options.source", "input exceeds 64 MiB limit")
        if bytes(source) != raw:
            raise TracepackError("source does not match embedded original bytes")
        summary["sourceMatches"] = True
    if strict_loss and report["losses"]:
        raise StrictLossError(report)
    if include_report:
        summary["report"] = report
    return summary


def extract_bundle(text: Any, options: Any = UNDEFINED, **keywords: Any) -> bytes:
    """Fully verify, validate the caller loss policy, then return the original bytes."""
    raw, report, _ = verified_bundle(text)
    options = merge_options(options, keywords)
    if options is UNDEFINED:
        options = {}
    object_(options, "options")
    for key in js_keys(options):
        if key != "strictLoss":
            fail("options", f"unexpected {key}")
    strict_loss = option(options, "strictLoss", False)
    if strict_loss is not True and strict_loss is not False:
        fail("options.strictLoss", "expected boolean")
    if strict_loss and report["losses"]:
        raise StrictLossError(report)
    return raw
