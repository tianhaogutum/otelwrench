# SPDX-License-Identifier: Apache-2.0
"""Ordered saved scrapes with explicit target labels, separate from provenance."""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
import json
from pathlib import Path
import re

from .parser import InputError, LABEL_NAME, Scrape, parse


@dataclass(frozen=True)
class IdentifiedScrape:
    id: str
    source: str
    captured_at: str
    scrape: Scrape
    target_labels: Mapping[str, str] = field(default_factory=dict)

    def identity(self):
        result = {"id": self.id, "source": self.source, "captured_at": self.captured_at}
        if self.target_labels:
            result["target_labels"] = dict(sorted(self.target_labels.items()))
        return result


def contextualize(item):
    """Return an analysis copy; never mutate the parsed exposition or its labels.

    Exact overlaps are accepted. Conflicts and context-induced duplicates fail
    before projection, regardless of requested plans or metric scope.
    """
    context = item.target_labels
    if not isinstance(context, Mapping):
        raise InputError(f"scrape {item.id!r}: target_labels must be an object of label names to strings")
    for name, value in context.items():
        if (not isinstance(name, str) or not LABEL_NAME.fullmatch(name)
                or name.startswith("__") or name in {"le", "quantile"}):
            raise InputError(f"scrape {item.id!r}: invalid or protected target label {name!r}")
        if (not isinstance(value, str) or any(c in value for c in ("\x00", "\r"))
                or any(0xD800 <= ord(c) <= 0xDFFF for c in value)):
            raise InputError(f"scrape {item.id!r}: target label {name!r} requires a UTF-8 string without NUL/CR")
    if not context:
        return item
    samples, seen = [], {}
    for sample in item.scrape.samples:
        labels = dict(sample.labels)
        for name, value in context.items():
            if name in labels and labels[name] != value:
                raise InputError(
                    f"scrape {item.id!r}, line {sample.line}: target label {name!r} conflicts with exposition",
                    details={"code": "target_label_conflict", "scrape_id": item.id,
                             "line": sample.line, "label": name,
                             "exposition_value": labels[name], "target_value": value})
            labels[name] = value
        effective = replace(sample, labels=tuple(sorted(labels.items())))
        if effective.key in seen:
            raise InputError(
                f"scrape {item.id!r}, line {sample.line}: duplicate input series after target labels; first seen on line {seen[effective.key]}",
                details={"code": "target_label_duplicate", "scrape_id": item.id,
                         "line": sample.line, "first_seen_line": seen[effective.key],
                         "effective_key": {"metric": effective.key[0], "labels": dict(effective.key[1])}})
        seen[effective.key] = sample.line
        samples.append(effective)
    return replace(item, scrape=Scrape(tuple(samples), item.scrape.types, item.scrape.help),
                   target_labels=dict(sorted(context.items())))


def validate_scrapes(scrapes):
    for item in scrapes:
        if not isinstance(item, IdentifiedScrape) or not isinstance(item.scrape, Scrape) or not item.scrape.samples:
            raise InputError("each identified scrape must contain a parsed, nonempty Scrape")
    _validate_identities([{"id": item.id, "source": item.source, "captured_at": item.captured_at}
                          for item in scrapes])


def validate_target_label_names(labels, kind="required"):
    """Normalize explicit coverage/protection names without inferring identity."""
    if not isinstance(labels, (list, tuple)):
        raise InputError(f"{kind}_target_labels must be a list or tuple of distinct label names")
    seen = set()
    for name in labels:
        if (not isinstance(name, str) or not LABEL_NAME.fullmatch(name)
                or name.startswith("__") or name in {"le", "quantile"}):
            raise InputError(f"invalid or protected {kind} target label {name!r}")
        if name in seen:
            raise InputError(f"duplicate {kind} target label {name!r}")
        seen.add(name)
    return tuple(sorted(seen))


def validate_required_target_labels(scrapes, labels):
    """Require nonempty labels on every effective sample, independent of plans."""
    required = validate_target_label_names(labels)
    if required:
        for item in scrapes:
            for sample in item.scrape.samples:
                effective = dict(sample.labels)
                for name in required:
                    if not effective.get(name):
                        raise InputError(
                            f"scrape {item.id!r}, line {sample.line}: required target label {name!r} is absent or empty",
                            details={"code": "target_label_coverage", "scrape_id": item.id,
                                     "line": sample.line, "label": name,
                                     "reason": "empty" if name in effective else "absent"})
    return required


def _validate_identities(identities):
    if not identities:
        raise InputError("a scrape set must contain at least one scrape")
    seen = set()
    for item in identities:
        for name in ("id", "source", "captured_at"):
            value = item[name]
            if not isinstance(value, str) or not value.strip():
                raise InputError(f"scrape {name} must be a nonempty string")
        if item["id"] in seen:
            raise InputError(f"duplicate scrape id {item['id']!r}")
        seen.add(item["id"])
        # Explicit timezone and seconds are required. Preserve the original token.
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})", item["captured_at"]):
            raise InputError(f"scrape {item['id']!r}: captured_at must be an RFC3339 timestamp with timezone")
        try:
            datetime.fromisoformat(item["captured_at"].replace("Z", "+00:00"))
            if not re.search(r"(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])$", item["captured_at"]):
                raise ValueError("invalid timezone offset")
        except ValueError as exc:
            raise InputError(f"scrape {item['id']!r}: invalid captured_at timestamp") from exc


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputError(f"duplicate JSON field {key!r}")
        result[key] = value
    return result


def load_scrape_set(path):
    """Strict local manifest v1. Relative files resolve beside the manifest."""
    path = Path(path)
    try:
        data = json.loads(path.read_bytes().decode("utf-8"), object_pairs_hook=_unique_object)
    except (ValueError, RecursionError) as exc:
        raise InputError(f"invalid scrape-set JSON: {exc}") from exc
    if (not isinstance(data, dict) or set(data) != {"schema_version", "scrapes"}
            or type(data["schema_version"]) is not int or data["schema_version"] != 1):
        raise InputError("scrape-set manifest requires only schema_version: 1 and scrapes")
    if not isinstance(data["scrapes"], list) or not data["scrapes"]:
        raise InputError("scrapes must be a nonempty ordered array")
    entries = []
    for entry in data["scrapes"]:
        required = {"id", "source", "captured_at", "path"}
        if (not isinstance(entry, dict) or not required <= set(entry)
                or set(entry) - required - {"target_labels"}):
            raise InputError("each scrape requires id, source, captured_at and path; only target_labels is optional")
        if (not isinstance(entry["path"], str) or not entry["path"].strip()
                or entry["path"] == "-" or "\x00" in entry["path"]):
            raise InputError("each scrape path must name a local file; stdin is unsupported in a set")
        entries.append(entry)
    _validate_identities(entries)
    result = []
    for entry in entries:
        try:
            scrape = parse((path.parent / entry["path"]).read_bytes().decode("utf-8"))
        except (InputError, OSError, UnicodeError) as exc:
            details = ({**exc.details, "scrape_id": entry["id"]}
                       if isinstance(exc, InputError) and exc.details is not None else None)
            raise InputError(f"scrape {entry['id']!r} ({entry['path']}): {exc}", details=details) from exc
        item = IdentifiedScrape(entry["id"], entry["source"], entry["captured_at"], scrape,
                                entry.get("target_labels", {}))
        contextualize(item)  # Validate context too; retain raw exposition in the API.
        result.append(item)
    return result
