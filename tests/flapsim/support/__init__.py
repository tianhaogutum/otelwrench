# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures and oracles for the alert-flapsim suite (ported from the JS test-support)."""
from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from otelwrench.flapsim import to_json
from otelwrench.flapsim.cli import main
from otelwrench.flapsim.jsonparse import parse_json

SUPPORT = Path(__file__).resolve().parent
EXAMPLES = Path(__file__).resolve().parents[3] / "examples" / "alert-flapsim"
SRC = Path(__file__).resolve().parents[3] / "src"

DEFAULTS = {"evaluationInterval": "60s", "threshold": {"op": ">", "value": 1},
            "for": "0s", "keep_firing_for": "0s", "missingData": "false"}


def cfg(**overrides: Any) -> dict:
    return {**copy.deepcopy(DEFAULTS), **overrides}


def cfgd(overrides: dict) -> dict:
    """``cfg`` for override keys that are not Python identifiers (``for``)."""
    return {**copy.deepcopy(DEFAULTS), **overrides}


def series(values: list, metric: Any = None) -> dict:
    if metric is None:
        metric = {"instance": "a"}
    return {"metric": metric, "values": [v if isinstance(v, list) else [i * 60, str(v) if not isinstance(v, str) else v]
                                         for i, v in enumerate(values)]}


def matrix(*result: dict) -> dict:
    return {"status": "success", "data": {"resultType": "matrix", "result": list(result)}}


def states(report: dict) -> list:
    return [x["state"] for x in report["series"][0]["timeline"]]


def example(name: str) -> Any:
    return parse_json((EXAMPLES / name).read_text("utf-8"), name)


def example_path(name: str) -> str:
    return str(EXAMPLES / name)


def fixture_text(name: str) -> str:
    return (SUPPORT / name).read_bytes().decode("utf-8")


def roundtrip(value: Any) -> Any:
    """``JSON.parse(JSON.stringify(value))``."""
    return parse_json(to_json(value))


def write_json(path: Any, value: Any) -> None:
    Path(path).write_bytes(to_json(value).encode("utf-8"))


def write_raw(path: Any, data: Any) -> None:
    Path(path).write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))


def sha(text: Any) -> str:
    return hashlib.sha256(text if isinstance(text, bytes) else text.encode("utf-8")).hexdigest()


def sort_pairs(labels: dict) -> list:
    return sorted(([k, v] for k, v in labels.items()), key=lambda p: p[0].encode("utf-16-be", "surrogatepass"))


def reverse_keys(value: Any) -> Any:
    """``Object.fromEntries(Object.entries(v).reverse())`` applied recursively."""
    if isinstance(value, list):
        return [reverse_keys(v) for v in value]
    if isinstance(value, dict):
        return {k: reverse_keys(v) for k, v in reversed(list(value.items()))}
    return value


# --- immutability / accessor probes -------------------------------------------------

class FrozenDict(dict):
    def _blocked(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("frozen")

    __setitem__ = __delitem__ = _blocked
    clear = pop = popitem = setdefault = update = __ior__ = _blocked  # type: ignore[assignment]


class FrozenList(list):
    def _blocked(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("frozen")

    __setitem__ = __delitem__ = __iadd__ = __imul__ = _blocked  # type: ignore[assignment]
    append = extend = insert = pop = remove = reverse = sort = clear = _blocked  # type: ignore[assignment]


def freeze(value: Any) -> Any:
    """``Object.freeze`` applied deeply (returns frozen copies)."""
    if isinstance(value, dict):
        return FrozenDict({k: freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return FrozenList(freeze(v) for v in value)
    return value


class Probe(dict):
    """A report whose ``sourceWarnings`` read is counted or forbidden (JS getter)."""

    def __init__(self, base: dict, on_read: Any):
        super().__init__(base)
        self._on_read = on_read

    def __getitem__(self, key: str) -> Any:
        if key == "sourceWarnings":
            return self._on_read()
        return super().__getitem__(key)


def forbid_warnings(report: dict, message: str = "replay happened before preflight") -> Probe:
    def boom() -> Any:
        raise RuntimeError(message)
    return Probe(report, boom)


# --- CLI runners --------------------------------------------------------------------

class Result:
    def __init__(self, status: int, stdout: str, stderr: str):
        self.status, self.stdout, self.stderr = status, stdout, stderr

    def __repr__(self) -> str:
        return f"Result(status={self.status}, stderr={self.stderr!r})"


class _Capture:
    def __init__(self) -> None:
        self.buffer = io.BytesIO()

    def write(self, text: str) -> int:
        self.buffer.write(text.encode("utf-8"))
        return len(text)

    def flush(self) -> None:
        pass

    def text(self) -> str:
        return self.buffer.getvalue().decode("utf-8")


def cli(*args: str, cwd: Any = None) -> Result:
    """Run ``otelwrench alert-flapsim`` in-process and capture its streams."""
    out, err = _Capture(), _Capture()
    previous = os.getcwd()
    try:
        if cwd is not None:
            os.chdir(cwd)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):  # type: ignore[type-var]
            status = main([str(a) for a in args])
    finally:
        os.chdir(previous)
    return Result(status, out.text(), err.text())


def cli_process(*args: str, cwd: Any = None) -> Result:
    """Run the real ``python -m otelwrench.flapsim`` process (startup may be slow on CI)."""
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(SRC), os.environ.get("PYTHONPATH")]))}
    run = subprocess.run([sys.executable, "-m", "otelwrench.flapsim", *map(str, args)], cwd=cwd, env=env,
                         capture_output=True, timeout=120)
    return Result(run.returncode, run.stdout.decode("utf-8"), run.stderr.decode("utf-8"))


