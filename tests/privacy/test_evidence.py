import pytest

from otelwrench.privacy.evidence import area, diagnostics, differences, inventory, view
from otelwrench.privacy.expectations import evaluate, validate
from otelwrench.privacy.inputs import ReplayError


def test_attribute_reordering_does_not_fabricate_changes():
    first = {
        "attributes": [
            {"key": "email", "value": {"stringValue": "x"}},
            {"key": "count", "value": {"intValue": "1"}},
        ]
    }
    last = {"attributes": list(reversed(first["attributes"]))}
    assert not differences(view(first), view(last))
    assert all(row["status"] == "retained" for row in inventory(view(first), view(last)))


def test_attribute_types_and_special_names_preserved():
    result = view({"attributes": [{"key": "http.user/email", "value": {"intValue": "123"}}]})
    assert result["attributes"]["http.user/email"] == {"intValue": "123"}
    check = {
        "checks": [
            {
                "name": "typed",
                "path": "$.attributes['http.user/email'].intValue",
                "op": "equals",
                "value": "123",
            }
        ]
    }
    assert evaluate(check, result, result)[0]["passed"]


def test_duplicate_attributes_do_not_disappear_in_view():
    with pytest.raises(ReplayError, match="duplicate attribute"):
        view({"attributes": [{"key": "x", "value": {}}, {"key": "x", "value": {}}]})


def test_absence_requires_a_matching_input_control():
    check = {"checks": [{"name": "typo", "path": "$.never_existed", "op": "absent"}]}
    assert not evaluate(check, {"secret": "a"}, {})[0]["passed"]
    check["checks"][0]["path"] = "$.secret"
    assert evaluate(check, {"secret": "a"}, {})[0]["passed"]


def test_equals_requires_all_wildcard_matches_and_real_types():
    spec = {"checks": [{"name": "all", "path": "$.items[*]", "op": "equals", "value": 1}]}
    assert not evaluate(spec, {}, {"items": [1, True]})[0]["passed"]
    assert not evaluate(spec, {}, {"items": []})[0]["passed"]
    assert evaluate(spec, {}, {"items": [1, 1]})[0]["passed"]


def test_forbidden_sentinel_in_key_or_numeric_value_is_visible():
    spec = {"forbid": ["SECRET", "4111"]}
    results = evaluate(spec, {}, {"SECRET-key": {"intValue": "4111"}})
    assert not any(result["passed"] for result in results)
    assert results[0]["matches"][0]["match"] == "key"


def test_forbid_requires_an_exercised_sentinel():
    result = evaluate({"forbid": ["NEVER-PRESENT"]}, {"safe": 1}, {"safe": 1})[0]
    assert not result["passed"]
    assert result["input_matches"] == 0
    assert "not exercised" in result["detail"]


def test_attribute_names_do_not_change_location_classification():
    assert area(("spans", 0, "attributes", "events", "stringValue")) == "record_attributes"
    assert area(("logRecords", 0, "body", "kvlistValue", "values", "attributes")) == "log_body"


def test_preexisting_diagnostic_does_not_become_an_invented_rule_hit():
    before = {"attributes": {"redaction.masked.count": {"intValue": "999"}}}
    observation = diagnostics(before, before)[0]
    assert observation["present_before"]
    assert not observation["changed_in_this_stage"]


@pytest.mark.parametrize(
    "check",
    [
        {"name": "bad", "path": "$.", "op": "equals", "value": 1},
        {"name": "bad", "path": "$.x", "op": "equal", "value": 1},
        {"name": "bad", "path": "$.x", "op": "count", "value": True},
        {"name": "bad", "path": "$.x", "op": "present", "value": None},
        {"name": "bad", "path": "$.x", "op": {}},
        {"name": "bad", "path": "$.x", "op": "equals", "value": float("nan")},
    ],
)
def test_invalid_expectations_fail_before_replay(check):
    with pytest.raises(ReplayError):
        validate({"checks": [check]})
