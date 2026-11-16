"""Load the PromQL expressions of dashboards and rule files, and list the names they use.

This is name matching, not a PromQL parser: an expression "uses" a metric or label
when the identifier appears outside string literals and range selectors. That is
enough to answer "which panels and alerts read something that changed".
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re

from ..promdiff._files import MAX_INPUT_BYTES, read_text
from ..promdiff.parser import InputError

_STRING = re.compile(r'"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\'|`[^`]*`')
_RANGE = re.compile(r"\[[^\]]*\]")
_COMMENT = re.compile(r"#[^\n]*")
_IDENT = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")


@dataclass(frozen=True)
class Query:
    name: str
    kind: str  # panel, alert, record or query
    source: str
    expr: str

    def record(self) -> dict:
        return {"name": self.name, "kind": self.kind, "source": self.source, "expr": self.expr}


def identifiers(expr: str) -> set[str]:
    text = _STRING.sub(" ", _COMMENT.sub(" ", expr))
    return set(_IDENT.findall(_RANGE.sub(" ", text)))


def _dashboard(doc, source: str) -> list[Query]:
    found: list[tuple[str, str]] = []

    def walk(node, title):
        if isinstance(node, dict):
            if isinstance(node.get("title"), str) and node["title"].strip():
                title = node["title"].strip()
            expr = node.get("expr")
            if isinstance(expr, str) and expr.strip():
                found.append((title, expr.strip()))
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value, title)
        elif isinstance(node, list):
            for value in node:
                walk(value, title)

    walk(doc, Path(source).stem)
    seen: dict[str, int] = {}
    out = []
    for title, expr in found:
        seen[title] = seen.get(title, 0) + 1
        name = title if seen[title] == 1 else f"{title} #{seen[title]}"
        out.append(Query(name, "panel", source, expr))
    return out


def _rules(docs, source: str) -> list[Query]:
    out = []
    for doc in docs:
        if doc is None:
            continue
        if isinstance(doc, dict) and isinstance(doc.get("spec"), dict):  # PrometheusRule resource
            doc = doc["spec"]
        groups = doc.get("groups") if isinstance(doc, dict) else None
        if not isinstance(groups, list):
            raise InputError(f"{source}: expected a Prometheus rule file with a top-level 'groups' list")
        for group in groups:
            for rule in (group.get("rules") or []) if isinstance(group, dict) else []:
                if not isinstance(rule, dict) or not isinstance(rule.get("expr"), (str, int, float)):
                    continue
                kind = "alert" if "alert" in rule else "record"
                out.append(Query(str(rule.get(kind, "")), kind, source, str(rule["expr"]).strip()))
    return out


def _lines(text: str, source: str) -> list[Query]:
    out = []
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, expr = line.partition(": ")
        if not sep or not re.fullmatch(r"[\w:.\- ]+", name):
            name, expr = f"line {number}", line
        out.append(Query(name.strip(), "query", source, expr.strip()))
    return out


def load_queries(path: str) -> list[Query]:
    """A Grafana dashboard (.json), a Prometheus rule file (.yml/.yaml) or one query per line."""
    text = read_text(path, max_bytes=MAX_INPUT_BYTES)
    suffix = Path(path).suffix.lower()
    if suffix == ".json":
        try:
            return _dashboard(json.loads(text), path)
        except (ValueError, RecursionError) as exc:
            raise InputError(f"{path}: invalid JSON: {exc}") from exc
    if suffix in (".yml", ".yaml"):
        import yaml
        try:
            return _rules(list(yaml.safe_load_all(text)), path)
        except yaml.YAMLError as exc:
            raise InputError(f"{path}: invalid YAML: {exc}") from exc
    return _lines(text, path)


__all__ = ["Query", "identifiers", "load_queries"]
