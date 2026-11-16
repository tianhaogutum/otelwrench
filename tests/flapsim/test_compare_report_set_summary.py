# SPDX-License-Identifier: Apache-2.0
import copy
import os
import tempfile

import pytest

import otelwrench.flapsim as api
from otelwrench.flapsim import parse_json, to_json

from .support import (
    Probe, assert_set_summary, bad, cfg, cfgd, cli, forbid_warnings, freeze, joined_set, matrix, ok, series, sha,
    write_json, write_raw,
)

FULL_SHA = "e15ba058c4513dc349cfe4c26c70f09f7fca3007446b2165d9ba9efddcad5487"
CSV_SHA = "082ab68ccb5e150f3235693cd196f24e6fac46ffcc4f796a4b943c3ebce59e3a"


def summarize(b, cs):
    return api.compare_report_set(b, cs, {"detail": "summary"})


def run(*args):
    return cli("compare-report-set", *args)


def files(tmp_path, reports=None, prefix=""):
    reports = joined_set(api) if reports is None else reports
    paths = []
    for i, r in enumerate(reports):
        p = tmp_path / f"{prefix}{i}.json"
        write_json(p, r)
        paths.append(p)
    return paths


def test_summary_library_independent_joined_two_label_oracle_exact_ms_and_immutable_inputs():
    reports = joined_set(api)
    before = copy.deepcopy(reports)
    frozen = freeze(reports)
    result = api.compare_report_set(frozen[0], frozen[1:], freeze({"detail": "summary"}))
    assert_set_summary(result)
    assert frozen == before


def test_summary_cli_oracle_and_full_default_json_and_csv_prior_byte_compatibility(tmp_path):
    paths = files(tmp_path)
    raw = [p.read_bytes() for p in paths]
    full = ok(run(*paths))
    assert sha(full) == FULL_SHA
    summary = ok(run(*paths, "--detail", "summary"))
    assert_set_summary(parse_json(summary))
    base, *candidates = joined_set(api)
    assert summary == to_json(summarize(base, candidates), 2) + "\n"
    for flags in (["--detail", "full"], ["--format", "json"], ["--detail", "full", "--format", "json"]):
        assert ok(run(*paths, *flags)) == full
    csv_text = ok(run(*paths, "--format", "csv"))
    assert sha(csv_text) == CSV_SHA
    assert ok(run(*paths, "--format", "csv", "--detail", "full")) == csv_text
    assert ok(run(*paths, "--format", "json", "--detail", "summary")) == summary
    assert ok(run(*paths, "--detail", "summary", "--format", "json")) == summary
    assert [p.read_bytes() for p in paths] == raw


def test_summary_preserves_diagnostics_context_canonical_labels_candidate_order_and_baseline_once():
    base, *candidates = joined_set(api)
    base["sourceWarnings"] = ["w", "w"]
    candidates[1]["sourceInfos"] = ["i", "i"]
    full, result = api.compare_report_set(base, candidates), summarize(base, candidates)
    full_runs = [full["baseline"], *[c["report"] for c in full["candidates"]]]
    for i, r in enumerate([result["baseline"], *[c["report"] for c in result["candidates"]]]):
        f = full_runs[i]
        for key in ("config", "window", "assumptions", "sourceWarnings", "sourceInfos"):
            assert r[key] == f[key]
        for key, v in f["summary"].items():
            assert r["summary"][key] == v
        for j, s in enumerate(r["series"]):
            for key, v in f["series"][j]["summary"].items():
                assert s["summary"][key] == v
    assert result["baseline"]["sourceWarnings"] == ["w", "w"]
    assert result["candidates"][1]["report"]["sourceInfos"] == ["i", "i"]
    assert result["candidates"][0]["report"]["sourceInfos"] == []
    for i, c in enumerate(result["candidates"]):
        for key, v in full["candidates"][i]["delta"].items():
            assert c["delta"][key] == v
    reversed_result = summarize(base, candidates[::-1])
    assert [c["report"] for c in reversed_result["candidates"]] == [c["report"] for c in result["candidates"]][::-1]
    assert [c["candidateIndex"] for c in reversed_result["candidates"]] == [0, 1]
    scrambled = copy.deepcopy([base, *candidates])
    for r in scrambled:
        r["series"].reverse()
        r["checkpoint"]["series"].reverse()
    assert summarize(scrambled[0], scrambled[1:]) == result
    reads = [0]

    def count():
        reads[0] += 1
        return []

    probe = Probe({**base, "sourceWarnings": []}, count)
    summarize(probe, [candidates[0]])
    once = reads[0]
    reads[0] = 0
    summarize(probe, candidates)
    assert once > 0
    assert reads[0] == once


