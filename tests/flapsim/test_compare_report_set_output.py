# SPDX-License-Identifier: Apache-2.0
import os
import stat

import pytest

import otelwrench.flapsim as api
from otelwrench.flapsim import local_output, parse_json
from otelwrench.flapsim._nodefs import NodeFsError

from ._report_set_modes import MODES, empty_steps, make_unsafe
from .support import bad, cli, joined_set, ok, sha, write_json, write_raw

POSIX = pytest.mark.skipif(os.name != "posix", reason="POSIX links and FIFOs")


class Setup:
    def __init__(self, tmp_path, reports=None):
        reports = joined_set(api) if reports is None else reports
        self.dir = tmp_path
        self.paths = []
        for i, r in enumerate(reports):
            p = tmp_path / f"{i}.json"
            write_json(p, r)
            self.paths.append(p)
        self.out = tmp_path / "result with spaces"
        self.before = [p.read_bytes() for p in self.paths]

    def unchanged(self):
        assert [p.read_bytes() for p in self.paths] == self.before

    def no_temps(self):
        assert [p for p in os.listdir(self.dir) if p.startswith(".alert-flapsim-")] == []


def run(paths, flags=(), cwd=None):
    return cli("compare-report-set", *paths, *flags, cwd=cwd)


def umask():
    mask = os.umask(0)
    os.umask(mask)
    return mask


@POSIX
@pytest.mark.parametrize("index", range(len(MODES)))
def test_atomic_output_known_bytes_new_file_and_overwrite_match_stdout_and_preserve_inputs(tmp_path, index):
    mode = MODES[index]
    s = Setup(tmp_path, mode["reports"](api))
    stdout = ok(run(s.paths, mode["flags"]))
    mode["check"](stdout)
    if mode["hash"]:
        assert sha(stdout) == mode["hash"]
    assert ok(run(s.paths, ["--output", s.out, *mode["flags"]])) == ""
    assert s.out.read_bytes().decode("utf-8") == stdout
    assert stat.S_IMODE(os.stat(s.out).st_mode) == 0o600 & ~umask()
    write_raw(s.out, "PREVIOUS\x00BYTES")
    inode = os.stat(s.out).st_ino
    assert ok(run(s.paths, [*mode["flags"], "--output", s.out])) == ""
    assert s.out.read_bytes().decode("utf-8") == stdout
    assert os.stat(s.out).st_ino != inode
    s.unchanged()
    s.no_temps()


def test_output_option_grammar_is_strict_and_scoped_to_report_set(tmp_path):
    s = Setup(tmp_path)
    for suffix in (["--output"], ["--output", ""], ["--output", "-"], ["--output", "--format", "json"],
                   ["--output", s.out, "--output", s.out], ["--output", f"{s.out}{os.sep}"],
                   ["--output", s.out, "--detail", "SUMMARY"], ["--output", s.out, "--format", "tsv"],
                   ["--output", s.out, "--format", "csv", "--format", "json"], ["--output", s.out, "extra.json"]):
        bad(run(s.paths, suffix), "output|detail|format|unsupported")
        assert not s.out.exists()
        s.no_temps()
    bad(run([s.paths[0]], ["--output", s.out, s.paths[1]]), "2 to 256")
    bad(run([s.paths[0]] * 257, ["--output", s.out]), "2 to 256")
    for command in ("compare-reports", "join", "replay", "checkpoint", "compare", "grid"):
        bad(cli(command, *s.paths[:2], "--output", s.out), "unsupported")
    # A leading hyphen can be made a literal local path using ./; '-' has no stdout meaning.
    assert ok(run(s.paths, ["--output", "./-"], cwd=s.dir)) == ""
    assert (s.dir / "-").read_bytes().decode("utf-8") == ok(run(s.paths))
    s.unchanged()


@POSIX
def test_output_rejects_input_aliases_symlinks_and_unsupported_targets_without_changing_any_file(tmp_path):
    s = Setup(tmp_path)
    parent = s.dir / "parent-link"
    os.symlink(s.dir, parent)
    input_link = s.dir / "input-link"
    os.symlink(s.paths[0], input_link)
    hard = s.dir / "hard"
    os.link(s.paths[2], hard)
    sym = s.dir / "target-link"
    os.symlink(s.paths[1], sym)
    dangling = s.dir / "dangling"
    os.symlink("absent", dangling)
    directory = s.dir / "directory"
    directory.mkdir()
    fifo = s.dir / "fifo"
    os.mkfifo(fifo)
    for path in (s.paths[0], f"{s.dir}{os.sep}.{os.sep}1.json", parent / "2.json", hard, sym, dangling, directory, fifo):
        bad(run(s.paths, ["--output", path]), "input|regular|symlink")
        s.unchanged()
        s.no_temps()
    bad(run([input_link, *s.paths[1:]], ["--output", s.paths[0]]), "input")
    assert not (s.dir / "absent").exists()
    # Non-input hard links and symlinked parents are allowed; only the named entry is replaced.
    other = s.dir / "other"
    write_raw(other, "old")
    os.link(other, s.out)
    assert ok(run(s.paths, ["--output", parent / "result with spaces"])) == ""
    assert other.read_text() == "old"
    assert s.out.read_bytes().decode("utf-8") == ok(run(s.paths))
    s.unchanged()
    s.no_temps()


