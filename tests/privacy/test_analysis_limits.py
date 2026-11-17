import pytest

from otelwrench.privacy.expectations import evaluate, validate
from otelwrench.privacy.inputs import ReplayError, bound_document


@pytest.mark.parametrize(
    "path",
    [
        "$.x + $.y",
        '$[?(@.x =~ "a+")]',
        "$.x.`sub(/a/, b)`",
        "$..*..*",
        "$['a','a']",
        "$.x[::0]",
        "$.items[*].$",
    ],
)
def test_computed_or_amplifying_queries_are_rejected(path):
    with pytest.raises(ReplayError):
        validate({"checks": [{"name": "query", "path": path, "op": "present"}]})


def test_simple_filters_and_one_recursive_selector_address_actual_values():
    doc = {"items": [{"key": "safe", "value": "control"}, {"key": "other", "value": "x"}]}
    spec = {
        "checks": [
            {
                "name": "control",
                "path": '$.items[?(@.key == "safe")].value',
                "op": "equals",
                "value": "control",
            },
            {"name": "keys", "path": "$..key", "op": "count", "value": 2},
        ]
    }
    assert all(row["passed"] for row in evaluate(spec, doc, doc))


def test_analysis_budget_rejects_wide_or_deep_documents():
    with pytest.raises(ReplayError, match="analysis budget"):
        bound_document({"items": list(range(20))}, max_nodes=10)
    with pytest.raises(ReplayError, match="analysis budget"):
        bound_document({"a": {"b": {"c": 1}}}, max_depth=2)
    bound_document({"items": [1, 2, 3]}, max_nodes=10)


def test_inapplicable_filter_fails_cleanly_instead_of_a_traceback():
    spec = {"checks": [{"name": "numeric", "path": "$.items[?(@.x > 0)]", "op": "present"}]}
    with pytest.raises(ReplayError, match="selector cannot be applied"):
        evaluate(spec, {"items": [{"x": {}}]}, {"items": [{"x": {}}]})
