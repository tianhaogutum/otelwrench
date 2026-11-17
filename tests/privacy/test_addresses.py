from jsonpointer import JsonPointer
import pytest

from otelwrench.privacy.evidence import find_text, inventory, json_pointer
from otelwrench.privacy.expectations import evaluate, validate
from otelwrench.privacy.inputs import ReplayError


@pytest.mark.parametrize("key", ["email/slash~", "quote'\"", "line\nbreak", "☃", "back\\slash"])
def test_arbitrary_attribute_names_have_roundtrippable_addresses(key):
    document = {"attributes": {key: {"stringValue": "SECRET"}}}
    row = inventory(document, document)[0]
    assert JsonPointer(row["pointer"]).resolve(document) == "SECRET"
    leak = list(find_text(document, "SECRET"))[0]
    assert JsonPointer(leak["pointer"]).resolve(document) == "SECRET"
    check = {
        "checks": [
            {"name": "precise", "pointer": row["pointer"], "op": "equals", "value": "SECRET"}
        ]
    }
    assert evaluate(check, document, document)[0]["passed"]


def test_pointer_absence_requires_input_match_and_handles_array_end():
    pointer = json_pointer(("attributes", "line\nbreak"))
    spec = {"checks": [{"name": "removed", "pointer": pointer, "op": "absent"}]}
    assert evaluate(spec, {"attributes": {"line\nbreak": 1}}, {})[0]["passed"]
    assert not evaluate(spec, {}, {})[0]["passed"]
    append = {"checks": [{"name": "append", "pointer": "/items/-", "op": "present"}]}
    assert not evaluate(append, {"items": []}, {"items": []})[0]["passed"]


@pytest.mark.parametrize(
    "check",
    [
        {"name": "both", "path": "$.x", "pointer": "/x", "op": "present"},
        {"name": "invalid", "pointer": "/bad~2escape", "op": "present"},
        {"name": "invalid", "pointer": "not-rooted", "op": "present"},
    ],
)
def test_ambiguous_or_invalid_selectors_fail_up_front(check):
    with pytest.raises(ReplayError):
        validate({"checks": [check]})