def ok(result: Result) -> str:
    assert result.status == 0, result.stderr
    assert result.stderr == ""
    return result.stdout


def success(result: Result) -> Any:
    return parse_json(ok(result))


def bad(result: Result, pattern: str) -> None:
    import re
    assert result.status == 1, (result.stdout[:200], result.stderr)
    assert result.stdout == "", "no partial output on failure"
    assert re.search(pattern, result.stderr), result.stderr


# --- CSV reader (parse quoted cells instead of splitting on commas) ----------------------

def csv_rows(text: str) -> list:
    records: list = []
    record: list = []
    cell = ""
    quoted = False
    i = 0
    while i < len(text):
        c = text[i]
        if c == '"':
            if quoted and i + 1 < len(text) and text[i + 1] == '"':
                cell += '"'
                i += 1
            else:
                quoted = not quoted
        elif not quoted and c == ",":
            record.append(cell)
            cell = ""
        elif not quoted and c == "\r" and i + 1 < len(text) and text[i + 1] == "\n":
            record.append(cell)
            records.append(record)
            record, cell = [], ""
            i += 1
        else:
            cell += c
        i += 1
    assert quoted is False, "closed CSV quotes"
    assert record == [], "trailing CRLF"
    assert cell == ""
    header, *rows = records
    assert len(header) > 1
    assert len(set(header)) == len(header), "unique column names"
    result = []
    for row in rows:
        assert len(row) == len(header), "uniform column count"
        result.append(dict(zip(header, row)))
    return result


# --- report fixtures ----------------------------------------------------------------

def replay_with(api: Any, data: dict, config: dict, checkpoint: Any = None) -> dict:
    return api.replay(data, config) if checkpoint is None else api.replay(data, config, checkpoint)


def slice_matrix(data: dict, start: float, end: float) -> dict:
    return {**data, "data": {**data["data"], "result": [
        {**s, "values": [v for v in s["values"] if start <= v[0] <= end]} for s in data["data"]["result"]]}}


def split_reports(api: Any, data: dict, config: dict, bounds: Any = None) -> list:
    if bounds is None:
        bounds = [[0, 60], [120, 240], [300, 360]]
    checkpoint = None
    reports = []
    for start, end in bounds:
        r = replay_with(api, slice_matrix(data, start, end), {**config, "start": start, "end": end}, checkpoint)
        checkpoint = r["checkpoint"]
        reports.append(r)
    return reports


def report_matrix() -> dict:
    return matrix(series([0, 2, 2, 0, 2, 2, 0]), series([0, 2, 0, 0, 0, 0, 0], {"instance": "b"}))


def baseline_config() -> dict:
    return cfg(start=0, end=360)


def candidate_config() -> dict:
    return cfgd({"start": 0, "end": 360, "for": "60s", "keep_firing_for": "120s"})


AGGREGATE_DELTA = {"firstFiringDelaySeconds": 60, "firingDurationSeconds": -60,
                   "firingEpisodes": -2, "resolutions": -3, "openAtEnd": 1}
