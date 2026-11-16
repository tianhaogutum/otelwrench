# SPDX-License-Identifier: Apache-2.0
"""Known-byte export modes shared by the --output, --manifest and --names suites."""
import otelwrench.flapsim as api
from otelwrench.flapsim import parse_json

from .support import assert_set_csv, assert_set_json, assert_set_summary, fixture_text, joined_set, summary_csv_set


def _literal_v1(text):
    assert text == fixture_text("report-set-summary-v1.csv")


MODES = [
    {"flags": [], "hash": "e15ba058c4513dc349cfe4c26c70f09f7fca3007446b2165d9ba9efddcad5487",
     "check": lambda s: assert_set_json(parse_json(s)), "reports": joined_set},
    {"flags": ["--detail", "summary"], "hash": "2b8cdc2ce3e64ff47276a7cb3274b970cbd6a03d0f8a9d0210b5caa04770510c",
     "check": lambda s: assert_set_summary(parse_json(s)), "reports": joined_set},
    {"flags": ["--format", "csv"], "hash": "082ab68ccb5e150f3235693cd196f24e6fac46ffcc4f796a4b943c3ebce59e3a",
     "check": assert_set_csv, "reports": joined_set},
    {"flags": ["--detail", "summary", "--format", "csv"], "hash": None, "check": _literal_v1, "reports": summary_csv_set},
]


def make_unsafe():
    from .support import cfg, matrix, series
    unsafe = api.replay(matrix(series([2]), series([2], {"instance": "b"})), cfg())
    unsafe["config"]["evaluationIntervalSeconds"] = 4503599627370.495
    unsafe["checkpoint"]["config"]["evaluationInterval"] = "4503599627370495ms"
    unsafe["window"]["end"] = 9007199254740.99
    unsafe["window"]["evaluations"] = 3
    unsafe["checkpoint"]["evaluatedThrough"] = unsafe["window"]["end"]
    return unsafe


def empty_steps(steps):
    from .support import cfg, matrix
    return api.replay(matrix(), cfg(start=0, end=(steps - 1) * 60))
