# SPDX-License-Identifier: Apache-2.0
import json
import os
import sys

import pytest

from otelwrench.tracepack import create_bundle, stringify

from .support import (BUNDLE_LIMIT, ORIGINAL, RefusalGuard, growing_chunks, ino, mode, refuse, run, run_process,
                      snapshot, sparse, wire)

NONREGULAR = {"error": "bundle input must be a regular file, not a directory/device/FIFO"}
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="FIFOs require POSIX")
linux_devices = ["/dev/null", "/dev/zero"] if sys.platform.startswith("linux") else []


def setup(tmp_path):
    bundle, output, source = tmp_path / "bundle", tmp_path / "output", tmp_path / "source"
    bundle.write_text(wire(create_bundle(ORIGINAL)), encoding="utf-8")
    source.write_bytes(ORIGINAL)
    output.write_bytes(b"PRESERVE\r\n")
    os.chmod(output, 0o640)
    return bundle, output, source


def failure(result, expected=NONREGULAR, code=1):
    assert result.status == code, result.stderr
    assert result.stdout == ""
    assert result.stderr == stringify(expected)


def success(result, command):
    assert result.status == 0, result.stderr
    assert result.stderr == ""
    if command == "extract":
        assert result.stdout == ""
    else:
        assert json.loads(result.stdout)["valid"] is True


@posix_only
@pytest.mark.parametrize("command", ["verify", "extract"])
@pytest.mark.parametrize("linked", [False, True])
def test_refuses_fifo_promptly_before_source_or_destination_access(tmp_path, command, linked):
    _, output, source = setup(tmp_path)
    pipe, link = tmp_path / "pipe", tmp_path / "pipe-link"
    os.mkfifo(pipe)
    os.symlink(pipe, link)
    path = link if linked else pipe
    before = snapshot(tmp_path)
    if command == "verify":
        cases = [["--source", source, "--strict-loss", "--report"]]
    else:
        cases = [["--output", p, "--strict-loss"] for p in (output, tmp_path / "absent", path,
                                                             tmp_path / "missing-parent" / "output")]
    # Default mode must also refuse; policy/report/source flags cannot bypass input validation.
    cases.append([] if command == "verify" else ["--output", output])
    for extra in cases:
        failure(RefusalGuard(path).run([command, path, *extra]))
        assert snapshot(tmp_path) == before


@pytest.mark.parametrize("command", ["verify", "extract"])
def test_refuses_directory_and_device_handles_without_reading_or_touching_output(tmp_path, command):
    _, output, _ = setup(tmp_path)
    directory = tmp_path / "directory"
    directory.mkdir()
    before = snapshot(tmp_path)
    for path in (directory, *linux_devices):
        failure(RefusalGuard(path).run([command, path, *(["--output", output] if command == "extract" else [])]))
        assert snapshot(tmp_path) == before
        assert os.listdir(directory) == []


def _swapping_open(link, target, after_open=False):
    real_open = os.open

    def _open(path, flags, mode=0o777):
        if not after_open and path == str(link):
            os.unlink(path)
            os.symlink(target, path)
        fd = real_open(path, flags, mode)
        if after_open and path == str(link):
            os.unlink(path)
            os.symlink(target, path)
        return fd
    return _open


@posix_only
def test_bundle_path_swaps_to_fifo_are_checked_on_the_nonblocking_opened_handle(tmp_path):
    bundle, output, _ = setup(tmp_path)
    pipe, link = tmp_path / "pipe", tmp_path / "link"
    os.mkfifo(pipe)
    before, info = output.read_bytes(), os.lstat(output)
    for command in ("verify", "extract"):
        if os.path.lexists(link):
            link.unlink()
        os.symlink(bundle, link)
        failure(run([command, link, *(["--output", output] if command == "extract" else [])],
                    hooks={"_open": _swapping_open(link, pipe)}))
        assert os.readlink(link) == str(pipe)
        assert output.read_bytes() == before
        after = os.lstat(output)
        assert (after.st_ino, after.st_mode, after.st_mtime_ns) == (info.st_ino, info.st_mode, info.st_mtime_ns)
    assert sorted(os.listdir(tmp_path)) == ["bundle", "link", "output", "pipe", "source"]


@posix_only
def test_an_already_opened_regular_handle_remains_the_input_when_its_symlink_changes(tmp_path):
    bundle, output, _ = setup(tmp_path)
    pipe, link = tmp_path / "pipe", tmp_path / "link"
    os.mkfifo(pipe)
    for command in ("verify", "extract"):
        if os.path.lexists(link):
            link.unlink()
        os.symlink(bundle, link)
        flags = ["--output", output, "--strict-loss"] if command == "extract" else ["--strict-loss", "--report"]
        success(run([command, link, *flags], hooks={"_open": _swapping_open(link, pipe, after_open=True)}), command)
        assert os.readlink(link) == str(pipe)
    assert output.read_bytes() == ORIGINAL