def test_late_invalid_files_reports_leave_existing_and_absent_targets_untouched_in_every_mode(tmp_path):
    s = Setup(tmp_path)
    bad_path = s.dir / "bad.json"
    changed = parse_json(s.before[2].decode())
    changed["summary"]["resolutions"] += 1
    compact = api.compare_report_set(parse_json(s.before[0].decode()), [parse_json(s.before[1].decode())],
                                     {"detail": "summary"})["baseline"]
    from otelwrench.flapsim import to_json
    cases = [("{broken", "invalid JSON"), ('{"a":1,"\\u0061":2}', "duplicate JSON"), (b"\xff", "encoded data"),
             ("[" * 101 + "0" + "]" * 101, "nesting"), (to_json(changed), "inconsistent"), (to_json(compact), "replay|field")]
    for mode in MODES:
        for data, pattern in cases:
            write_raw(bad_path, data)
            for existing in (False, True):
                if s.out.exists():
                    s.out.unlink()
                if existing:
                    write_raw(s.out, "keep me")
                bad(run([*s.paths, bad_path], [*mode["flags"], "--output", s.out]), pattern)
                assert s.out.exists() == existing
                if existing:
                    assert s.out.read_text() == "keep me"
                assert bad_path.read_bytes() == (data if isinstance(data, bytes) else data.encode())
                s.unchanged()
                s.no_temps()
    for late in (s.dir, s.dir / "missing"):
        bad(run([*s.paths, late], ["--output", s.out]), "regular|ENOENT")
        assert s.out.read_text() == "keep me"
    write_raw(bad_path, "")
    os.truncate(bad_path, 64 * 1024 * 1024 + 1)
    bad(run([*s.paths, bad_path], ["--output", s.out]), "64 MiB")
    assert s.out.read_text() == "keep me"


def test_output_retains_entire_set_count_combined_cost_and_safe_aggregate_exposure_preflight(tmp_path):
    s = Setup(tmp_path)
    for report, n, pattern in ((empty_steps(250001), 4, "combined timeline exceeds"), (make_unsafe(), 2, "aggregate duration exceeds")):
        write_json(s.paths[0], report)
        for mode in MODES:
            write_raw(s.out, "keep me")
            bad(run([s.paths[0]] * n, [*mode["flags"], "--output", s.out]), pattern)
            assert s.out.read_text() == "keep me"
            s.no_temps()
    write_json(s.paths[0], empty_steps(1))
    expected = ok(run([s.paths[0]] * 256, ["--detail", "summary"]))
    assert ok(run([s.paths[0]] * 256, ["--detail", "summary", "--output", s.out])) == ""
    assert s.out.read_bytes().decode("utf-8") == expected


def _install_faults(monkeypatch, stage, target, exists, expected=None):
    """Inject one failure at the named OS boundary of write_local_output."""
    real = dict(vars(local_output.fs))
    state = {"fd": None, "tmp": None, "failed": False}

    def fail():
        state["failed"] = True
        raise NodeFsError(f"injected {stage}", "EEXIST" if stage == "rename" else "EIO")

    def fake_open(path, flags, mode=0o777):
        assert os.path.dirname(path) == os.path.dirname(os.path.realpath(target))
        assert mode == 0o600
        if stage == "open":
            fail()
        state["fd"] = real["open"](path, flags, mode)
        state["tmp"] = path
        return state["fd"]

    def fake_write(fd, data):
        if fd == state["fd"]:
            current = open(target, encoding="utf-8").read() if os.path.exists(target) else None
            assert current == ("keep me" if exists else None)
            if stage == "write":
                real["write"](fd, b"PARTIAL")
                fail()
        return real["write"](fd, data)

    def fake_close(fd):
        if fd == state["fd"] and stage == "close" and not state["failed"]:
            fail()
        result = real["close"](fd)
        if fd == state["fd"] and stage == "changed":
            with open(target, "w", encoding="utf-8") as handle:
                handle.write("external writer")
        return result

    def fake_rename(src, dst):
        assert src == state["tmp"]
        assert dst == os.path.join(os.path.realpath(os.path.dirname(target)), os.path.basename(target))
        if expected is not None:
            assert open(src, encoding="utf-8").read() == expected
        if stage == "rename":
            fail()
        return real["rename"](src, dst)

    monkeypatch.setattr(local_output.fs, "open", fake_open)
    monkeypatch.setattr(local_output.fs, "write", fake_write)
    monkeypatch.setattr(local_output.fs, "close", fake_close)
    monkeypatch.setattr(local_output.fs, "rename", fake_rename)


