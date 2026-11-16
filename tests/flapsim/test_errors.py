# SPDX-License-Identifier: Apache-2.0
import math

import pytest

from otelwrench.flapsim import compare, parse_json, replay, to_json

from .support import DEFAULTS, cfg, cfgd, matrix, series


def valid():
    return matrix(series([2, 0]))


INVALID_INPUTS = [
    ("error API response", lambda: {"status": "error", "errorType": "bad_data", "error": "invalid query"}, "status success.*invalid query"),
    ("missing API data", lambda: {"status": "success"}, "missing field data"),
    ("scalar type", lambda: {"status": "success", "data": {"resultType": "scalar", "result": [0, "2"]}}, "unsupported resultType"),
    ("vector type", lambda: {"status": "success", "data": {"resultType": "vector", "result": []}}, "unsupported resultType"),
    ("unknown type", lambda: {"status": "success", "data": {"resultType": "custom", "result": []}}, "unsupported resultType"),
    ("non-array result", lambda: {"status": "success", "data": {"resultType": "matrix", "result": {}}}, "expected an array"),
    ("duplicate times", lambda: matrix(series([[0, "2"], [0, "3"]])), "unsorted or duplicate"),
    ("descending times", lambda: matrix(series([[60, "2"], [0, "3"]])), "unsorted or duplicate"),
    ("string timestamp", lambda: matrix(series([["0", "2"]])), "numeric Unix seconds"),
    ("sub-millisecond timestamp", lambda: matrix(series([[0.0001, "2"]])), "millisecond precision"),
    ("off-grid sample", lambda: matrix(series([[0, "2"], [61, "2"], [120, "0"]])), "incompatible with evaluationInterval"),
    ("incompatible end grid", lambda: matrix(series([[0, "2"], [61, "2"]])), "exact multiple"),
    ("duplicate labels reordered", lambda: matrix(series([2], {"a": "1", "b": "2"}), series([2], {"b": "2", "a": "1"})), "duplicate canonical label set"),
    ("non-string labels", lambda: matrix(series([2], {"a": 1})), "values must be strings"),
    ("empty label name", lambda: matrix(series([2], {"": "a"})), "label names must be nonempty"),
    ("null metric", lambda: matrix({"metric": None, "values": [[0, "2"]]}), "expected an object"),
    ("float value not string", lambda: matrix({"metric": {}, "values": [[0, 2]]}), "numeric string"),
    ("null value", lambda: matrix({"metric": {}, "values": [[0, None]]}), "numeric string"),
    ("missing sample element", lambda: matrix({"metric": {}, "values": [[0]]}), r"expected \[timestamp"),
    ("extra sample element", lambda: matrix({"metric": {}, "values": [[0, "2", "extra"]]}), r"expected \[timestamp"),
    ("native histograms", lambda: matrix({"metric": {}, "histograms": [[0, {}]], "values": []}), 'unsupported field "histograms"'),
    ("array metric", lambda: matrix({"metric": [], "values": []}), "expected an object"),
    ("null series", lambda: matrix(None), "expected an object"),
    ("array values required", lambda: matrix({"metric": {}, "values": {}}), "expected an array"),
    ("warnings type", lambda: {**valid(), "warnings": "oops"}, "array of strings"),
    ("infos type", lambda: {**valid(), "infos": [None]}, "array of strings"),
]
for _value in ["stale", "StaleNaN", "", "0x10", " 2", "2 ", "1e999", "nan", "Infinity", "null"]:
    INVALID_INPUTS.append((f"bad float {to_json(_value)}", (lambda v=_value: matrix(series([v]))), "unsupported numeric value"))


@pytest.mark.parametrize("name,make,error", INVALID_INPUTS, ids=[x[0] for x in INVALID_INPUTS])
def test_reject_input(name, make, error):
    with pytest.raises(Exception, match=error):
        replay(make(), cfg())