LABEL_DELTAS = [
    {"firstFiringDelaySeconds": 60, "firingDurationSeconds": 0, "firingEpisodes": -1, "resolutions": -2, "openAtEnd": 1},
    {"firstFiringDelaySeconds": None, "firingDurationSeconds": -60, "firingEpisodes": -1, "resolutions": -1, "openAtEnd": 0},
]


def joined_pair(api: Any) -> list:
    return [api.join(split_reports(api, report_matrix(), c)) for c in (baseline_config(), candidate_config())]


TIMES = [1.001, 1.061, 1.121, 1.181, 1.241, 1.301, 1.361]
SET_BOUNDS = [[1.001, 1.061], [1.121, 1.241], [1.301, 1.361]]


def joined_set(api: Any) -> list:
    data = matrix(*[series([[t, str(values[j])] for j, t in enumerate(TIMES)], {"instance": "b" if i == 0 else "a"})
                    for i, values in enumerate([[0, 2, 0, 0, 0, 0, 0], [0, 2, 2, 0, 2, 2, 0]])])
    base = cfgd({"evaluationInterval": "60ms", "for": "60ms", "start": TIMES[0], "end": TIMES[-1]})
    configs = [base,
               {**base, "threshold": {"op": ">=", "value": 2}, "for": "0ms", "keep_firing_for": "120ms", "missingData": "error"},
               {**base, "threshold": {"op": ">", "value": 3}, "for": "120ms", "keep_firing_for": "60ms"}]
    return [api.join(split_reports(api, data, c, SET_BOUNDS)) for c in configs]


def summary_csv_set(api: Any) -> list:
    capture = matrix(*[series([[t, str(values[j])] for j, t in enumerate(TIMES)], {"instance": "b\r\n" if i == 0 else 'a,"\n'})
                       for i, values in enumerate([[0, 2, 0, 0, 0, 0, 0], [0, 2, 2, 0, 2, 2, 0]])])
    capture["warnings"] = ['w,"\n', 'w,"\n']
    capture["infos"] = ["i\r\n"]
    base = cfgd({"evaluationInterval": "60ms", "for": "60ms", "start": TIMES[0], "end": TIMES[-1]})
    configs = [base, {**base, "threshold": {"op": ">=", "value": 2}, "for": "0ms", "keep_firing_for": "120ms"},
               {**base, "threshold": {"op": ">", "value": 3}, "for": "120ms", "keep_firing_for": "60ms"}]
    return [api.join(split_reports(api, capture, c, SET_BOUNDS)) for c in configs]


# Hand-computed episodes: baseline a [.121,.181),[.301,.361), b never;
# candidate0 a [.061,end) open; b [.061,.241); candidate1 never fires.
SET_METRICS = [
    [1.121, 0.12, 2, 2, 0], [1.121, 0.12, 2, 2, False], [None, 0, 0, 0, False],
    [1.061, 0.48, 2, 1, 1], [1.061, 0.3, 1, 0, True], [1.061, 0.18, 1, 1, False],
    [None, 0, 0, 0, 0], [None, 0, 0, 0, False], [None, 0, 0, 0, False],
]
SET_DELTAS = [
    [-0.06, 0.36, 0, -1, 1], [-0.06, 0.18, -1, -2, 1], [None, 0.18, 1, 1, 0],
    [None, -0.12, -2, -2, 0], [None, -0.12, -2, -2, 0], [None, 0, 0, 0, 0],
]


def _strict(values: list) -> list:
    """Make bool/number distinctions visible to ``==`` like assert.deepStrictEqual."""
    return [(type(v) is bool, v) if v is not None else None for v in values]