def test_filesystem_failures_preserve_target_and_clean_owned_temporary_files(tmp_path, monkeypatch):
    s = Setup(tmp_path)
    for mode in MODES:
        for stage in ("open", "write", "close", "rename"):
            for existing in (False, True):
                if s.out.exists():
                    s.out.unlink()
                if existing:
                    write_raw(s.out, "keep me")
                expected = ok(run(s.paths, mode["flags"]))
                with monkeypatch.context() as m:
                    _install_faults(m, stage, str(s.out), existing, expected)
                    bad(run(s.paths, [*mode["flags"], "--output", s.out]), f"injected {stage}")
                assert s.out.exists() == existing
                if existing:
                    assert s.out.read_text() == "keep me"
                s.unchanged()
                s.no_temps()
    write_raw(s.out, "keep me")
    with monkeypatch.context() as m:
        _install_faults(m, "changed", str(s.out), True)
        bad(run(s.paths, ["--output", s.out]), "destination changed")
    assert s.out.read_text() == "external writer"
    s.no_temps()
    s.unchanged()
    bad(run(s.paths, ["--output", s.dir / "missing-parent" / "report.json"]), "ENOENT")
    bad(run(s.paths, ["--output", s.paths[0] / "report.json"]), "ENOTDIR")
    s.no_temps()
    s.unchanged()


def test_exclusive_temporary_name_collision_never_deletes_a_file_owned_by_someone_else(tmp_path, monkeypatch):
    s = Setup(tmp_path)
    write_raw(s.out, "keep me")
    real_open = local_output.fs.open
    foreign = {}

    def colliding_open(path, flags, mode=0o777):
        if flags & os.O_EXCL:
            foreign["path"] = path
            write_raw(path, "foreign bytes")
        return real_open(path, flags, mode)

    monkeypatch.setattr(local_output.fs, "open", colliding_open)
    with pytest.raises(Exception, match="EEXIST"):
        local_output.write_local_output(str(s.out), [str(p) for p in s.paths], "complete bytes")
    assert s.out.read_text() == "keep me"
    assert open(foreign["path"]).read() == "foreign bytes"
    assert [p for p in os.listdir(s.dir) if p.startswith(".alert-flapsim-")] == [os.path.basename(foreign["path"])]
    s.unchanged()


@POSIX
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory"])
def test_destination_alias_or_type_changes_after_writing_reject_rename_and_clean_temporary(tmp_path, monkeypatch, kind):
    s = Setup(tmp_path)
    write_raw(s.out, "keep me")
    real_close = local_output.fs.close

    def swapping_close(fd):
        real_close(fd)
        s.out.unlink()
        if kind == "directory":
            s.out.mkdir()
        elif kind == "symlink":
            os.symlink(s.paths[0], s.out)
        else:
            os.link(s.paths[0], s.out)

    monkeypatch.setattr(local_output.fs, "close", swapping_close)
    with pytest.raises(Exception, match="input|regular|symlink"):
        local_output.write_local_output(str(s.out), [str(p) for p in s.paths], "complete bytes")
    monkeypatch.undo()
    s.unchanged()
    s.no_temps()


def test_cleanup_denial_reports_both_errors_and_preserves_target_bytes(tmp_path, monkeypatch):
    s = Setup(tmp_path)
    write_raw(s.out, "keep me")

    def deny_rename(src, dst):
        raise RuntimeError("rename denied")

    def deny_unlink(path):
        raise RuntimeError("unlink denied")

    monkeypatch.setattr(local_output.fs, "rename", deny_rename)
    monkeypatch.setattr(local_output.fs, "unlink", deny_unlink)
    with pytest.raises(Exception, match="rename denied; cleanup failed: remove temporary file .*unlink denied"):
        local_output.write_local_output(str(s.out), [str(p) for p in s.paths], "complete bytes")
    assert s.out.read_text() == "keep me"
    s.unchanged()
    remaining = [p for p in os.listdir(s.dir) if p.startswith(".alert-flapsim-")]
    assert len(remaining) == 1
    assert (s.dir / remaining[0]).read_text() == "complete bytes"
