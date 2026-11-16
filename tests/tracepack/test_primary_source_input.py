# SPDX-License-Identifier: Apache-2.0
import json
import os
import sys

import pytest

from otelwrench.tracepack import stringify

from .support import (ORIGINAL, SOURCE_LIMIT, RefusalGuard, b64, growing_chunks, ino, mode, refuse, run, run_process,
                      snapshot, sparse)

NONREGULAR = {"error": "input must be a regular file, not a directory/device/FIFO"}
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="FIFOs require POSIX")
linux_devices = ["/dev/null", "/dev/zero"] if sys.platform.startswith("linux") else []


def setup(tmp_path):
    source, output = tmp_path / "source", tmp_path / "output"
    source.write_bytes(ORIGINAL)
    output.write_bytes(b"PRESERVE\r\n")
    os.chmod(output, 0o640)
    return source, output


def failure(result, expected=NONREGULAR, code=1):
    assert result.status == code, result.stderr
    assert result.stdout == ""
    assert result.stderr == stringify(expected)


def argv(command, path, extra=()):
    return [command, path, *(["--to", "otlp"] if command == "convert" else []), *extra]


def accepted(result, command, dry_run=False):
    assert result.status == 0, result.stderr
    assert json.loads(result.stderr) == {"from": "zipkin", "to": "otlp", "spanCount": 1, "losses": []}
    if dry_run:
        assert result.stdout == ""
    elif command == "convert":
        assert json.loads(result.stdout) == {"resourceSpans": [{
            "resource": {"attributes": []}, "scopeSpans": [{"spans": [{
                "traceId": "1" * 32, "spanId": "2" * 16, "name": "café/雪", "kind": 0,
                "startTimeUnixNano": "9007199254740993000", "endTimeUnixNano": "9007199254740994000",
                "attributes": [], "events": [], "status": {"code": 0, "message": ""}}]}]}]}
    else:
        bundle = json.loads(result.stdout)
        assert bundle["source"]["data"] == b64(ORIGINAL)
        assert bundle["options"]["strictLoss"] is True
        assert bundle["lossReport"] == json.loads(result.stderr)


@posix_only
@pytest.mark.parametrize("command", ["convert", "bundle"])
@pytest.mark.parametrize("linked", [False, True])
def test_refuses_fifo_promptly_before_bytes_policy_or_output_access(tmp_path, command, linked):
    _, output = setup(tmp_path)
    pipe, link = tmp_path / "pipe", tmp_path / "pipe-link"
    os.mkfifo(pipe)
    os.symlink(pipe, link)
    path = link if linked else pipe
    before = snapshot(tmp_path)
    cases = [[]]
    for p in (output, tmp_path / "absent", path, tmp_path / "missing-parent" / "output"):
        cases += [["--output", p], ["--output", p, "--strict-loss"]]
        if command == "convert":
            cases.append(["--output", p, "--strict-loss", "--dry-run"])
    for extra in cases:
        failure(RefusalGuard(path).run(argv(command, path, extra)))
        assert snapshot(tmp_path) == before


@pytest.mark.parametrize("command", ["convert", "bundle"])
def test_refuses_directory_and_device_handles_before_consumption_and_output_access(tmp_path, command):
    _, output = setup(tmp_path)
    directory = tmp_path / "directory"
    directory.mkdir()
    before = snapshot(tmp_path)
    for path in (directory, *linux_devices):
        failure(RefusalGuard(path).run(argv(command, path, ["--output", output, "--strict-loss"])))
        assert snapshot(tmp_path) == before
        assert os.listdir(directory) == []