def assert_set_json(result: dict) -> None:
    assert result["schemaVersion"] == 1
    assert result["command"] == "compare-report-set"
    assert result["budget"] == {"maxReports": 256, "reports": 3, "candidates": 2,
                                "maxEvaluations": 1000000, "requiredEvaluations": 42}
    assert [c["candidateIndex"] for c in result["candidates"]] == [0, 1]
    runs = [result["baseline"], *[c["report"] for c in result["candidates"]]]
    metric = lambda s: [s["firstFiring"], s["firingDurationSeconds"], s["firingEpisodes"], s["resolutions"], s["openAtEnd"]]  # noqa: E731
    got = [metric(s) for r in runs for s in [r["summary"], *[x["summary"] for x in r["series"]]]]
    assert [_strict(m) for m in got] == [_strict(m) for m in SET_METRICS]
    delta = lambda s: [s["firstFiringDelaySeconds"], s["firingDurationSeconds"], s["firingEpisodes"], s["resolutions"], s["openAtEnd"]]  # noqa: E731
    assert [delta(d) for c in result["candidates"] for d in [c["delta"], *c["seriesDeltas"]]] == SET_DELTAS
    for r in runs:
        assert [s["id"] for s in r["series"]] == ['[["instance","a"]]', '[["instance","b"]]']
        assert r["window"]["start"] == 1.001
        assert r["window"]["end"] == 1.361
        assert r["checkpoint"]["originStart"] == 1.001
    assert [r["config"]["threshold"] for r in runs] == [{"op": ">", "value": 1}, {"op": ">=", "value": 2}, {"op": ">", "value": 3}]
    assert [r["config"]["missingData"] for r in runs] == ["false", "error", "false"]
    assert runs[1]["series"][0]["episodes"][0]["censored"] is True
    assert runs[1]["series"][0]["episodes"][0]["end"] is None


def _js_str(value: Any) -> str:
    from otelwrench.flapsim._js import js_string
    return js_string(value)


def assert_set_csv(text: str) -> None:
    rows = csv_rows(text)
    assert len(rows) == 9
    expected = []
    for i in range(3):
        role, index = ("candidate", str(i - 1)) if i else ("baseline", "null")
        expected.append([role, index, "aggregate", "null"])
        expected.extend([role, index, "series", f'[["instance","{label}"]]'] for label in ("a", "b"))
    assert [[r["role"], r["candidateIndex"], r["scope"], r["seriesId"]] for r in rows] == expected
    assert [[r["firstFiring"], r["firingDurationSeconds"], r["firingEpisodes"], r["resolutions"], r["openAtEnd"]] for r in rows] == [
        [_js_str(int(v) if i == 4 else v) for i, v in enumerate(m)] for m in SET_METRICS]
    assert [[r["firstFiringDelaySeconds"], r["deltaFiringDurationSeconds"], r["deltaFiringEpisodes"], r["deltaResolutions"],
             r["deltaOpenAtEnd"]] for r in rows[3:]] == [[_js_str(v) for v in d] for d in SET_DELTAS]
    assert all(r["firstFiringDelaySeconds"] == "null" and r["deltaFiringDurationSeconds"] == "null" for r in rows[:3])
    for i, r in enumerate(rows):
        run = i // 3
        assert r["forMs"] == ["60", "0", "120"][run]
        assert r["keepFiringForMs"] == ["0", "120", "60"][run]
        assert r["baselineForMs"] == "60"
        assert r["evaluationIntervalMs"] == "60"
        assert r["thresholdJson"] == ['{"op":">","value":1}', '{"op":">=","value":2}', '{"op":">","value":3}'][run]
        assert r["missingData"] == ["false", "error", "false"][run]
        assert r["windowStart"] == "1.001"
        assert r["windowEnd"] == "1.361"
        assert r["pendingAtEnd"] == "0"
        assert r["deltaPendingAtEnd"] == ("0" if run else "null")
        assert r["transitionCount"] == ["8", "6", "2", "3", "1", "2", "0", "0", "0"][i]
        assert r["deltaTransitionCount"] == ["null", "null", "null", "-5", "-5", "0", "-8", "-6", "-2"][i]


# --- identity oracles -------------------------------------------------------------------

JOINED_RULE_BYTES = [
    '["alert-flapsim-rule-v1","60","60","0",">",1,"false"]',
    '["alert-flapsim-rule-v1","60","0","120",">=",2,"error"]',
    '["alert-flapsim-rule-v1","60","120","60",">",3,"false"]',
]


def rule_oracle(text: str) -> dict:
    return {"schemaVersion": 1, "algorithm": "sha256", "encoding": "alert-flapsim-rule-v1", "digest": sha(text)}


def assert_joined_rule_identities(result: dict) -> None:
    runs = [result["baseline"], *[c["report"] for c in result["candidates"]]]
    assert [r["ruleIdentity"] for r in runs] == [rule_oracle(b) for b in JOINED_RULE_BYTES]
    assert "ruleIdentity" not in result, "rules belong to each replay summary"