def test_regular_files_symlinks_and_stdin_preserve_verification_and_exact_atomic_extraction(tmp_path):
    bundle, output, source = setup(tmp_path)
    link, old = tmp_path / "link", tmp_path / "old"
    os.symlink(bundle, link)
    text = bundle.read_bytes()
    expected = run(["verify", "-", "--report", "--source", source, "--strict-loss"], text)
    success(expected, "verify")
    assert json.loads(expected.stdout)["sourceMatches"] is True
    assert json.loads(expected.stdout)["report"] == {"from": "zipkin", "to": "otlp", "spanCount": 1, "losses": []}
    os.link(output, old)
    inode = ino(output)
    for path in (bundle, link, "-"):
        stdin = text if path == "-" else None
        verified = run(["verify", path, "--strict-loss", "--source", source, "--report"], stdin)
        success(verified, "verify")
        assert verified.stdout == expected.stdout
        success(run(["extract", path, "--output", output, "--strict-loss"], stdin), "extract")
        assert output.read_bytes() == ORIGINAL and mode(output) == 0o600
    assert ino(output) != inode and old.read_bytes() == b"PRESERVE\r\n"
    success(run(["extract", bundle, "--output", bundle, "--strict-loss"]), "extract")
    assert bundle.read_bytes() == ORIGINAL and link.is_symlink()
    assert sorted(os.listdir(tmp_path)) == ["bundle", "link", "old", "output", "source"]


@posix_only
def test_explicit_stdin_can_stream_from_a_fifo_when_the_caller_opts_in(tmp_path):
    _, output, _ = setup(tmp_path)
    pipe = tmp_path / "pipe"
    os.mkfifo(pipe)
    # Queue one small document, retain a read-only descriptor, then close the writer for EOF.
    keeper = os.open(pipe, os.O_RDWR)
    try:
        os.write(keeper, wire(create_bundle(ORIGINAL)).encode())
        fd = os.open(pipe, os.O_RDONLY)
    finally:
        os.close(keeper)
    try:
        for command in ("verify", "extract"):
            if command == "extract":
                writer = os.open(pipe, os.O_WRONLY)
                try:
                    os.write(writer, wire(create_bundle(ORIGINAL)).encode())
                finally:
                    os.close(writer)
            result = run_process([command, "-", "--strict-loss",
                                  *(["--report"] if command == "verify" else ["--output", output])], stdin=fd)
            success(result, command)
        assert output.read_bytes() == ORIGINAL
    finally:
        os.close(fd)


def test_bundle_file_size_is_refused_before_consumption_including_through_symlinks(tmp_path):
    _, output, _ = setup(tmp_path)
    large, link = tmp_path / "large", tmp_path / "large-link"
    sparse(large, BUNDLE_LIMIT + 1)
    os.symlink(large, link)
    for command in ("verify", "extract"):
        for path in (large, link):
            failure(RefusalGuard(path).run([command, path, *(["--output", output] if command == "extract" else [])]),
                    {"error": "input exceeds 256 MiB limit"})
    assert output.read_bytes() == b"PRESERVE\r\n" and os.lstat(large).st_size == BUNDLE_LIMIT + 1


def test_bounded_byte_counting_remains_active_for_growing_files_and_explicit_stdin(tmp_path):
    bundle, output, _ = setup(tmp_path)
    hooks = {"_file_chunks": growing_chunks(257), "_stdin_chunks": growing_chunks(257),
             "_lstat": refuse("destination inspected before size refusal")}
    for command in ("verify", "extract"):
        for path in (bundle, "-"):
            failure(run([command, path, *(["--output", output] if command == "extract" else [])], hooks=hooks),
                    {"error": "input exceeds 256 MiB limit"})
    assert output.read_bytes() == b"PRESERVE\r\n"


@posix_only
def test_invalid_flags_precede_primary_bundle_open_even_for_unread_fifos(tmp_path):
    _, output, _ = setup(tmp_path)
    pipe = tmp_path / "pipe"
    os.mkfifo(pipe)
    hooks = {"_open": refuse("input opened before flag validation")}
    for command in ("verify", "extract"):
        for flags, error in (
            (["--strict-loss", "--strict-loss"], "duplicate option --strict-loss"),
            (["--report", "--report"],
             "duplicate option --report" if command == "verify" else "unknown option --report for extract"),
            (["--source"], "missing value for --source" if command == "verify" else "unknown option --source for extract"),
            (["--dry-run"], f"unknown option --dry-run for {command}"),
        ):
            failure(run([command, pipe, *(["--output", output] if command == "extract" else []), *flags], hooks=hooks),
                    {"error": error})
    failure(run(["extract", pipe], hooks=hooks), {"error": "extract requires --output FILE (a file path, not stdout)"})
