# SPDX-License-Identifier: Apache-2.0
import copy
import os

import pytest

import otelwrench.flapsim as api
from otelwrench.flapsim import parse_json, to_json

from ._report_set_modes import empty_steps, make_unsafe
from .support import (
    assert_set_summary, bad, cli, csv_rows, fixture_text, joined_set, ok, sha, summary_csv_set, write_json, write_raw,
)

POSIX = pytest.mark.skipif(os.name != "posix", reason="POSIX links and FIFOs")
NAMES = parse_json(fixture_text("report-set-names.json"))
MODES = [[], ["--format", "csv"]]


class Setup:
    def __init__(self, tmp_path, reports=None):
        reports = joined_set(api) if reports is None else reports
        self.dir = tmp_path
        self.saved = tmp_path / "saved"
        self.saved.mkdir()
        self.paths = []
        for i, r in enumerate(reports):
            p = self.saved / f"{i}.json"
            write_json(p, r)
            self.paths.append(p)
        self.metadata = tmp_path / "names.json"
        self.manifest = self.saved / "set.json"
        self.out = tmp_path / "result"
        write_json(self.metadata, NAMES)
        write_json(self.manifest, {"schemaVersion": 1, "baseline": "0.json", "candidates": ["1.json", "2.json"]})
        self.before = [p.read_bytes() for p in (*self.paths, self.metadata, self.manifest)]

    def no_temps(self):
        for d in (self.dir, self.saved):
            assert [p for p in os.listdir(d) if p.startswith(".alert-flapsim-")] == []

    def preserved(self):
        assert [p.read_bytes() for p in (*self.paths, self.metadata, self.manifest)] == self.before
        self.no_temps()


def run(s, args):
    return cli("compare-report-set", *args, cwd=s.dir)


def named(s, mode=()):
    return [*s.paths, "--detail", "summary", "--names", s.metadata, *mode]


def atomic_bad(s, args, pattern):
    bad(run(s, args), pattern)
    for existing in (False, True):
        if s.out.exists():
            s.out.unlink()
        if existing:
            write_raw(s.out, "keep")
        bad(run(s, [*args, "--output", s.out]), pattern)
        assert s.out.exists() == existing
        if existing:
            assert s.out.read_text() == "keep"
        s.no_temps()


@pytest.mark.parametrize("index", [0, 1], ids=["JSON", "CSV v2"])
def test_names_independent_multi_candidate_byte_oracle_identities_and_atomic_destinations(tmp_path, index):
    mode = MODES[index]
    s = Setup(tmp_path, (summary_csv_set if index else joined_set)(api))
    expected = fixture_text("report-set-summary-v2.csv" if index else "report-set-named-summary.json")
    assert sha(expected) == ("5bf7f08155e7617f8b8f998f9926bdd46b0974765c00466453e84ee1fd47c972" if index
                             else "c8f9dff296da33b105151b3c856e97324104032129c60922384aeda3d4e005a3")
    actual = ok(run(s, named(s, mode)))
    assert actual == expected
    assert ok(run(s, ["--names", "names.json", *mode, "--manifest", os.path.join("saved", "set.json"), "--detail", "summary"])) == expected
    for existing in (False, True):
        if s.out.exists():
            s.out.unlink()
        if existing:
            write_raw(s.out, "old")
        assert ok(run(s, [*named(s, mode), "--output", s.out])) == ""
        assert s.out.read_bytes().decode("utf-8") == expected
    if not index:
        r = parse_json(actual)
        assert r["baseline"]["displayName"] == NAMES["baseline"]
        assert [[c["candidateIndex"], c["displayName"]] for c in r["candidates"]] == [[i, n] for i, n in enumerate(NAMES["candidates"])]
        del r["baseline"]["displayName"]
        for c in r["candidates"]:
            del c["displayName"]
        assert_set_summary(r)
        assert sha(to_json(r, 2) + "\n") == "2b8cdc2ce3e64ff47276a7cb3274b970cbd6a03d0f8a9d0210b5caa04770510c"
    else:
        rows, old = csv_rows(actual), csv_rows(fixture_text("report-set-summary-v1.csv"))
        assert len(rows) == 9
        assert len(rows[0]) == 51
        all_names = [NAMES["baseline"], *NAMES["candidates"]]
        assert [[r["role"], r["candidateIndex"], r["displayName"]] for r in rows] == [
            ["candidate" if i else "baseline", str(i - 1) if i else "null", n] for i, n in enumerate(all_names) for _ in range(3)]
        for i, r in enumerate(rows):
            assert r["schemaVersion"] == "2"
            assert r["csvSchema"] == "alert-flapsim-report-set-summary-v2"
            del r["displayName"]
            r["schemaVersion"] = "1"
            r["csvSchema"] = "alert-flapsim-report-set-summary-v1"
            assert r == old[i]
    s.preserved()