# Fully specified oracle, independent of report data and production encoding.
# Exact ms 1,61,121,181; a: 2.0,missing,NaN,-0; b: 0,2,2,0.
CANONICAL_CAPTURE = ('["alert-flapsim-capture-v1","1","181","60","4",[[[["10","ten"],["2","two"],["instance","a"],'
                     '["zone","é\\n\\"\\\\"]],["2.0",null,"NaN","-0"]],[[["10","ten"],["2","two"],["instance","b"],'
                     '["zone","é\\n\\"\\\\"]],["0","2","2","0"]]]]')


def identity_oracle() -> dict:
    return {"schemaVersion": 1, "algorithm": "sha256", "encoding": "alert-flapsim-capture-v1", "digest": sha(CANONICAL_CAPTURE)}


def identity_config() -> dict:
    return cfg(evaluationInterval="60ms", start=0.001, end=0.181)


def identity_matrix() -> dict:
    # Key order mirrors the JS object literal { zone, instance, 2, 10 } (integer keys first).
    return matrix(series([[0.001, "0"], [0.061, "2"], [0.121, "2"], [0.181, "0"]],
                         {"2": "two", "10": "ten", "zone": 'é\n"\\', "instance": "b"}),
                  series([[0.001, "2.0"], [0.121, "NaN"], [0.181, "-0"]],
                         {"2": "two", "10": "ten", "zone": 'é\n"\\', "instance": "a"}))


def identity_set(api: Any, data: Any = None, config: Any = None) -> list:
    data = identity_matrix() if data is None else data
    config = identity_config() if config is None else config
    configs = [config, {**config, "for": "60ms", "keep_firing_for": "120ms", "threshold": {"op": "!=", "value": 2}}]
    return [api.join(split_reports(api, data, c, [[0.001, 0.061], [0.121, 0.181]])) for c in configs]


def assert_identity(result: dict) -> None:
    assert result["captureIdentity"] == identity_oracle()
    assert result["detail"] == "summary"
    assert len(result["baseline"]["series"]) == 2
    assert [s["summary"]["missingSamples"] for s in result["baseline"]["series"]] == [1, 0]
    assert [s["summary"]["nanSamples"] for s in result["baseline"]["series"]] == [1, 0]