def test_summary_distinguishes_pending_and_right_censor_counts_missing_and_nan_samples():
    data = matrix(series([[0, "NaN"], [120, "2"]]))
    base = api.replay(data, cfg(start=0, end=120))
    pending = api.replay(data, cfgd({"start": 0, "end": 120, "for": "60s"}))
    r = summarize(base, [pending])
    s = r["baseline"]["summary"]
    assert [s["firstFiring"], s["firingDurationSeconds"], s["censoredEpisodes"], s["pendingAtEnd"]] == [120, 0, 1, 0]
    c = r["candidates"][0]["report"]["summary"]
    assert [c["firstFiring"], c["censoredEpisodes"], c["pendingAtEnd"]] == [None, 0, 1]
    assert r["candidates"][0]["delta"] == {"firstFiringDelaySeconds": None, "firingDurationSeconds": 0, "firingEpisodes": -1,
                                           "resolutions": 0, "openAtEnd": -1, "pendingAtEnd": 1, "transitionCount": 0,
                                           "censoredEpisodes": -1}
    for run_report in (r["baseline"], r["candidates"][0]["report"]):
        for s in (run_report["summary"], run_report["series"][0]["summary"]):
            assert s["missingSamples"] == 1
            assert s["nanSamples"] == 1
            assert s["heldEvaluations"] == 0
        assert run_report["series"][0]["summary"]["pendingAtEnd"] is (run_report is not r["baseline"])
    inferred = api.replay(data, cfg())
    assert summarize(inferred, [base])["baseline"]["window"]["rangeSource"] == "sample-extrema"
    assert summarize(inferred, [base])["candidates"][0]["report"]["window"]["rangeSource"] == "config"


def test_summary_exact_signed_ms_deltas_and_timer_identities_at_safe_integer_limits():
    ms = 4503599627370480
    config = cfg(evaluationInterval=f"{ms}ms", start=-0.01, end=4503599627370.47)
    data = matrix(*[series([[config["start"], "2"], [config["end"], "2"]], {"instance": i}) for i in ("b", "a")])
    b, c, never = [api.replay(data, x) for x in (config, {**config, "for": "1ms"}, {**config, "threshold": {"op": ">", "value": 3}})]
    r = summarize(b, [c, never])
    assert r["baseline"]["summary"]["firingDurationSeconds"] == 2 * ms / 1000
    assert r["candidates"][0]["delta"]["firingDurationSeconds"] == -2 * ms / 1000
    assert r["candidates"][0]["delta"]["firstFiringDelaySeconds"] == ms / 1000
    assert r["candidates"][1]["delta"]["firstFiringDelaySeconds"] is None
    reverse = summarize(c, [b])["candidates"][0]
    assert reverse["delta"]["firingDurationSeconds"] == 2 * ms / 1000
    assert reverse["delta"]["firstFiringDelaySeconds"] == -ms / 1000
    for d in reverse["seriesDeltas"]:
        assert d["firingDurationSeconds"] == ms / 1000
        assert d["firstFiringDelaySeconds"] == -ms / 1000
    mx, almost = "9007199254740991ms", "9007199254740990ms"

    def empty(f):
        return api.replay(matrix(), cfgd({"evaluationInterval": mx, "for": f, "keep_firing_for": "1ms", "start": 0, "end": 0}))

    exact = summarize(empty(mx), [empty(almost)])
    assert exact["baseline"]["exactConfig"]["evaluationInterval"] == mx
    assert exact["baseline"]["exactConfig"]["for"] == mx
    assert exact["candidates"][0]["report"]["exactConfig"]["for"] == almost
    assert exact["baseline"]["config"]["forSeconds"] == exact["candidates"][0]["report"]["config"]["forSeconds"], \
        "exact identities distinguish colliding seconds"