def test_names_preserve_all_unnamed_export_bytes_with_no_new_library_options(tmp_path):
    (tmp_path / "a").mkdir()
    s = Setup(tmp_path / "a")
    checks = [([], "e15ba058c4513dc349cfe4c26c70f09f7fca3007446b2165d9ba9efddcad5487"),
              (["--detail", "summary"], "2b8cdc2ce3e64ff47276a7cb3274b970cbd6a03d0f8a9d0210b5caa04770510c"),
              (["--format", "csv"], "082ab68ccb5e150f3235693cd196f24e6fac46ffcc4f796a4b943c3ebce59e3a")]
    for flags, digest in checks:
        for source in (s.paths, ["--manifest", s.manifest]):
            assert sha(ok(run(s, [*source, *flags]))) == digest
    (tmp_path / "b").mkdir()
    csv_setup = Setup(tmp_path / "b", summary_csv_set(api))
    assert ok(run(csv_setup, [*csv_setup.paths, "--detail", "summary", "--format", "csv"])) == fixture_text("report-set-summary-v1.csv")
    reports = joined_set(api)
    with pytest.raises(Exception, match="unsupported"):
        api.compare_report_set(reports[0], reports[1:], {"detail": "summary", "names": NAMES})
    s.preserved()
    csv_setup.preserved()


def test_names_strict_schema_unicode_and_byte_count_limits_reject_atomically(tmp_path):
    s = Setup(tmp_path)
    values = [None, [], {}, 1, "names", {**NAMES, "extra": 0}, {**NAMES, "schemaVersion": 2}, {**NAMES, "schemaVersion": "1"},
              {"baseline": NAMES["baseline"], "candidates": NAMES["candidates"]}, {**NAMES, "baseline": None},
              {**NAMES, "candidates": None}, {**NAMES, "candidates": {}}, {**NAMES, "candidates": "x"},
              {**NAMES, "candidates": []}, {**NAMES, "candidates": ["one"]}, {**NAMES, "candidates": ["one", "two", "three"]},
              {**NAMES, "candidates": ["x"] * 256}]
    for n in ("", None, 3, {}, [], "\0", "a\0b", "\ud800", "\udfff", "a" * 257, "é" * 129, "\U0001F642" * 65):
        values.extend([{**NAMES, "baseline": n}, {**NAMES, "candidates": ["valid", n]}])
    cases = [(to_json(v), "names") for v in values] + [
        ('{"schemaVersion":1,"baseline":"a","\\u0062aseline":"b","candidates":["c","d"]}', "duplicate JSON"),
        ('{"schemaVersion":1,"baseline":"a","candidates":["c","d"],"candidates":["e","f"]}', "duplicate JSON"),
        ("{broken", "invalid JSON"), ("[" * 101 + "0" + "]" * 101, "nesting"), (b"\xff", "encoded data"),
    ]
    for data, pattern in cases:
        write_raw(s.metadata, data)
        atomic_bad(s, named(s, ["--format", "csv"]), pattern)
        assert s.metadata.read_bytes() == (data if isinstance(data, bytes) else data.encode())
        assert [p.read_bytes() for p in s.paths] == s.before[:3]
        assert s.manifest.read_bytes() == s.before[-1]


def test_names_accept_exact_utf8_bounds_duplicates_whitespace_and_normalization_distinct_strings(tmp_path):
    s = Setup(tmp_path)
    ns = {"schemaVersion": 1, "baseline": "\U0001F642" * 64, "candidates": ["é" * 128, "a" * 256]}
    paths = [s.paths[0], s.paths[2], s.paths[1]]
    for mapping in (ns, {**ns, "baseline": " ", "candidates": ["same", "same"]},
                    {**ns, "baseline": "=SUM(A1)", "candidates": ["é", "é"]}):
        write_json(s.metadata, mapping)
        r = parse_json(ok(run(s, [*paths, "--names", s.metadata, "--detail", "summary"])))
        assert r["baseline"]["displayName"] == mapping["baseline"]
        assert [[c["candidateIndex"], c["displayName"], c["report"]["config"]["threshold"]["value"]] for c in r["candidates"]] == \
            [[0, mapping["candidates"][0], 3], [1, mapping["candidates"][1], 2]]
        rows = csv_rows(ok(run(s, [*paths, "--names", s.metadata, "--detail", "summary", "--format", "csv"])))
        assert [r["displayName"] for r in rows if r["scope"] == "aggregate"] == [mapping["baseline"], *mapping["candidates"]]
    s.no_temps()