def assert_set_summary(result: dict) -> None:
    """Hand-calculated from joined_set's 60 ms samples."""
    assert result["schemaVersion"] == 1
    assert result["command"] == "compare-report-set"
    assert result["detail"] == "summary"
    assert_joined_rule_identities(result)
    capture = ('["alert-flapsim-capture-v1","1001","1361","60","7",[[[["instance","a"]],["0","2","2","0","2","2","0"]],'
               '[[["instance","b"]],["0","2","0","0","0","0","0"]]]]')
    assert result["captureIdentity"] == {"schemaVersion": 1, "algorithm": "sha256",
                                         "encoding": "alert-flapsim-capture-v1", "digest": sha(capture)}
    assert result["budget"] == {"maxReports": 256, "reports": 3, "candidates": 2,
                                "maxEvaluations": 1000000, "requiredEvaluations": 42}
    assert [c["candidateIndex"] for c in result["candidates"]] == [0, 1]
    runs = [result["baseline"], *[c["report"] for c in result["candidates"]]]
    values = lambda s: [s["firstFiring"], s["firingDurationSeconds"], s["firingEpisodes"], s["resolutions"],  # noqa: E731
                        int(s["openAtEnd"]), s["pendingAtEnd"], s["censoredEpisodes"], s["transitionCount"]]
    expected = [
        [1.121, .12, 2, 2, 0, 0, 0, 8], [1.121, .12, 2, 2, 0, False, 0, 6], [None, 0, 0, 0, 0, False, 0, 2],
        [1.061, .48, 2, 1, 1, 0, 1, 3], [1.061, .3, 1, 0, 1, False, 1, 1], [1.061, .18, 1, 1, 0, False, 0, 2],
        [None, 0, 0, 0, 0, 0, 0, 0], [None, 0, 0, 0, 0, False, 0, 0], [None, 0, 0, 0, 0, False, 0, 0],
    ]
    got = [values(s) for r in runs for s in [r["summary"], *[x["summary"] for x in r["series"]]]]
    assert [_strict(v) for v in got] == [_strict(v) for v in expected]
    d_values = lambda d: [d["firstFiringDelaySeconds"], d["firingDurationSeconds"], d["firingEpisodes"], d["resolutions"],  # noqa: E731
                          d["openAtEnd"], d["pendingAtEnd"], d["censoredEpisodes"], d["transitionCount"]]
    assert [d_values(d) for c in result["candidates"] for d in [c["delta"], *c["seriesDeltas"]]] == [
        [-.06, .36, 0, -1, 1, 0, 1, -5], [-.06, .18, -1, -2, 1, 0, 1, -5], [None, .18, 1, 1, 0, 0, 0, 0],
        [None, -.12, -2, -2, 0, 0, 0, -8], [None, -.12, -2, -2, 0, 0, 0, -6], [None, 0, 0, 0, 0, 0, 0, -2],
    ]
    for i, r in enumerate(runs):
        assert r["schemaVersion"] == 1
        assert r["command"] == "replay-summary"
        assert sorted(r) == sorted(["schemaVersion", "command", "config", "exactConfig", "ruleIdentity", "window", "origin",
                                    "assumptions", "sourceWarnings", "sourceInfos", "summary", "series"])
        assert r["origin"] == {"originStart": 1.001, "evaluatedThrough": 1.361}
        assert r["window"] == {"start": 1.001, "end": 1.361, "evaluations": 7, "rangeSource": "config"}
        assert r["exactConfig"] == {"evaluationInterval": "60ms", "threshold": {"op": ">=" if i == 1 else ">", "value": [1, 2, 3][i]},
                                    "for": ["60ms", "0ms", "120ms"][i], "keep_firing_for": ["0ms", "120ms", "60ms"][i],
                                    "missingData": "error" if i == 1 else "false"}
        assert r["config"]["forSeconds"] == [.06, 0, .12][i]
        assert r["config"]["keepFiringForSeconds"] == [0, .12, .06][i]
        assert r["config"]["evaluationIntervalSeconds"] == .06
        assert r["config"]["threshold"] == r["exactConfig"]["threshold"]
        assert r["config"]["missingData"] == r["exactConfig"]["missingData"]
        assert [s["id"] for s in r["series"]] == ['[["instance","a"]]', '[["instance","b"]]']
        for s in r["series"]:
            assert sorted(s) == ["id", "labels", "summary"]
            assert s["labels"] == {"instance": "a" if '"a"' in s["id"] else "b"}
        for s in [r["summary"], *[x["summary"] for x in r["series"]]]:
            assert s["missingSamples"] == 0
            assert s["nanSamples"] == 0

    def forbidden(value: Any) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                assert k not in ("timeline", "checkpoint", "transitions", "episodes"), f"expanded {k} leaked"
                forbidden(v)
        elif isinstance(value, list):
            for v in value:
                forbidden(v)
    forbidden(result)


# Independent episodes: a [60,180),[240,360), b [60,120); candidate
# a open from 120 to capture end 360 (240 observed seconds), b never fires.
def assert_reports_csv(text: str) -> list:
    rows = csv_rows(text)
    assert len(rows) == 6
    assert [[r["role"], r["scope"], r["seriesId"]] for r in rows] == [
        ["baseline", "aggregate", "null"], ["baseline", "series", '[["instance","a"]]'], ["baseline", "series", '[["instance","b"]]'],
        ["candidate", "aggregate", "null"], ["candidate", "series", '[["instance","a"]]'], ["candidate", "series", '[["instance","b"]]'],
    ]
    expected = [
        ["60", "300", "3", "3", "0", "null", "null", "null", "null", "null"],
        ["60", "240", "2", "2", "0", "null", "null", "null", "null", "null"],
        ["60", "60", "1", "1", "0", "null", "null", "null", "null", "null"],
        ["120", "240", "1", "0", "1", "60", "-60", "-2", "-3", "1"],
        ["120", "240", "1", "0", "1", "60", "0", "-1", "-2", "1"],
        ["null", "0", "0", "0", "0", "null", "-60", "-1", "-1", "0"],
    ]
    for i, r in enumerate(rows):
        assert [r["firstFiring"], r["firingDurationSeconds"], r["firingEpisodes"], r["resolutions"], r["openAtEnd"],
                r["firstFiringDelaySeconds"], r["deltaFiringDurationSeconds"], r["deltaFiringEpisodes"], r["deltaResolutions"],
                r["deltaOpenAtEnd"]] == expected[i]
        assert r["schemaVersion"] == "1"
        assert r["candidateIndex"] == ("null" if i < 3 else "0")
        assert r["seriesCount"] == ("2" if i % 3 == 0 else "1")
        assert r["baselineForMs"] == "0"
        assert r["baselineKeepFiringForMs"] == "0"
        assert r["forMs"] == ("0" if i < 3 else "60000")
        assert r["keepFiringForMs"] == ("0" if i < 3 else "120000")
        assert r["evaluationIntervalMs"] == "60000"
        assert r["windowStart"] == "0"
        assert r["windowEnd"] == "360"
        assert r["rangeSource"] == "config"
        assert r["thresholdJson"] == '{"op":">","value":1}'
        assert r["missingData"] == "false"
        assert r["pendingAtEnd"] == "0"
        assert r["deltaPendingAtEnd"] == ("null" if i < 3 else "0")
        assert r["transitionCount"] == ["6", "4", "2", "4", "2", "2"][i]
        assert r["deltaTransitionCount"] == ("null" if i < 3 else ["-2", "-2", "0"][i - 3])
        if i < 3:
            assert all(v == "null" for k, v in r.items() if k.startswith("delta"))
    return rows