def empty_steps(steps):
    return api.replay(matrix(), cfg(start=0, end=(steps - 1) * 60))


def make_unsafe():
    unsafe = api.replay(matrix(series([2]), series([2], {"instance": "b"})), cfg())
    unsafe["config"]["evaluationIntervalSeconds"] = 4503599627370.495
    unsafe["checkpoint"]["config"]["evaluationInterval"] = "4503599627370495ms"
    unsafe["window"]["end"] = 9007199254740.99
    unsafe["window"]["evaluations"] = 3
    unsafe["checkpoint"]["evaluatedThrough"] = unsafe["window"]["end"]
    return unsafe


def test_summary_entire_set_count_empty_nonempty_cost_and_safe_exposure_preflight(tmp_path):
    a, cap, over = empty_steps(1), empty_steps(250000), empty_steps(250001)
    for bad_candidates in (None, {}, [], [a] * 256, [None]):
        with pytest.raises(Exception, match="1 to 255|expected an object"):
            summarize(a, bad_candidates)
    many = summarize(a, [a] * 255)
    assert len(many["candidates"]) == 255
    assert many["budget"]["requiredEvaluations"] == 256
    assert [c["candidateIndex"] for c in many["candidates"]] == list(range(255))
    assert summarize(cap, [cap, cap, cap])["budget"]["requiredEvaluations"] == 1000000
    paths = files(tmp_path, [over])
    bad(run(*([paths[0]] * 4), "--detail", "summary"), "combined timeline exceeds 1000000")
    guarded = forbid_warnings(over, "replay preceded preflight")
    with pytest.raises(Exception, match="combined timeline exceeds 1000000"):
        summarize(guarded, [guarded, guarded, guarded])
    nonempty = api.replay(matrix(series([0])), cfg(start=0, end=0))
    nonempty["window"]["end"] = 250000 * 60
    nonempty["window"]["evaluations"] = 250001
    nonempty["checkpoint"]["evaluatedThrough"] = nonempty["window"]["end"]
    with pytest.raises(Exception, match="combined timeline exceeds 1000000"):
        summarize(nonempty, [nonempty, nonempty, nonempty])
    unsafe = make_unsafe()
    bad_paths = files(tmp_path, [unsafe, unsafe], prefix="unsafe-")
    bad(run(*bad_paths, "--detail", "summary"), "aggregate duration exceeds safe integer")
    guarded = forbid_warnings(unsafe, "replay preceded preflight")
    with pytest.raises(Exception, match="aggregate duration exceeds safe integer"):
        summarize(guarded, [guarded])


def _window(r):
    r["window"]["start"] = 1.061
    r["window"]["evaluations"] -= 1


SUMMARY_INVALID = [
    (lambda r: r.update(schemaVersion=2), "schemaVersion 1 replay"),
    (lambda r: r["checkpoint"].update(originStart=0.941), "missing origin history"),
    (_window, "identical evaluation grids"),
    (lambda r: r["series"][0]["labels"].update(instance="other"), "exact label sets"),
    (lambda r: r["series"][0]["timeline"][1].update(value="2.0"), "identical raw samples"),
    (lambda r: r["summary"].update(resolutions=r["summary"]["resolutions"] + 1), "inconsistent"),
    (lambda r: r["series"][0]["summary"].update(firstFiring=0), "inconsistent"),
    (lambda r: r["series"][0]["timeline"][1].update(state="invalid"), "inconsistent"),
    (lambda r: r["series"][0]["episodes"].append({"invalid": True}), "inconsistent"),
    (lambda r: r["series"][0]["transitions"].append({"invalid": True}), "inconsistent"),
    (lambda r: r["series"][0]["timeline"].pop(), "every evaluation"),
    (lambda r: r["checkpoint"]["config"].update({"for": "1ms"}), "inconsistent"),
    (lambda r: r["config"].update(forSeconds=0.001), "inconsistent"),
    (lambda r: r.update(sourceWarnings=[1]), "array of strings"),
]