def test_names_syntax_requires_summary_once_valid_path_and_saved_set_command_only(tmp_path):
    s = Setup(tmp_path)
    for tail in (["--names"], ["--names", ""], ["--names", "-"], ["--names", "--detail", "summary"],
                 ["--names", s.metadata], ["--names", s.metadata, "--detail", "full"],
                 ["--names", s.metadata, "--detail", "summary", "--names", s.metadata]):
        atomic_bad(s, [*s.paths, *tail], "names")
    for command in ("compare-reports", "join", "replay", "checkpoint", "compare", "grid"):
        bad(cli(command, *s.paths[:2], "--names", s.metadata), "unsupported")
    s.preserved()


@POSIX
def test_names_metadata_aliases_reject_canonically_by_inode_and_through_symlinks(tmp_path):
    s = Setup(tmp_path)
    hard, sym, parent = s.dir / "hard", s.dir / "sym", s.dir / "parent"
    os.link(s.metadata, hard)
    os.symlink(s.metadata, sym)
    os.symlink(s.dir, parent)
    for meta in (s.metadata, hard, sym, parent / "names.json"):
        for out in (s.metadata, hard, sym, parent / "names.json", s.manifest, *s.paths):
            bad(run(s, ["--manifest", s.manifest, "--detail", "summary", "--names", meta, "--output", out]), "input|symlink")
            s.preserved()
    assert ok(run(s, [*s.paths, "--names", sym, "--detail", "summary"])) == fixture_text("report-set-named-summary.json")


@POSIX
def test_names_files_retain_regular_local_file_utf8_size_and_missing_file_boundaries(tmp_path):
    s = Setup(tmp_path)
    fifo, huge, dangling = s.dir / "fifo", s.dir / "huge", s.dir / "dangling"
    os.mkfifo(fifo)
    write_raw(huge, "")
    os.truncate(huge, 64 * 1024 * 1024 + 1)
    os.symlink("absent", dangling)
    for p, pattern in ((s.dir, "regular local file"), (fifo, "regular local file"), (huge, "64 MiB"),
                       (dangling, "ENOENT"), (s.dir / "absent", "ENOENT")):
        atomic_bad(s, [*s.paths, "--detail", "summary", "--names", p], pattern)
        s.preserved()


def test_names_never_bypass_late_capture_derived_origin_compact_validation_or_mutate_inputs(tmp_path):
    s = Setup(tmp_path)
    original = parse_json(s.before[2].decode())
    inconsistent = copy.deepcopy(original)
    inconsistent["summary"]["resolutions"] += 1
    capture = copy.deepcopy(original)
    capture["series"][0]["timeline"][0]["value"] = "0.0"
    compact = api.compare_report_set(original, [original], {"detail": "summary"})["baseline"]
    cases = [(to_json(inconsistent), "inconsistent"), (to_json(capture), "identical raw samples"),
             (to_json(compact), "replay|field"), ("{broken", "invalid JSON")]
    for raw, pattern in cases:
        for mode in MODES:
            write_raw(s.paths[2], raw)
            atomic_bad(s, named(s, mode), pattern)
            assert s.paths[2].read_bytes() == raw.encode("utf-8")
            assert s.metadata.read_bytes() == s.before[3]
            assert s.manifest.read_bytes() == s.before[4]


def test_names_retain_min_max_counts_charged_duplicates_whole_set_cost_and_safe_exposure(tmp_path):
    s = Setup(tmp_path)
    write_json(s.paths[0], empty_steps(1))
    for count in (1, 255):
        write_json(s.metadata, {"schemaVersion": 1, "baseline": "same", "candidates": ["same"] * count})
        write_json(s.manifest, {"schemaVersion": 1, "baseline": str(s.paths[0]), "candidates": [str(s.paths[0])] * count})
        args = ["--manifest", s.manifest, "--detail", "summary", "--names", s.metadata]
        r = parse_json(ok(run(s, args)))
        assert len(r["candidates"]) == count
        assert r["budget"]["reports"] == count + 1
        assert [c["candidateIndex"] for c in r["candidates"]] == list(range(count))
        assert len(csv_rows(ok(run(s, [*args, "--format", "csv"])))) == count + 1
    write_json(s.metadata, {**NAMES, "candidates": ["a", "b", "c"]})
    write_json(s.manifest, {"schemaVersion": 1, "baseline": str(s.paths[0]), "candidates": [str(s.paths[0])] * 3})
    args = ["--manifest", s.manifest, "--detail", "summary", "--names", s.metadata]
    write_json(s.paths[0], empty_steps(250000))
    assert parse_json(ok(run(s, args)))["budget"]["requiredEvaluations"] == 1000000
    for report, pattern in ((empty_steps(250001), "combined timeline exceeds"), (make_unsafe(), "aggregate duration exceeds")):
        write_json(s.paths[0], report)
        for mode in MODES:
            atomic_bad(s, [*args, *mode], pattern)
    write_json(s.manifest, {"schemaVersion": 1, "baseline": str(s.paths[0]), "candidates": [str(s.paths[0])] * 256})
    atomic_bad(s, args, "1 to 255|2 to 256")
