# SPDX-License-Identifier: Apache-2.0
"""Deterministic local result archives, published by one atomic replacement."""
import hashlib
import json
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
import tempfile
import zipfile

from .core import ConversionError, STRATEGIES, UNITS, WITH, conversion_metric_names, convert, double, label_name, load_json, metric_name, milliseconds_to_seconds, string

ARTIFACTS = ("exposition.prom", "mapping.json")
MEMBERS = (*ARTIFACTS, "manifest.json")


def json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def digest(content: bytes) -> dict:
    return {"sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content)}


def build_bundle(source: bytes, *, strategy=WITH, promote_resource=(),
                 allow_nonfinite=False, convert_unit=None, convert_metrics=None) -> tuple[dict[str, bytes], dict]:
    """Convert once and bind exact input bytes, effective config and both artifacts."""
    exposition, report = convert(load_json(source.decode("utf-8")), strategy=strategy,
                                 promote_resource=promote_resource,
                                 allow_nonfinite=allow_nonfinite, convert_unit=convert_unit,
                                 convert_metrics=convert_metrics)
    members = {"exposition.prom": exposition.encode("utf-8"), "mapping.json": json_bytes(report)}
    manifest = {
        "schema_version": 1,
        "input": digest(source),
        "configuration": {"strategy": strategy, "promote_resource": report["resource_promotion"],
                          "allow_nonfinite": allow_nonfinite, "convert_unit": convert_unit},
        "artifacts": {name: digest(content) for name, content in members.items()},
    }
    if "unit_conversion_metrics" in report:
        manifest["configuration"]["convert_metrics"] = report["unit_conversion_metrics"]
    members["manifest.json"] = json_bytes(manifest)
    return members, report


def atomic_write_bundle(path: Path, members: dict[str, bytes]):
    """Stage and fsync a complete archive beside its destination, then replace once.

    No fallible write happens after the replacement. This is process-level atomic
    publication, not a guarantee about directory durability after power loss.
    """
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        try:
            stream = os.fdopen(fd, "wb")
        except BaseException:
            os.close(fd)
            raise
        with stream:
            with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
                for name in MEMBERS:
                    # Fixed ZIP metadata avoids wall-clock/host-dependent results.
                    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                    info.create_system = 3
                    info.external_attr = 0o100644 << 16
                    archive.writestr(info, members[name])
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _object(value, keys, context):
    if not isinstance(value, dict) or value.keys() != set(keys):
        raise ConversionError(f"bundle {context}: expected exactly {', '.join(sorted(keys))}")
    return value


def _digest(value, context):
    _object(value, ("sha256", "size_bytes"), context)
    if (not isinstance(value["sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is None
            or type(value["size_bytes"]) is not int or value["size_bytes"] < 0):
        raise ConversionError(f"bundle {context}: invalid SHA-256/size descriptor")


def _manifest(raw: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ConversionError(f"bundle manifest: duplicate key {key!r}")
            result[key] = value
        return result

    try:
        manifest = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    except (ValueError, UnicodeError) as exc:
        raise ConversionError(f"bundle manifest: {exc}") from exc
    _object(manifest, ("schema_version", "input", "configuration", "artifacts"), "manifest")
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise ConversionError("bundle manifest: unsupported schema_version; expected 1")
    _digest(manifest["input"], "input")
    _object(manifest["artifacts"], ARTIFACTS, "artifacts")
    for name in ARTIFACTS:
        _digest(manifest["artifacts"][name], name)
    config = manifest["configuration"]
    keys = ("strategy", "promote_resource", "allow_nonfinite", "convert_unit")
    if isinstance(config, dict) and "convert_metrics" in config:
        keys += ("convert_metrics",)
    config = _object(config, keys, "configuration")
    promotion = config["promote_resource"]
    if (config["strategy"] not in STRATEGIES or type(config["allow_nonfinite"]) is not bool
            or config["convert_unit"] not in (None, "ms:s")
            or not isinstance(promotion, list)
            or any(not isinstance(key, str) or not key for key in promotion)
            or promotion != sorted(set(promotion))):
        raise ConversionError("bundle configuration: unsupported or noncanonical configuration")
    if "convert_metrics" in config:
        names = config["convert_metrics"]
        if (not isinstance(names, list) or not names
                or names != conversion_metric_names(names, config["convert_unit"])):
            raise ConversionError("bundle configuration: convert_metrics must be a nonempty sorted unique list of exact raw names")
    return manifest


def _verified_members(path: Path) -> tuple[dict, dict[str, bytes]]:
    """Read and hash-check one archive generation; never reopen or extract it."""
    try:
        # Keep a single file handle open even if a concurrent publisher replaces path.
        with path.open("rb") as stream, zipfile.ZipFile(stream) as archive:
            names = archive.namelist()
            if len(names) != len(MEMBERS) or set(names) != set(MEMBERS):
                raise ConversionError("bundle must contain exactly one exposition.prom, mapping.json and manifest.json")
            for info in archive.infolist():
                if info.compress_type != zipfile.ZIP_STORED or info.flag_bits & 1:
                    raise ConversionError("bundle members must be uncompressed and unencrypted")
            members = {name: archive.read(name) for name in MEMBERS}
    except (zipfile.BadZipFile, EOFError, NotImplementedError, RuntimeError) as exc:
        raise ConversionError(f"invalid bundle archive: {exc}") from exc
    manifest = _manifest(members["manifest.json"])
    for name in ARTIFACTS:
        if digest(members[name]) != manifest["artifacts"][name]:
            raise ConversionError(f"bundle {name}: SHA-256/size mismatch")
    return manifest, members


def _reproduce_members(manifest: dict, members: dict[str, bytes], source: bytes | None):
    """Replay against already verified bytes, without reopening the bundle path."""
    if source is not None:
        if digest(source) != manifest["input"]:
            raise ConversionError("bundle input: SHA-256/size mismatch (exact original input bytes required)")
        reproduced, _ = build_bundle(source, **manifest["configuration"])
        for name in ARTIFACTS:
            if reproduced[name] != members[name]:
                raise ConversionError(f"bundle {name}: input/configuration did not reproduce stored bytes")


def verify_bundle(path: Path, *, source: bytes | None = None) -> dict:
    """Verify one open archive generation; optionally reproduce exact result bytes.

    Hashes detect accidental corruption/mixing, not a hostile manifest rewrite.
    No archive paths are extracted and no manifest code is executed.
    """
    manifest, members = _verified_members(path)
    _reproduce_members(manifest, members, source)
    return {"status": "ok", "reproduced": source is not None,
            "artifacts": manifest["artifacts"], "input": manifest["input"],
            "configuration": manifest["configuration"]}


def _inspection_mapping(raw: bytes, configuration: dict) -> dict:
    """Validate the schema-v1 fields used by inspection, not a conversion replay."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ConversionError(f"bundle mapping: duplicate key {key!r}")
            result[key] = value
        return result

    def constant(value):
        raise ConversionError(f"bundle mapping: invalid JSON constant {value}")

    try:
        report = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
        # Check all retained loss details for UTF-8 and finite JSON numbers too.
        json.dumps(report, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise ConversionError(f"bundle mapping: {exc}") from exc
    if (not isinstance(report, dict) or type(report.get("schema_version")) is not int
            or report["schema_version"] != 1 or report.get("status") != "ok"):
        raise ConversionError("bundle mapping: expected schema_version 1 and status ok")
    for field, key in (("strategy", "strategy"), ("resource_promotion", "promote_resource"),
                       ("unit_conversion", "convert_unit"), ("unit_conversion_metrics", "convert_metrics")):
        if field not in report and key == "convert_metrics" and key not in configuration:
            continue
        if field not in report or key not in configuration or report[field] != configuration[key]:
            raise ConversionError(f"bundle mapping: {field} disagrees with manifest configuration")
    if (type(report.get("sample_count")) is not int or report["sample_count"] < 0
            or not isinstance(report.get("metrics"), list) or not isinstance(report.get("losses"), list)):
        raise ConversionError("bundle mapping: expected nonnegative sample_count and metrics/losses arrays")
    for loss in report["losses"]:
        if not isinstance(loss, dict):
            raise ConversionError("bundle mapping: loss must be an object")
        for field in ("path", "reason"):
            string(loss.get(field), f"bundle mapping loss.{field}", nonempty=True)
    return report


def inspect_bundle(path: Path, *, source: bytes | None = None, metric_names=None) -> dict:
    """Summarize verified mapping bytes without extraction or output writes.

    Counts describe descriptor occurrences and loss entries, not unique families
    or dropped samples. Integrity is mandatory; exact input reproduction is optional.
    Exact raw-name filtering happens after whole-bundle validation. Existing totals
    and losses remain whole-bundle; filtered output adds explicit matched counts.
    """
    manifest, members = _verified_members(path)
    _reproduce_members(manifest, members, source)
    configuration = manifest["configuration"]
    report = _inspection_mapping(members["mapping.json"], configuration)
    selected = set(configuration.get("convert_metrics", ()))
    selected_semantics = {}
    selected_descriptions = {}
    selected_family_owners = {}
    selected_parser_owners = {}
    selected_label_owners = {}
    selected_series = {}
    metrics = []
    fields = ("path", "original_name", "target_family", "target_type", "parser_family",
              "parser_sample", "original_unit", "target_unit")
    for entry in report["metrics"]:
        if not isinstance(entry, dict):
            raise ConversionError("bundle mapping: metric must be an object")
        metric = {key: string(entry.get(key), f"bundle mapping metric.{key}",
                              nonempty=key not in ("original_unit", "target_unit")) for key in fields}
        if (metric["target_type"] not in ("gauge", "counter")
                or metric["original_unit"] not in UNITS or metric["target_unit"] not in UNITS
                or not isinstance(entry.get("data_points"), list)):
            raise ConversionError("bundle mapping: invalid metric type, units or data_points")
        if metric["original_name"] in selected and metric["original_unit"] != "ms":
            raise ConversionError(f"bundle mapping: selected metric {metric['original_name']!r} "
                                  f"at {metric['path']} must have original unit ms")
        converted = (configuration["convert_unit"] == "ms:s" and metric["original_unit"] == "ms"
                     and ("convert_metrics" not in configuration
                          or metric["original_name"] in selected))
        if metric["target_unit"] != ("s" if converted else metric["original_unit"]):
            raise ConversionError("bundle mapping: target_unit disagrees with manifest configuration")
        if metric["original_name"] in selected:
            context = f"bundle mapping: selected metric {metric['original_name']!r} at {metric['path']}"
            original_type = entry.get("original_type")
            monotonic = entry.get("is_monotonic")
            if original_type not in ("gauge", "sum"):
                raise ConversionError(f"{context}: original_type must be gauge or sum")
            if ("is_monotonic" not in entry
                    or (original_type == "gauge" and monotonic is not None)
                    or (original_type == "sum" and type(monotonic) is not bool)):
                raise ConversionError(f"{context}: is_monotonic must be null for gauge or boolean for sum")
            temporality = entry.get("aggregation_temporality")
            if ("aggregation_temporality" not in entry
                    or (original_type == "gauge" and temporality is not None)
                    or (original_type == "sum" and (type(temporality) is not int or temporality != 2))):
                raise ConversionError(f"{context}: aggregation_temporality must be null for gauge or integer 2 for sum")
            counter = original_type == "sum" and monotonic is True
            if metric["target_type"] != ("counter" if counter else "gauge"):
                raise ConversionError(f"{context}: target_type disagrees with original_type/is_monotonic")
            try:
                target, _ = metric_name(metric["original_name"], metric["target_unit"], counter,
                                        configuration["strategy"], convert_milliseconds=converted)
            except ConversionError as exc:
                raise ConversionError(f"{context}: {exc}") from exc
            parser_family = target[:-6] if counter and target.endswith("_total") else target
            expected = {"target_family": target, "parser_family": parser_family,
                        "parser_sample": parser_family + "_total" if counter else target}
            for field, name in expected.items():
                if metric[field] != name:
                    raise ConversionError(f"{context}: {field} disagrees with configured conversion naming")
            semantics = (original_type, monotonic, temporality)
            first_semantics, first_path = selected_semantics.setdefault(
                metric["original_name"], (semantics, metric["path"]))
            if semantics != first_semantics:
                raise ConversionError(f"{context}: conflicting original descriptor semantics "
                                      f"(original_type/is_monotonic/aggregation_temporality) "
                                      f"with first occurrence at {first_path}")
            description = string(entry.get("original_description"), f"{context}: original_description")
            first_description, first_path = selected_descriptions.setdefault(
                metric["original_name"], (description, metric["path"]))
            if description != first_description:
                raise ConversionError(f"{context}: conflicting original_description "
                                      f"with first occurrence at {first_path}")
            first_name, first_path = selected_family_owners.setdefault(
                metric["target_family"], (metric["original_name"], metric["path"]))
            if metric["original_name"] != first_name:
                raise ConversionError(f"{context}: conflicting target_family {metric['target_family']!r} "
                                      f"owned by selected metric {first_name!r} "
                                      f"at first occurrence {first_path}")
            first_name, first_path = selected_parser_owners.setdefault(
                metric["parser_family"], (metric["original_name"], metric["path"]))
            if metric["original_name"] != first_name:
                raise ConversionError(f"{context}: conflicting parser_family {metric['parser_family']!r} "
                                      f"owned by selected metric {first_name!r} "
                                      f"at first occurrence {first_path}")
            for index, point in enumerate(entry["data_points"]):
                point_context = f"{context}: data_points[{index}]"
                if not isinstance(point, dict):
                    raise ConversionError(f"{point_context}: point must be an object")
                point_path = string(point.get("path"), f"{point_context}.path", nonempty=True)
                point_context += f" at {point_path}"
                original = string(point.get("original_value"), f"{point_context}: original_value", nonempty=True)
                stored = string(point.get("value"), f"{point_context}: value", nonempty=True)
                value_context = f"{point_context}: original_value {original!r}, value {stored!r}"
                if original in ("NaN", "+Inf", "-Inf"):
                    if not configuration["allow_nonfinite"]:
                        raise ConversionError(f"{value_context}: nonfinite original_value requires --allow-nonfinite")
                else:
                    if not re.fullmatch(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?", original):
                        raise ConversionError(f"{value_context}: original_value must be finite decimal text or NaN/+Inf/-Inf")
                    try:
                        double(Decimal(original), value_context, allow_nonfinite=False)
                    except InvalidOperation as exc:
                        raise ConversionError(f"{value_context}: invalid original_value decimal") from exc
                try:
                    expected_value = milliseconds_to_seconds(original, value_context)
                except (InvalidOperation, OverflowError) as exc:
                    raise ConversionError(f"{value_context}: invalid ms:s decimal conversion") from exc
                if stored != expected_value:
                    raise ConversionError(f"{value_context}: expected exact ms:s value {expected_value!r}")
                if not isinstance(point.get("labels"), list):
                    raise ConversionError(f"{point_context}: labels must be an array")
                labels = {}
                for label in point["labels"]:
                    if not isinstance(label, dict):
                        raise ConversionError(f"{point_context}: label must be an object")
                    target_label = string(label.get("target_label"),
                                          f"{point_context}: target_label", nonempty=True)
                    value = string(label.get("value"), f"{point_context}: label value")
                    if target_label in labels:
                        raise ConversionError(f"{point_context}: duplicate target_label {target_label!r}")
                    key = string(label.get("key"), f"{point_context}: label key", nonempty=True)
                    label_path = string(label.get("path"), f"{point_context}: label path", nonempty=True)
                    first_key, first_label_path, first_point_path = selected_label_owners.setdefault(
                        (metric["target_family"], target_label), (key, label_path, point_path))
                    if key != first_key:
                        raise ConversionError(f"{point_context}: conflicting target_label {target_label!r} "
                                              f"in target_family {metric['target_family']!r}: "
                                              f"raw key {key!r} at label {label_path} conflicts with "
                                              f"first raw key {first_key!r} at label {first_label_path} "
                                              f"in point {first_point_path}")
                    label_context = (f"{point_context}: raw key {key!r} at label {label_path}, "
                                     f"target_label {target_label!r}")
                    try:
                        expected_label = label_name(key)
                    except ConversionError as exc:
                        raise ConversionError(f"{label_context}: {exc}") from exc
                    if target_label != expected_label:
                        raise ConversionError(f"{label_context}: expected normalized label {expected_label!r}")
                    labels[target_label] = value
                # Match the converter's final-series identity, including empty/absent equivalence.
                identity = (metric["parser_sample"], tuple(sorted(
                    (key, value) for key, value in labels.items() if value != "")))
                if identity in selected_series:
                    raise ConversionError(f"{point_context}: duplicate canonical final-series identity "
                                          f"{identity!r}; first point at {selected_series[identity]}")
                selected_series[identity] = point_path
        metrics.append({**metric, "sample_count": len(entry["data_points"]), "unit_converted": converted})
    unmatched = selected - {metric["original_name"] for metric in metrics}
    if unmatched:
        raise ConversionError(f"bundle mapping: unmatched exact raw metric names: {sorted(unmatched)!r}")
    if sum(metric["sample_count"] for metric in metrics) != report["sample_count"]:
        raise ConversionError("bundle mapping: sample_count disagrees with descriptor point counts")
    result = {"schema_version": 1, "status": "ok", "verified": True, "reproduced": source is not None,
            "configuration": configuration, "sample_count": report["sample_count"],
            "metric_count": len(metrics), "loss_count": len(report["losses"]),
            "losses": report["losses"], "metrics": metrics}
    if metric_names is not None:
        if not isinstance(metric_names, (list, tuple, set, frozenset)) or not metric_names:
            raise ConversionError("bundle inspection: metric_names must be a nonempty collection of exact raw names")
        names = sorted({string(name, "bundle inspection metric_names", nonempty=True) for name in metric_names})
        unmatched = set(names) - {metric["original_name"] for metric in metrics}
        if unmatched:
            raise ConversionError(f"bundle inspection: unmatched exact raw metric names: {sorted(unmatched)!r}")
        requested = set(names)
        matches = [metric for metric in metrics if metric["original_name"] in requested]
        result.update(filter_metrics=names, matched_sample_count=sum(metric["sample_count"] for metric in matches),
                      matched_metric_count=len(matches), metrics=matches)
    return result
