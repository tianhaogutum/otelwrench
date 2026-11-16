# SPDX-License-Identifier: Apache-2.0
import json

import pytest

from otelwrench.tracediff import normalize

from .support import clone, otlp, reverse_keys


def _scope(rs):
    rs["scopeSpans"][0]["scope"] = {}


def _scope_schema(rs):
    rs["scopeSpans"][0]["schemaUrl"] = ""


def _resource_schema(rs):
    rs["schemaUrl"] = ""


CHANGES = [("scope", _scope), ("scope schema", _scope_schema), ("resource schema", _resource_schema)]


@pytest.mark.parametrize("name,change", CHANGES)
def test_duplicate_identity_rejects_omitted_versus_explicit_empty(name, change):
    a = otlp()
    copy = clone(a["resourceSpans"][0])
    change(copy)
    a["resourceSpans"].append(copy)
    with pytest.raises(Exception, match="conflicting duplicate identity"):
        normalize(a)


@pytest.mark.parametrize("name,change", CHANGES)
def test_identical_explicit_empty_still_deduplicates_ignoring_context_key_order(name, change):
    a = otlp()
    change(a["resourceSpans"][0])
    copy = json.loads(json.dumps(reverse_keys(a["resourceSpans"][0])))
    a["resourceSpans"].append(copy)
    n = normalize(a)
    assert n["spanCount"] == 1 and n["duplicateSpanCount"] == 1