@posix_only
def test_primary_source_path_swapped_to_fifo_is_checked_on_its_actual_opened_handle(tmp_path):
    source, output = setup(tmp_path)
    pipe, link = tmp_path / "pipe", tmp_path / "link"
    os.mkfifo(pipe)
    real_open = os.open
    for after_open in (False, True):
        # Before open: regular -> FIFO. After open: FIFO -> regular. Both must reject the FIFO handle.
        def _open(path, flags, mode=0o777, after_open=after_open):
            if path != str(link):
                raise RuntimeError("unexpected output open")

            def swap():
                os.unlink(path)
                os.symlink(source if after_open else pipe, path)
            if not after_open:
                swap()
            fd = real_open(path, flags, mode)
            if after_open:
                swap()
            return fd

        def _file_chunks(fd):
            raise RuntimeError("nonregular bytes consumed")
            yield b""  # pragma: no cover

        for command in ("convert", "bundle"):
            if os.path.lexists(link):
                link.unlink()
            os.symlink(pipe if after_open else source, link)
            before = [e for e in snapshot(tmp_path) if e["name"] != "link"]
            failure(run(argv(command, link, ["--output", output, "--strict-loss"]),
                        hooks={"_open": _open, "_file_chunks": _file_chunks}))
            assert [e for e in snapshot(tmp_path) if e["name"] != "link"] == before
            assert os.readlink(link) == str(source if after_open else pipe)


@posix_only
def test_an_already_opened_regular_handle_stays_the_input_after_its_symlink_switches(tmp_path):
    source, _ = setup(tmp_path)
    pipe, link = tmp_path / "pipe", tmp_path / "link"
    os.mkfifo(pipe)
    real_open = os.open

    def _open(path, flags, mode=0o777):
        fd = real_open(path, flags, mode)
        if path == str(link):
            os.unlink(path)
            os.symlink(pipe, path)
        return fd

    for command in ("convert", "bundle"):
        if os.path.lexists(link):
            link.unlink()
        os.symlink(source, link)
        accepted(run(argv(command, link, ["--strict-loss"]), hooks={"_open": _open}), command)
        assert os.readlink(link) == str(pipe)


def test_regular_files_symlinks_and_stdin_preserve_reports_bundle_bytes_and_atomic_output(tmp_path):
    source, output = setup(tmp_path)
    link, old = tmp_path / "link", tmp_path / "old"
    os.symlink(source, link)
    os.link(output, old)
    inode = ino(output)
    for command in ("convert", "bundle"):
        expected = run(argv(command, "-", ["--strict-loss"]), ORIGINAL)
        accepted(expected, command)
        for path in (source, link, "-"):
            stdin = ORIGINAL if path == "-" else None
            result = run(argv(command, path, ["--strict-loss"]), stdin)
            accepted(result, command)
            assert result.stdout == expected.stdout
            saved = run(argv(command, path, ["--strict-loss", "--output", output]), stdin)
            assert saved.status == 0, saved.stderr
            assert saved.stdout == "" and saved.stderr == expected.stderr
            assert output.read_text(encoding="utf-8") == expected.stdout and mode(output) == 0o600
            if command == "convert":
                before = snapshot(tmp_path)
                accepted(run(argv(command, path, ["--strict-loss", "--dry-run", "--output", output]), stdin), command, True)
                assert snapshot(tmp_path) == before
                accepted(run(argv(command, path, ["--dry-run", "--output", tmp_path / "missing-parent" / "out"]), stdin),
                         command, True)
        source.write_bytes(ORIGINAL)
        in_place = run(argv(command, source, ["--strict-loss", "--output", source]))
        assert in_place.status == 0, in_place.stderr
        assert source.read_text(encoding="utf-8") == expected.stdout and mode(source) == 0o600
        source.write_bytes(ORIGINAL)
    assert ino(output) != inode and old.read_bytes() == b"PRESERVE\r\n"
    assert sorted(os.listdir(tmp_path)) == ["link", "old", "output", "source"]


@posix_only
def test_explicit_stdin_can_stream_source_from_a_caller_provided_fifo(tmp_path):
    setup(tmp_path)
    pipe = tmp_path / "pipe"
    os.mkfifo(pipe)
    keeper = os.open(pipe, os.O_RDWR)
    try:
        os.write(keeper, ORIGINAL)
        fd = os.open(pipe, os.O_RDONLY)
    finally:
        os.close(keeper)
    try:
        for i, command in enumerate(("convert", "bundle")):
            if i:
                writer = os.open(pipe, os.O_WRONLY)
                try:
                    os.write(writer, ORIGINAL)
                finally:
                    os.close(writer)
            accepted(run_process(argv(command, "-", ["--strict-loss"]), stdin=fd), command)
    finally:
        os.close(fd)


