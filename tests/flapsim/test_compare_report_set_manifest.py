# SPDX-License-Identifier: Apache-2.0
import os

import pytest

import otelwrench.flapsim as api
from otelwrench.flapsim import parse_json, to_json

from ._report_set_modes import MODES, empty_steps, make_unsafe
from .support import bad, cli, joined_set, ok, sha, write_json, write_raw

POSIX = pytest.mark.skipif(os.name != "posix", reason="POSIX links and FIFOs")


class Setup:
    def __init__(self, tmp_path, reports=None):
        reports = joined_set(api) if reports is None else reports
        self.dir = tmp_path
        self.saved = tmp_path / "saved"
        self.saved.mkdir()
        self.paths = []
        for i, r in enumerate(reports):
            p = self.saved / f"{i} résumé.json"
            write_json(p, r)
            self.paths.append(p)
        self.manifest = self.saved / "set.json"
        self.out = tmp_path / "result"
        self.data = {"schemaVersion": 1, "baseline": "0 résumé.json", "candidates": ["1 résumé.json", "2 résumé.json"]}
        write_raw(self.manifest, to_json(self.data, 2) + "\n")
        self.before = [p.read_bytes() for p in self.paths]

    def no_temps(self):
        for d in (self.dir, self.saved):
            assert [p for p in os.listdir(d) if p.startswith(".alert-flapsim-")] == []

    def unchanged(self):
        assert [p.read_bytes() for p in self.paths] == self.before
        self.no_temps()


def run(s, args):
    return cli("compare-report-set", *args, cwd=s.dir)


@pytest.mark.parametrize("index", range(len(MODES)))
def test_manifest_equivalent_known_byte_export_from_another_cwd_stdout_and_atomic_output(tmp_path, index):
    mode = MODES[index]
    s = Setup(tmp_path, mode["reports"](api))
    manifest_bytes = s.manifest.read_bytes()
    expected = ok(run(s, [*s.paths, *mode["flags"]]))
    mode["check"](expected)
    if mode["hash"]:
        assert sha(expected) == mode["hash"]
    assert ok(run(s, ["--manifest", os.path.join("saved", "set.json"), *mode["flags"]])) == expected
    for existing in (False, True):
        if s.out.exists():
            s.out.unlink()
        if existing:
            write_raw(s.out, "old bytes")
        assert ok(run(s, [*mode["flags"], "--output", "result", "--manifest", s.manifest])) == ""
        assert s.out.read_bytes().decode("utf-8") == expected
    assert s.manifest.read_bytes() == manifest_bytes
    s.unchanged()


@POSIX
def test_manifest_mixed_absolute_relative_paths_preserve_reordered_duplicates_and_literal_names(tmp_path):
    s = Setup(tmp_path)
    literal = "-$HOME ~ [x] résumé.json"
    os.symlink(s.paths[1], s.saved / literal)
    data = {"schemaVersion": 1, "baseline": str(s.paths[0]), "candidates": ["2 résumé.json", literal, str(s.paths[2]), "0 résumé.json"]}
    write_json(s.manifest, data)
    before = s.manifest.read_bytes()
    paths = [s.paths[0], s.paths[2], s.paths[1], s.paths[2], s.paths[0]]
    for mode in MODES:
        result = ok(run(s, ["--manifest", s.manifest, *mode["flags"]]))
        assert result == ok(run(s, [*paths, *mode["flags"]]))
        if "csv" not in mode["flags"]:
            parsed = parse_json(result)
            assert [c["candidateIndex"] for c in parsed["candidates"]] == [0, 1, 2, 3]
            assert [c["report"]["config"]["threshold"]["value"] for c in parsed["candidates"]] == [3, 2, 3, 1]
            assert parsed["budget"]["reports"] == 5
    assert s.manifest.read_bytes() == before
    s.unchanged()


@POSIX
def test_manifest_and_input_symlinks_resolve_to_regular_files_at_canonical_manifest_directory(tmp_path):
    s = Setup(tmp_path)
    manifest_link, parent_link = s.dir / "elsewhere.json", s.dir / "parent"
    os.symlink(s.manifest, manifest_link)
    os.symlink(s.saved, parent_link)
    report_link = s.saved / "report-link"
    os.symlink(s.paths[0], report_link)
    s.data["baseline"] = "report-link"
    write_json(s.manifest, s.data)
    expected = ok(run(s, s.paths))
    for path in (manifest_link, parent_link / "set.json"):
        assert ok(run(s, ["--manifest", path])) == expected
        assert ok(run(s, ["--manifest", path, "--output", s.out])) == ""
        assert s.out.read_bytes().decode("utf-8") == expected
    # Hard links use their own named directory; place the manifest beside copies.
    hard_dir = s.dir / "hard"
    hard_dir.mkdir()
    os.link(s.manifest, hard_dir / "set.json")
    for name in ("report-link", "1 résumé.json", "2 résumé.json"):
        os.link(s.saved / name, hard_dir / name)
    assert ok(run(s, ["--manifest", hard_dir / "set.json"])) == expected
    s.unchanged()