# Independent interval oracle: baseline [60,180),[240,360),[420,540),
# [720,840),[900,1020); candidate [120,660),[780,1140). Both resolve.
def assert_jitter_csv(text: str) -> list:
    rows = csv_rows(text)
    assert len(rows) == 4
    assert [[r["role"], r["candidateIndex"], r["scope"]] for r in rows] == [
        ["baseline", "null", "aggregate"], ["baseline", "null", "series"],
        ["candidate", "0", "aggregate"], ["candidate", "0", "series"],
    ]
    for i, r in enumerate(rows):
        candidate = i >= 2
        assert r["schemaVersion"] == "1"
        assert r["seriesCount"] == "1"
        assert r["firstFiring"] == ("120" if candidate else "60")
        assert r["firingDurationSeconds"] == ("900" if candidate else "600")
        assert r["firingEpisodes"] == ("2" if candidate else "5")
        assert r["resolutions"] == ("2" if candidate else "5")
        assert r["firstFiringDelaySeconds"] == ("60" if candidate else "null")
        assert r["openAtEnd"] == "0"
        assert r["pendingAtEnd"] == "0"
        assert r["baselineForMs"] == "0"
        assert r["baselineKeepFiringForMs"] == "0"
        assert r["forMs"] == ("60000" if candidate else "0")
        assert r["keepFiringForMs"] == ("120000" if candidate else "0")
        assert r["evaluationIntervalMs"] == "60000"
        assert r["seriesId"] == ("null" if i % 2 == 0 else to_json([["__name__", "synthetic_jitter"], ["instance", "jitter-79"]]))
        assert r["deltaFiringDurationSeconds"] == ("300" if candidate else "null")
        assert r["deltaFiringEpisodes"] == ("-3" if candidate else "null")
        assert r["deltaResolutions"] == ("-3" if candidate else "null")
        assert r["deltaOpenAtEnd"] == ("0" if candidate else "null")
        assert r["deltaPendingAtEnd"] == ("0" if candidate else "null")
        if not candidate:
            assert all(v == "null" for k, v in r.items() if k.startswith("delta"))
    return rows


# Hand-derived for examples/alert-flapsim/grid-query-range.json, independent of replay code.
# Evaluations: true, true, false, MISSING, true, true, false, true at 0..420.
EXPECTED_GRID = [
    {"forSeconds": 0, "keepFiringForSeconds": 0, "transitionCount": 5, "firstFiring": 0,
     "firstFiringDelaySeconds": 0, "firingDurationSeconds": 240, "pendingAtEnd": False,
     "transitions": [[0, "firing"], [120, "inactive"], [240, "firing"], [360, "inactive"], [420, "firing"]],
     "episodes": [{"start": 0, "end": 120, "durationSeconds": 120, "censored": False},
                  {"start": 240, "end": 360, "durationSeconds": 120, "censored": False},
                  {"start": 420, "end": None, "durationSeconds": 0, "censored": True}]},
    {"forSeconds": 0, "keepFiringForSeconds": 120, "transitionCount": 1, "firstFiring": 0,
     "firstFiringDelaySeconds": 0, "firingDurationSeconds": 420, "pendingAtEnd": False,
     "transitions": [[0, "firing"]],
     "episodes": [{"start": 0, "end": None, "durationSeconds": 420, "censored": True}]},
    {"forSeconds": 60, "keepFiringForSeconds": 0, "transitionCount": 7, "firstFiring": 60,
     "firstFiringDelaySeconds": 60, "firingDurationSeconds": 120, "pendingAtEnd": True,
     "transitions": [[0, "pending"], [60, "firing"], [120, "inactive"], [240, "pending"],
                     [300, "firing"], [360, "inactive"], [420, "pending"]],
     "episodes": [{"start": 60, "end": 120, "durationSeconds": 60, "censored": False},
                  {"start": 300, "end": 360, "durationSeconds": 60, "censored": False}]},
    {"forSeconds": 60, "keepFiringForSeconds": 120, "transitionCount": 2, "firstFiring": 60,
     "firstFiringDelaySeconds": 60, "firingDurationSeconds": 360, "pendingAtEnd": False,
     "transitions": [[0, "pending"], [60, "firing"]],
     "episodes": [{"start": 60, "end": None, "durationSeconds": 360, "censored": True}]},
]