INVALID_CONFIGS = [
    ("negative for", {"for": "-1s"}, "nonnegative duration"),
    ("negative hold", {"keep_firing_for": "-1s"}, "nonnegative duration"),
    ("zero interval", {"evaluationInterval": "0s"}, "must be positive"),
    ("numeric duration", {"for": 60}, "nonnegative duration"),
    ("compound duration", {"for": "1m30s"}, "nonnegative duration"),
    ("sub-ms duration", {"for": "0.0001s"}, "safe integer milliseconds"),
    ("huge duration", {"for": "9007199254740992ms"}, "safe integer milliseconds"),
    ("unknown option", {"typo": "60s"}, 'unsupported field "typo"'),
    ("PromQL config", {"expr": "up == 0"}, 'unsupported field "expr"'),
    ("regex operator", {"threshold": {"op": "=~", "value": 1}}, "no regex or PromQL"),
    ("PromQL threshold", {"threshold": "rate(foo[5m])"}, "expected an object"),
    ("string threshold", {"threshold": {"op": ">", "value": "1"}}, "finite JSON number"),
    ("NaN threshold", {"threshold": {"op": ">", "value": math.nan}}, "finite JSON number"),
    ("infinity threshold", {"threshold": {"op": ">", "value": math.inf}}, "finite JSON number"),
    ("threshold extra field", {"threshold": {"op": ">", "value": 1, "bool": True}}, 'unsupported field "bool"'),
    ("boolean missing policy", {"missingData": False}, "expected the string"),
    ("unsupported missing policy", {"missingData": "interpolate"}, "expected the string"),
    ("unpaired start", {"start": 0}, "provided together"),
    ("unpaired end", {"end": 60}, "provided together"),
    ("reversed window", {"start": 60, "end": 0}, "end must be >= start"),
    ("range discards sample", {"start": 60, "end": 120}, "outside configured range"),
    ("bad start", {"start": "0", "end": 60}, "numeric Unix seconds"),
    ("oversized timeline", {"start": 0, "end": 60000000}, "exceeds 1000000"),
]


@pytest.mark.parametrize("name,override,error", INVALID_CONFIGS, ids=[x[0] for x in INVALID_CONFIGS])
def test_reject_config(name, override, error):
    with pytest.raises(Exception, match=error):
        replay(valid(), cfgd(override))


def test_required_fields_cannot_silently_default():
    for key in DEFAULTS:
        c = cfg()
        del c[key]
        with pytest.raises(Exception, match=f"missing field {key}"):
            replay(valid(), c)


def test_empty_capture_without_explicit_range_fails():
    with pytest.raises(Exception, match="no samples"):
        replay(matrix(), cfg())
    with pytest.raises(Exception, match="no samples"):
        replay(matrix(series([])), cfg())


def test_compare_refuses_different_windows_or_intervals():
    with pytest.raises(Exception, match="identical evaluation grids"):
        compare(valid(), cfg(start=0, end=60), cfg(start=0, end=120))
    with pytest.raises(Exception, match="identical evaluation grids"):
        compare(valid(), cfg(), cfg(evaluationInterval="30s"))


def test_duplicate_json_keys_and_escaped_equivalents_are_rejected_at_any_depth():
    for text in ['{"a":1,"a":2}', '{"a":1,"\\u0061":2}', '[{"x":{"b":1,"b":2}}]']:
        with pytest.raises(Exception, match="fixture: duplicate JSON object key"):
            parse_json(text, "fixture")
    expected = {"a": [1, True, None, {"a": '"\\'}], "b": 2}
    assert parse_json(f"  {to_json(expected)}  ") == expected
    with pytest.raises(Exception, match="fixture: invalid JSON"):
        parse_json("{bad}", "fixture")


def test_json_nesting_bound_reports_error_without_unbounded_recursive_walk():
    with pytest.raises(Exception, match="nesting exceeds"):
        parse_json("[" * 102 + "0" + "]" * 102)


def test_non_standard_json_constants_are_rejected():
    for text in ["NaN", "[Infinity]", '{"a":-Infinity}']:
        with pytest.raises(Exception, match="invalid JSON"):
            parse_json(text)