def test_manifest_strict_schema_bad_arrays_paths_versions_json_and_utf8_reject_without_output(tmp_path):
    s = Setup(tmp_path)
    values = [None, [], "manifest", 1, {}, {**s.data, "extra": True}, {**s.data, "schemaVersion": "1"},
              {**s.data, "schemaVersion": 2}, {"baseline": s.data["baseline"], "candidates": s.data["candidates"]},
              {**s.data, "baseline": ""}, {**s.data, "baseline": None}, {**s.data, "baseline": ["x"]},
              {**s.data, "baseline": "\0"}, {**s.data, "baseline": "\ud800"},
              {**s.data, "candidates": None}, {**s.data, "candidates": {}}, {**s.data, "candidates": "x"},
              {**s.data, "candidates": []}, {**s.data, "candidates": ["missing"] * 256},
              *[{**s.data, "candidates": ["1 résumé.json", p]} for p in ("", None, {}, 3, [], "\0", "\udfff")]]
    cases = [(to_json(v), "manifest") for v in values] + [
        ('{"schemaVersion":1,"baseline":"x","\\u0062aseline":"y","candidates":["z"]}', "duplicate JSON"),
        ('{"schemaVersion":1,"baseline":"x","candidates":["z"],"candidates":["y"]}', "duplicate JSON"),
        ("{broken", "invalid JSON"), ('{"candidates":[,"x"]}', "invalid JSON"),
        (b"\x7b\xff", "encoded data"), ("[" * 101 + "0" + "]" * 101, "nesting"),
    ]
    for data, pattern in cases:
        write_raw(s.manifest, data)
        for existing in (False, True):
            if s.out.exists():
                s.out.unlink()
            if existing:
                write_raw(s.out, "keep")
            bad(run(s, ["--manifest", s.manifest, "--detail", "summary", "--format", "csv", "--output", s.out]), pattern)
            assert s.out.exists() == existing
            if existing:
                assert s.out.read_text() == "keep"
        assert s.manifest.read_bytes() == (data if isinstance(data, bytes) else data.encode())
        s.unchanged()


def test_manifest_option_is_exclusive_strict_and_scoped_to_saved_sets(tmp_path):
    s = Setup(tmp_path)
    for args in (["--manifest"], ["--manifest", ""], ["--manifest", "-"], ["--manifest", "--detail", "summary"],
                 ["--manifest", s.manifest, "--manifest", s.manifest], [*s.paths, "--manifest", s.manifest],
                 ["--manifest", s.manifest, *s.paths], ["--manifest", s.manifest, "--detail", "bad"],
                 ["--manifest", s.manifest, "--format", "tsv"], ["--manifest", s.manifest, "--unknown", "x"],
                 ["--manifest", s.manifest, "--output"], ["--manifest", s.manifest, "--format", "json", "--format", "csv"]):
        bad(run(s, args), "manifest|unsupported|detail|format|output")
        s.unchanged()
    for command in ("compare-reports", "join", "replay", "checkpoint", "compare", "grid"):
        bad(cli(command, *s.paths[:2], "--manifest", s.manifest), "unsupported")


@POSIX
def test_manifest_output_aliases_to_manifest_or_any_report_reject_canonically_and_by_inode(tmp_path):
    s = Setup(tmp_path)
    manifest_before = s.manifest.read_bytes()
    parent = s.dir / "parent"
    os.symlink(s.saved, parent)
    manifest_hard = s.dir / "manifest-hard"
    os.link(s.manifest, manifest_hard)
    report_hard = s.dir / "report-hard"
    os.link(s.paths[2], report_hard)
    manifest_link = s.dir / "manifest-link"
    os.symlink(s.manifest, manifest_link)
    report_link = s.dir / "report-link"
    os.symlink(s.paths[0], report_link)
    for out in (s.manifest, manifest_hard, parent / "set.json", manifest_link, *s.paths, report_hard, report_link):
        bad(run(s, ["--manifest", manifest_link, "--output", out]), "input|symlink")
        assert s.manifest.read_bytes() == manifest_before
        s.unchanged()
    # A manifest sharing a report inode cannot form valid JSON under both schemas;
    # this is rejected as input, before a destination is considered.
    bad(run(s, ["--manifest", s.paths[0], "--output", s.out]), "manifest")
    assert not s.out.exists()