# --- seeded jitter experiment (formerly scripts/jitter-fixture.js and experiment.js) -------

SEED = 79
TRUTH_PATTERN = [0, 1, 1, 0, 1, 1, 0, 1, 1, 0, 0, 0, 1, 1, 0, 1, 1, 0, 0, 0]


def generate_jitter() -> dict:
    from otelwrench.flapsim._js import to_fixed
    state = SEED

    def random() -> float:
        nonlocal state
        state = (1664525 * state + 1013904223) & 0xFFFFFFFF  # Math.imul + >>> 0
        return state / 4294967296

    # An explicitly chosen finite scenario, not a statistical traffic model.
    # +/-0.01 noise keeps every point on its intended side of threshold 1.
    values = []
    for i, truth in enumerate(TRUTH_PATTERN):
        values.append([i * 60, to_fixed((1.02 if truth else 0.98) + (random() - 0.5) * 0.02, 6)])
    return {"status": "success", "data": {"resultType": "matrix", "result": [{
        "metric": {"__name__": "synthetic_jitter", "instance": "jitter-79"}, "values": values}]}}


def experiment_result(api: Any) -> str:
    data = example("jitter-query-range.json")
    assert data == generate_jitter(), "saved fixture must match deterministic generator"
    assert [1 if float(v) > 1 else 0 for _, v in data["data"]["result"][0]["values"]] == TRUTH_PATTERN
    report = api.compare(data, example("baseline.json"), example("candidate.json"))
    # Independent interval arithmetic for this finite capture, not replay-derived expectations.
    baseline_episodes = [[60, 180], [240, 360], [420, 540], [720, 840], [900, 1020]]
    candidate_episodes = [[120, 660], [780, 1140]]

    def check(run: dict, expected: list) -> None:
        assert [[e["start"], e["end"]] for e in run["series"][0]["episodes"]] == expected
        assert run["summary"]["firstFiring"] == expected[0][0]
        assert run["summary"]["firingEpisodes"] == len(expected)
        assert run["summary"]["resolutions"] == len(expected)
        assert run["summary"]["firingDurationSeconds"] == sum(end - start for start, end in expected)
        assert run["summary"]["openAtEnd"] == 0

    check(report["baseline"], baseline_episodes)
    check(report["candidate"], candidate_episodes)
    return to_json({
        "experiment": "seeded finite-capture jitter replay", "seed": SEED,
        "fixture": "examples/jitter-query-range.json", "samples": len(TRUTH_PATTERN),
        "window": report["baseline"]["window"],
        "baseline": {"config": report["baseline"]["config"], "summary": report["baseline"]["summary"], "episodes": baseline_episodes},
        "candidate": {"config": report["candidate"]["config"], "summary": report["candidate"]["summary"], "episodes": candidate_episodes},
        "delta": report["delta"],
        "validation": "fixture regeneration, threshold classification, and hand-calculated episode endpoints and counts passed",
        "limitations": [
            "Chosen synthetic pattern with small seeded noise; no statistical or production-performance claim.",
            "Fewer resolutions cost later first firing and more time firing during false conditions.",
            "No best threshold is selected. Results are limited to the explicit finite capture and initial inactive state.",
        ],
    }, 2) + "\n"


def jitter_text() -> str:
    return to_json(generate_jitter(), 2) + "\n"
