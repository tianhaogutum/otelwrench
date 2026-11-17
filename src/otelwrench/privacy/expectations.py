"""Non-vacuous checks against actual, typed evidence views using jsonpath-ng."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from jsonpath_ng.ext import parse
from jsonpath_ng.ext.filter import Expression, Filter
from jsonpath_ng.jsonpath import Child, Descendants, Fields, Index, JSONPath, Root, Slice, This
from jsonpointer import EndOfList, JsonPointer, JsonPointerException

from .evidence import contains_text, equal, find_text
from .inputs import ReplayError


OPS = {"absent", "present", "equals", "not_contains", "count", "changed", "unchanged"}


def _selector_only(expression: JSONPath) -> None:
    """Use the library's AST, allowing selectors without computed/regex results."""
    allowed = {Child, Descendants, Fields, Index, Root, Slice, This, Expression, Filter}
    remaining, recursive = 128, 0

    def visit(node: JSONPath, depth: int, allow_root: bool = False) -> None:
        nonlocal remaining, recursive
        remaining -= 1
        if remaining < 0 or depth > 64 or type(node) not in allowed:
            raise ReplayError(
                "JSONPath supports field/array selectors and simple comparison filters; "
                "arithmetic, regex, transformations and unions are not supported"
            )
        if isinstance(node, Root) and not allow_root:
            raise ReplayError(
                "JSONPath root is allowed only at the start, not as a repeated root reset"
            )
        if isinstance(node, Descendants):
            recursive += 1
            if recursive > 1:
                raise ReplayError("use at most one recursive-descent operator per JSONPath")
        if isinstance(node, Fields):
            if len(set(node.fields)) != len(node.fields) or len(node.fields) > 64:
                raise ReplayError("JSONPath fields must be distinct and bounded")
            if "*" in node.fields and len(node.fields) > 1:
                raise ReplayError("do not combine wildcard and explicit fields in one selector")
        if isinstance(node, Slice) and node.step == 0:
            raise ReplayError("JSONPath slice step must be nonzero")
        if isinstance(node, Expression) and node.op not in {
            None,
            "=",
            "==",
            "!=",
            "<",
            "<=",
            ">",
            ">=",
        }:
            raise ReplayError("JSONPath filters support literal comparisons; regex is disabled")
        if isinstance(node, (Child, Descendants)):
            visit(node.left, depth + 1, allow_root)
            visit(node.right, depth + 1, False)
            return
        for value in vars(node).values():
            if isinstance(value, JSONPath):
                visit(value, depth + 1)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    if isinstance(item, JSONPath):
                        visit(item, depth + 1)

    visit(expression, 0, True)


class _PointerSelector:
    def __init__(self, pointer: str):
        self.pointer = JsonPointer(pointer)

    def find(self, document: dict) -> list:
        try:
            value = self.pointer.resolve(document)
        except JsonPointerException:
            return []
        if isinstance(value, EndOfList):
            return []
        return [SimpleNamespace(full_path=self.pointer.path, value=value)]


def validate(spec: dict) -> list[tuple[dict, Any]]:
    if set(spec) - {"checks", "forbid"}:
        raise ReplayError("expectations support only checks and forbid")
    checks = spec.get("checks", [])
    forbidden = spec.get("forbid", [])
    if not isinstance(checks, list) or not isinstance(forbidden, list):
        raise ReplayError("checks and forbid must be lists")
    if not checks and not forbidden:
        raise ReplayError("provide at least one expectation or forbidden sentinel")
    if len(checks) + len(forbidden) > 100:
        raise ReplayError("at most 100 privacy expectations are supported")
    if any(not isinstance(text, str) or not text for text in forbidden):
        raise ReplayError("forbid entries must be nonempty literal strings")
    compiled = []
    names = set()
    for check in checks:
        if not isinstance(check, dict) or set(check) - {"name", "path", "pointer", "op", "value"}:
            raise ReplayError("each check supports name, path or pointer, op and value only")
        name, path, op = (check.get(key) for key in ("name", "path", "op"))
        if not isinstance(name, str) or not name or name in names:
            raise ReplayError("checks require unique, nonempty names")
        names.add(name)
        if not isinstance(op, str) or op not in OPS:
            raise ReplayError(f"check {name!r}: provide a supported op")
        if ("path" in check) == ("pointer" in check):
            raise ReplayError(f"check {name!r}: provide exactly one path or pointer")
        if "path" in check and (not isinstance(path, str) or not path.startswith("$")):
            raise ReplayError(f"check {name!r}: path must be a rooted JSONPath")
        if "path" in check and len(path) > 2048:
            raise ReplayError(f"check {name!r}: JSONPath exceeds 2048 characters")
        if "pointer" in check and not isinstance(check["pointer"], str):
            raise ReplayError(f"check {name!r}: pointer must be an RFC 6901 string")
        needs_value = op in {"equals", "not_contains", "count"}
        if needs_value != ("value" in check):
            raise ReplayError(
                f"check {name!r}: value is required only for equals/not_contains/count"
            )
        value = check.get("value")
        if op == "not_contains" and (not isinstance(value, str) or not value):
            raise ReplayError(f"check {name!r}: not_contains requires a nonempty literal string")
        if op == "count" and (type(value) is not int or value < 0):
            raise ReplayError(f"check {name!r}: count requires a nonnegative integer")
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ReplayError(f"check {name!r}: value must be a finite JSON value") from exc
        try:
            selector = _PointerSelector(check["pointer"]) if "pointer" in check else parse(path)
            if "path" in check:
                _selector_only(selector)
            compiled.append((check, selector))
        except Exception as exc:
            raise ReplayError(f"check {name!r}: invalid JSONPath/pointer: {exc}") from exc
    return compiled


def evaluate(spec: dict, before: dict, after: dict) -> list[dict]:
    results = []
    for check, expression in validate(spec):
        try:
            old = {str(match.full_path): match.value for match in expression.find(before)}
            new = {str(match.full_path): match.value for match in expression.find(after)}
        except (TypeError, ValueError, IndexError) as exc:
            raise ReplayError(
                f"check {check['name']!r}: selector cannot be applied to this data: {exc}"
            ) from exc
        op = check["op"]
        detail = ""
        if op == "absent":
            passed = bool(old) and not new
            if not old:
                detail = "input path matched nothing; absence would be vacuous"
        elif op == "present":
            passed = bool(new)
        elif op == "equals":
            passed = bool(new) and all(equal(v, check["value"]) for v in new.values())
        elif op == "not_contains":
            passed = bool(new) and all(not contains_text(v, check["value"]) for v in new.values())
        elif op == "count":
            passed = len(new) == check["value"]
        elif op == "unchanged":
            passed = bool(old) and equal(old, new)
        else:  # changed: every selected input location must change or disappear
            passed = bool(old) and all(k not in new or not equal(v, new[k]) for k, v in old.items())
        if not passed and not detail:
            detail = "actual output did not satisfy expectation"
        results.append(
            {
                **check,
                "passed": passed,
                "input_matches": len(old),
                "output_matches": len(new),
                "detail": detail,
                "actual": [
                    {"path": path, "value": value} for path, value in list(new.items())[:20]
                ],
            }
        )
    for needle in spec.get("forbid", []):
        found = list(find_text(after, needle))
        exercised = list(find_text(before, needle))
        results.append(
            {
                "name": f"forbid {needle}",
                "op": "forbid",
                "value": needle,
                "passed": bool(exercised) and not found,
                "matches": found,
                "input_matches": len(exercised),
                "detail": ""
                if exercised
                else "sentinel does not occur in input; coverage not exercised",
            }
        )
    return results