def test_manifest_late_invalid_reports_preserve_absent_and_existing_targets_in_all_modes(tmp_path):
    s = Setup(tmp_path)
    late = s.saved / "late.json"
    s.data["candidates"].append("late.json")
    write_json(s.manifest, s.data)
    manifest_before = s.manifest.read_bytes()
    changed = parse_json(s.before[2].decode())
    changed["summary"]["resolutions"] += 1
    wrong_capture = parse_json(s.before[2].decode())
    wrong_capture["series"][0]["timeline"][0]["value"] = "0.0"
    compact = api.compare_report_set(parse_json(s.before[0].decode()), [parse_json(s.before[1].decode())],
                                     {"detail": "summary"})["baseline"]
    cases = [("{broken", "invalid JSON"), ('{"x":1,"x":2}', "duplicate JSON"), (b"\xff", "encoded data"),
             (to_json(changed), "inconsistent"), (to_json(wrong_capture), "identical raw samples"), (to_json(compact), "replay|field")]
    for mode in MODES:
        for data, pattern in cases:
            write_raw(late, data)
            for existing in (False, True):
                if s.out.exists():
                    s.out.unlink()
                if existing:
                    write_raw(s.out, "keep")
                bad(run(s, ["--manifest", s.manifest, *mode["flags"], "--output", s.out]), pattern)
                assert s.out.exists() == existing
                if existing:
                    assert s.out.read_text() == "keep"
            assert late.read_bytes() == (data if isinstance(data, bytes) else data.encode())
            s.unchanged()
            assert s.manifest.read_bytes() == manifest_before


@POSIX
def test_manifest_and_late_report_files_retain_regular_file_size_and_missing_path_boundaries(tmp_path):
    s = Setup(tmp_path)
    broken = s.dir / "broken"
    os.symlink("missing", broken)
    write_raw(s.out, "keep")
    for path, pattern in ((s.saved, "regular local file"), (s.dir / "missing", "ENOENT"), (broken, "ENOENT")):
        bad(run(s, ["--manifest", path, "--output", s.out]), pattern)
        s.data["candidates"][1] = str(path)
        write_json(s.manifest, s.data)
        bad(run(s, ["--manifest", s.manifest, "--output", s.out]), pattern)
        assert s.out.read_text() == "keep"
        s.unchanged()
    huge = s.saved / "huge"
    write_raw(huge, "")
    os.truncate(huge, 64 * 1024 * 1024 + 1)
    bad(run(s, ["--manifest", huge, "--output", s.out]), "64 MiB")
    s.data["candidates"][1] = "huge"
    write_json(s.manifest, s.data)
    bad(run(s, ["--manifest", s.manifest, "--output", s.out]), "64 MiB")
    assert s.out.read_text() == "keep"
    s.unchanged()


def test_manifest_accepts_256_reports_and_retains_whole_set_cost_and_safe_exposure_preflight(tmp_path):
    s = Setup(tmp_path)
    write_json(s.paths[0], empty_steps(1))
    maximum = {"schemaVersion": 1, "baseline": str(s.paths[0]), "candidates": [str(s.paths[0])] * 255}
    write_json(s.manifest, maximum)
    result = ok(run(s, ["--manifest", s.manifest, "--detail", "summary"]))
    assert result == ok(run(s, [*([s.paths[0]] * 256), "--detail", "summary"]))
    assert len(parse_json(result)["candidates"]) == 255
    # Identical files still count separately; baseline is charged once.
    maximum["candidates"] = [str(s.paths[0])] * 3
    write_json(s.manifest, maximum)
    write_json(s.paths[0], empty_steps(250000))
    assert parse_json(ok(run(s, ["--manifest", s.manifest, "--detail", "summary"])))["budget"]["requiredEvaluations"] == 1000000
    for report, pattern in ((empty_steps(250001), "combined timeline exceeds"), (make_unsafe(), "aggregate duration exceeds")):
        write_json(s.paths[0], report)
        before = s.paths[0].read_bytes()
        for mode in MODES:
            write_raw(s.out, "keep")
            bad(run(s, ["--manifest", s.manifest, *mode["flags"], "--output", s.out]), pattern)
            assert s.out.read_text() == "keep"
            s.no_temps()
        assert s.paths[0].read_bytes() == before


def test_manifest_minimum_one_candidate_lexical_parent_paths_and_explicit_full_mode(tmp_path):
    s = Setup(tmp_path)
    s.data["baseline"] = "../saved/0 résumé.json"
    s.data["candidates"] = ["./1 résumé.json"]
    write_json(s.manifest, s.data)
    before = s.manifest.read_bytes()
    for mode in MODES:
        flags = mode["flags"] if "summary" in mode["flags"] else [*mode["flags"], "--detail", "full"]
        assert ok(run(s, ["--manifest", s.manifest, *flags])) == ok(run(s, [*s.paths[:2], *flags]))
    assert s.manifest.read_bytes() == before
    s.unchanged()


@POSIX
def test_manifest_fifos_reject_before_reading_and_late_invalid_stdout_is_wholly_empty(tmp_path):
    s = Setup(tmp_path)
    fifo = s.saved / "fifo"
    os.mkfifo(fifo)
    bad(run(s, ["--manifest", fifo]), "regular local file")
    s.data["candidates"].append("fifo")
    write_json(s.manifest, s.data)
    for mode in MODES:
        bad(run(s, ["--manifest", s.manifest, *mode["flags"]]), "regular local file")
    s.data["candidates"][2] = "bad.json"
    write_json(s.manifest, s.data)
    broken = parse_json(s.before[2].decode())
    broken["summary"]["resolutions"] += 1
    write_json(s.saved / "bad.json", broken)
    for mode in MODES:
        bad(run(s, ["--manifest", s.manifest, *mode["flags"]]), "inconsistent")
    s.unchanged()
