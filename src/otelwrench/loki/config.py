"""The deliberately limited limits_config.otlp_config profile."""

from dataclasses import dataclass
from importlib.resources import files
import json

import re2

from .strict import fail, obj, read_yaml, seq, string

DEFAULTS = json.loads(files(__package__).joinpath("defaults.json").read_text())


@dataclass(frozen=True)
class Rule:
    action: str
    attributes: tuple[str, ...]
    regex: object | None

    def matches(self, key):
        return key in self.attributes or (self.regex is not None and self.regex.search(key) is not None)


@dataclass(frozen=True)
class Profile:
    ignore_defaults: bool
    resource: tuple[Rule, ...]
    scope: tuple[Rule, ...]
    log: tuple[Rule, ...]

    def describe(self):
        result = {"ignore_defaults": self.ignore_defaults}
        for level in ("resource", "scope", "log"):
            result[level] = [
                {"action": rule.action, **({"regex": rule.regex.pattern} if rule.regex is not None
                                           else {"attributes": list(rule.attributes)})}
                for rule in getattr(self, level)
            ]
        return result

    def placement(self, level, key):
        decision = self.resolve(level, key)
        return decision["placement"], decision["reason"]

    def resolve(self, level, key, *, trace=False):
        """One precedence implementation for preview and optional selector audits.

        A trace tests later selectors to show shadowing, without applying them.
        Normal projection stops testing selectors at its first match.
        """
        winner, checks = None, []
        base = f"/limits_config/otlp_config/{level}_attributes"
        if level == "resource":
            base += "/attributes_config"
        for index, rule in enumerate(getattr(self, level)):
            matched = bool(rule.matches(key))
            selected = matched and winner is None
            ref = f"{base}/{index}"
            if selected:
                winner = {"placement": rule.action, "reason": f"{level} rule {index + 1}: first match",
                          "ruleRef": ref}
            if not trace and selected:
                return winner
            if trace:
                checks.append({"ruleRef": ref, "action": rule.action,
                               "selector": ({"regex": rule.regex.pattern} if rule.regex is not None
                                            else {"attributes": list(rule.attributes)}),
                               "matchType": "re2_search" if rule.regex is not None else "literal",
                               "matched": matched,
                               "status": "selected" if selected else "shadowed" if matched else "not_matched"})
        contains = level == "resource" and key in DEFAULTS["attributes"]
        enabled = level == "resource" and not self.ignore_defaults
        default_ref = f"defaults.json#/attributes/{DEFAULTS['attributes'].index(key)}" if contains else None
        default_selected = winner is None and enabled and contains
        if default_selected:
            winner = {"placement": "index_label", "reason": "dated default resource index-label list",
                      "ruleRef": default_ref}
        fallback_selected = winner is None
        if fallback_selected:
            winner = {"placement": "structured_metadata", "reason": "unmatched attribute; structured metadata fallback",
                      "ruleRef": "builtin:structured_metadata"}
        if trace:
            winner.update({"originalKey": key, "rules": checks,
                           "defaultResourceList": {"enabled": enabled, "containsKey": contains,
                                                   "selected": default_selected, "ruleRef": default_ref},
                           "fallback": {"ruleRef": "builtin:structured_metadata", "selected": fallback_selected}})
        return winner


def rules(value, level):
    result = []
    for index, item in enumerate(seq(value, f"{level}_attributes")):
        where = f"{level} rule {index + 1}"
        obj(item, where, {"action", "attributes", "regex"})
        action = item.get("action")
        if action not in ("index_label", "structured_metadata", "drop"):
            fail(where, "action must be index_label, structured_metadata, or drop")
        if action == "index_label" and level != "resource":
            fail(where, "index_label is supported only for resource attributes")
        # Explicit selectors avoid ambiguous upstream/version-dependent combinations.
        if ("attributes" in item) == ("regex" in item):
            fail(where, "provide exactly one of attributes or regex")
        names, regex = (), None
        if "attributes" in item:
            names = tuple(string(x, where, nonempty=True) for x in seq(item["attributes"], where))
            if not names or len(names) != len(set(names)):
                fail(where, "attributes must be nonempty and unique")
        else:
            expression = string(item["regex"], where, nonempty=True)
            options = re2.Options()
            options.log_errors = False
            try:
                regex = re2.compile(expression, options=options)
            except re2.error as exc:
                fail(where, f"invalid RE2 regex: {exc}")
        result.append(Rule(action, names, regex))
    return tuple(result)


def parse_profile(data):
    obj(data, "config", {"limits_config"})
    if "limits_config" not in data:
        fail("config", "missing limits_config; use an explicit mapping profile")
    limits = obj(data["limits_config"], "limits_config", {"otlp_config", "allow_structured_metadata"})
    if "allow_structured_metadata" in limits and limits["allow_structured_metadata"] is not True:
        fail("limits_config", "allow_structured_metadata must be true")
    otlp = obj(limits.get("otlp_config", {}), "otlp_config",
               {"resource_attributes", "scope_attributes", "log_attributes"})
    resource = obj(otlp.get("resource_attributes", {}), "resource_attributes",
                   {"ignore_defaults", "attributes_config"})
    ignore = resource.get("ignore_defaults", False)
    if type(ignore) is not bool:
        fail("ignore_defaults", "expected a boolean")
    return Profile(ignore, rules(resource.get("attributes_config", []), "resource"),
                   rules(otlp.get("scope_attributes", []), "scope"),
                   rules(otlp.get("log_attributes", []), "log"))


def load_profile(path):
    return parse_profile(read_yaml(path))
