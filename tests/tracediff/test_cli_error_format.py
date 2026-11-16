# SPDX-License-Identifier: Apache-2.0
import json
import re

import pytest

from .support import EXAMPLES, run

INPUTS = [EXAMPLES / "witness-baseline.json", EXAMPLES / "witness-candidate.json"]


def failing(args):
    r = run([*INPUTS, *args])
    assert r.status == 2 and r.stdout == ""
    return r.stderr


BAD_CAPS = ["null", "0", "-1", "1.5", "NaN", "Infinity", "-Infinity", "1e999", "9007199254740992", "true", '"2"',
            "0x2", " ", ""]


@pytest.mark.parametrize("value", BAD_CAPS)
def test_cli_json_cap_error_is_independent_of_format_position(value):
    args = ["--witnesses", "--witness-source-limit", value]
    before = failing(["--format", "json", *args])
    after = failing([*args, "--format", "json"])
    assert after == before
    error = json.loads(after)["error"]
    assert error["code"] == "INPUT_ERROR"
    assert re.search(r"witness.source.limit", error["message"], re.I)


@pytest.mark.parametrize("name,args,message", [
    ("missing cap value", ["--witnesses", "--witness-source-limit"], r"requires a value"),
    ("duplicate cap", ["--witnesses", "--witness-source-limit", "2", "--witness-source-limit", "2"],
     r"duplicate option --witness-source-limit"),
    ("missing witnesses", ["--witness-source-limit", "2"], r"requires --witnesses"),
    ("duplicate witnesses", ["--witnesses", "--witnesses"], r"duplicate option --witnesses"),
    ("unknown flag", ["--unknown"], r"unsupported option --unknown"),
    ("missing selector value", ["--witnesses", "--witness-path"], r"requires a value"),
    ("missing config value", ["--config"], r"requires a value"),
])
def test_cli_json_usage_error_honors_trailing_format(name, args, message):
    error = json.loads(failing([*args, "--format", "json"]))["error"]
    assert error["code"] == "INPUT_ERROR"
    assert re.search(message, error["message"])


def test_cli_first_format_controls_duplicate_format_errors_and_invalid_formats_remain_errors():
    for later in ("text", "json", "xml"):
        error = json.loads(failing(["--format", "json", "--format", later]))["error"]
        assert error["code"] == "INPUT_ERROR"
        assert "duplicate option --format" in error["message"]
        assert re.match(r"otelwrench tracediff: duplicate option --format", failing(["--format", "text", "--format", later]))
    for args in (["--format"], ["--format", "xml"], ["--format", "xml", "--format", "json"],
                 ["--format", "--format", "json"]):
        assert re.match(r"otelwrench tracediff: --format (requires a value|must be text or json)", failing(args))


@pytest.mark.parametrize("args", [
    ["--witnesses", "--witness-source-limit", "0"],
    ["--witnesses", "--witness-source-limit", "0", "--format", "text"],
    ["--witnesses", "--witness-source-limit", '"--format json"'],
    ["--config", "policy --format json", "--witnesses", "--witness-source-limit", "0"],
    ["--format", "text", "--witness-source-limit", "0", "--format", "json"],
])
def test_cli_text_default_errors_and_values_containing_format_words_are_not_json_format_options(args):
    assert re.match(r"otelwrench tracediff: .*witness-source-limit", failing(args))