def test_summary_never_bypasses_complete_origin_capture_derived_validation_at_any_position(tmp_path):
    paths = files(tmp_path)
    for mutate, pattern in SUMMARY_INVALID:
        for position in range(3):
            reports = joined_set(api)
            mutate(reports[position])
            before = copy.deepcopy(reports)
            with pytest.raises(Exception, match=pattern):
                summarize(reports[0], reports[1:])
            assert reports == before
            for p, r in zip(paths, reports):
                write_json(p, r)
            raw = [p.read_bytes() for p in paths]
            bad(run(*paths, "--detail", "summary"), pattern)
            assert [p.read_bytes() for p in paths] == raw


def test_summary_output_is_not_accepted_as_replay_input_even_with_a_forged_command(tmp_path):
    base, *candidates = joined_set(api)
    compact = summarize(base, candidates)
    for n, forged in enumerate((compact, compact["baseline"], compact["candidates"][0]["report"],
                                {**compact["baseline"], "command": "replay"})):
        for detail in ("full", "summary"):
            with pytest.raises(Exception, match="unsupported field|missing field|schemaVersion 1 replay"):
                api.compare_report_set(base, [candidates[0], forged], {"detail": detail})
            with pytest.raises(Exception, match="unsupported field|missing field|schemaVersion 1 replay"):
                api.compare_report_set(forged, candidates, {"detail": detail})
        with pytest.raises(Exception):
            api.compare_reports(base, forged)
        with pytest.raises(Exception):
            api.join([base, forged])
        paths = files(tmp_path, [base, candidates[0], forged], prefix=f"forged{n}-")
        bad(run(*paths, "--detail", "summary"), "unsupported field|missing field|schemaVersion 1 replay")


def test_summary_strict_library_and_cli_detail_options_reject_before_replay_or_file_loading(tmp_path):
    base, *candidates = joined_set(api)
    assert api.compare_report_set(base, candidates, {}) == api.compare_report_set(base, candidates, {"detail": "full"})
    for bad_options in (None, [], "summary", 1, {"detail": None}, {"detail": ""}, {"detail": "SUMMARY"},
                        {"detail": "csv"}, {"detail": "summary", "unknown": True}):
        with pytest.raises(Exception, match="options.*expected an object|options.detail: expected full or summary|options: unsupported field"):
            api.compare_report_set(None, [], bad_options)
    for suffix, pattern in [
        (["--detail"], "--detail: expected full or summary"),
        *[(["--detail", v], "--detail: expected full or summary") for v in ("", "SUMMARY", "compact", "csv")],
        (["--detail", "summary", "--detail", "full"], "duplicate option --detail"),
        (["--detail", "summary", "--format"], "--format: expected json or csv"),
        (["--detail", "summary", "extra.json"], "unsupported argument"),
    ]:
        bad(run("/absent-base", "/absent-candidate", *suffix), pattern)
    bad(run("/absent-base", "--detail", "summary", "/absent-candidate"), "2 to 256")
    for args in ([], ["/absent"], ["/absent"] * 257):
        bad(run(*args, "--detail", "summary"), "2 to 256")
    paths = files(tmp_path)
    original = paths[2].read_bytes()
    for raw, pattern in [("{broken", "invalid JSON"), ('{"x":1,"\\u0078":2}', "duplicate JSON object key"),
                         (b"\xff", "encoded data was not valid"), ("[" * 101 + "0" + "]" * 101, "nesting")]:
        write_raw(paths[2], raw)
        bad(run(*paths, "--detail", "summary"), pattern)
        assert paths[2].read_bytes() == (raw if isinstance(raw, bytes) else raw.encode())
    write_raw(paths[2], original)
    bad(run(*paths[:2], tempfile.gettempdir(), "--detail", "summary"), "regular local file")
    bad(run(*paths[:2], "/absent", "--detail", "summary"), "ENOENT")
    os.truncate(paths[2], 64 * 1024 * 1024 + 1)
    bad(run(*paths, "--detail", "summary"), "exceeds 64 MiB")