def test_source_file_size_is_refused_before_consumption_including_symlinks_and_dry_run(tmp_path):
    _, output = setup(tmp_path)
    large, link = tmp_path / "large", tmp_path / "large-link"
    sparse(large, SOURCE_LIMIT + 1)
    os.symlink(large, link)
    before = snapshot(tmp_path, ("large", "large-link"))
    for command in ("convert", "bundle"):
        for path in (large, link):
            extra = ["--output", output, *(["--dry-run"] if command == "convert" else [])]
            failure(RefusalGuard(path).run(argv(command, path, extra)), {"error": "input exceeds 64 MiB limit"})
    assert snapshot(tmp_path, ("large", "large-link")) == before


def test_bounded_source_byte_counting_remains_active_for_growing_files_and_explicit_stdin(tmp_path):
    source, output = setup(tmp_path)
    hooks = {"_file_chunks": growing_chunks(65), "_stdin_chunks": growing_chunks(65),
             "_lstat": refuse("output inspected before size refusal")}
    for command in ("convert", "bundle"):
        for path in (source, "-"):
            failure(run(argv(command, path, ["--strict-loss", "--output", output]), hooks=hooks),
                    {"error": "input exceeds 64 MiB limit"})
    assert output.read_bytes() == b"PRESERVE\r\n"


@posix_only
def test_syntactically_invalid_flags_precede_primary_source_open_even_for_unread_fifos(tmp_path):
    setup(tmp_path)
    pipe = tmp_path / "pipe"
    os.mkfifo(pipe)
    hooks = {"_open": refuse("input opened before flag validation")}
    for command in ("convert", "bundle"):
        for flags, error in ((["--strict-loss", "--strict-loss"], "duplicate option --strict-loss"),
                             (["--output"], "missing value for --output"),
                             (["--from"], "missing value for --from"),
                             (["--source", "file"], f"unknown option --source for {command}"),
                             (["--report"], f"unknown option --report for {command}"),
                             (["--unknown"], f"unknown option --unknown for {command}")):
            failure(run(argv(command, pipe, flags), hooks=hooks), {"error": error})
    failure(run(["bundle", pipe, "--dry-run"], hooks=hooks), {"error": "unknown option --dry-run for bundle"})
    failure(run(["convert", pipe, "--to"], hooks=hooks), {"error": "missing value for --to"})


def test_source_stdin_loss_reports_and_strict_loss_refusal_precede_all_output_access(tmp_path):
    _, output = setup(tmp_path)
    lossy = ORIGINAL.decode().replace('"duration":1', '"duration":1,"debug":true').encode()
    report = {"from": "zipkin", "to": "otlp", "spanCount": 1,
              "losses": [{"path": '$[0]["debug"]', "reason": "unsupported field dropped"}]}
    hooks = {"_open": refuse("output accessed before policy"), "_lstat": refuse("output accessed before policy")}
    before = snapshot(tmp_path)
    for command in ("convert", "bundle"):
        normal = run(argv(command, "-"), lossy)
        assert normal.status == 0, normal.stderr
        assert json.loads(normal.stderr) == report
        if command == "bundle":
            assert json.loads(normal.stdout)["source"]["data"] == b64(lossy)
        for destination in (output, tmp_path / "absent", tmp_path / "missing-parent" / "output"):
            for dry in ([[], ["--dry-run"]] if command == "convert" else [[]]):
                failure(run(argv(command, "-", ["--strict-loss", "--output", destination, *dry]), lossy, hooks),
                        {"error": "strict-loss rejected 1 loss(es)", "report": report}, 2)
    assert snapshot(tmp_path) == before
