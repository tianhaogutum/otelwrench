import errno
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

import pytest

from otelwrench.privacy import cli, collector as runtime
from otelwrench.privacy.collector import _LogCapture, identify, run_stage
from otelwrench.privacy.inputs import ReplayError, sample_messages


def test_atomic_report_write_preserves_previous_evidence_on_enospc(monkeypatch, local_tmp):
    path = local_tmp / "report.json"
    path.write_text('{"status":"previous-complete"}\n')

    def no_space(_obj, stream, **_kwargs):
        stream.write('{"partial":')
        raise OSError(errno.ENOSPC, "simulated full disk")

    monkeypatch.setattr(cli.json, "dump", no_space)
    with pytest.raises(OSError):
        cli.write_json(path, {"status": "new"})
    assert json.loads(path.read_text()) == {"status": "previous-complete"}
    assert list(local_tmp.iterdir()) == [path]


def test_log_capture_drains_large_output_with_a_fixed_disk_budget(local_tmp):
    path = local_tmp / "collector.log"
    program = (
        "import sys,json; "
        "print(json.dumps({'level':'info','msg':'Everything is ready. Begin running and processing data.'}),flush=True); "
        "sys.stdout.buffer.write(b'x' * (2 * 1024 * 1024))"
    )
    process = subprocess.Popen([sys.executable, "-c", program], stdout=subprocess.PIPE)
    capture = _LogCapture(process.stdout, path, limit=64 * 1024)
    try:
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        capture.join()
    assert capture.ready.is_set()
    assert capture.metadata()["truncated"]
    assert path.stat().st_size == 64 * 1024
    assert capture.total_bytes > 2 * 1024 * 1024


def test_readiness_text_in_an_error_or_plain_line_is_not_a_ready_record(local_tmp):
    program = (
        "import json; "
        "print(json.dumps({'level':'error','msg':'Everything is ready. Begin running and processing data.'})); "
        "print('Everything is ready. Begin running and processing data.')"
    )
    process = subprocess.Popen([sys.executable, "-c", program], stdout=subprocess.PIPE)
    capture = _LogCapture(process.stdout, local_tmp / "error.log")
    try:
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        capture.join()
    assert not capture.ready.is_set()


@pytest.mark.integration
def test_port_collision_does_not_send_telemetry_to_an_unrelated_listener(
    collector, root, local_tmp, monkeypatch
):
    messages, kind = sample_messages(root / "examples/privacy/traces.pb", "traces")
    with socket.socket() as unrelated:
        unrelated.bind(("127.0.0.1", 0))
        unrelated.listen(2)
        unrelated.settimeout(0.1)
        monkeypatch.setattr(runtime, "_free_port", lambda: unrelated.getsockname()[1])
        with pytest.raises(ReplayError, match="failed to start"):
            run_stage(collector, "traces", messages, kind, local_tmp / "collision", None, 2)
        with pytest.raises(TimeoutError):
            unrelated.accept()


def _sleeping_binary(directory: Path) -> tuple[Path, Path]:
    binary = directory / "fake-collector"
    pidfile = directory / "child.pid"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import os,sys,time\n"
        "from pathlib import Path\n"
        "if '--version' in sys.argv:\n"
        " print('otelcol-contrib version 9.9.9')\n"
        " sys.exit(0)\n"
        f"Path({str(pidfile)!r}).write_text(str(os.getpid()))\n"
        "print('starting but never ready',flush=True)\n"
        "time.sleep(20)\n"
    )
    binary.chmod(0o755)
    return binary, pidfile


def _assert_gone(pidfile: Path):
    assert pidfile.is_file()
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


def test_startup_timeout_kills_and_reaps_child(local_tmp, monkeypatch):
    binary, pidfile = _sleeping_binary(local_tmp)
    original = runtime.subprocess.Popen
    children = []

    def track_child(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(runtime.subprocess, "Popen", track_child)
    with pytest.raises(ReplayError, match="startup timed out"):
        run_stage(binary, "traces", [], "application/json", local_tmp / "timeout", None, 0.2)
    assert children and children[0].returncode is not None
    with pytest.raises(ProcessLookupError):
        os.kill(children[0].pid, 0)


def test_sigterm_records_interruption_and_reaps_child(root, local_tmp):
    binary, pidfile = _sleeping_binary(local_tmp)
    lock = local_tmp / "lock.json"
    cli.write_json(lock, identify(binary))
    out = local_tmp / "interrupted"
    command = [
        sys.executable,
        "-m",
        "otelwrench",
        "privacy",
        "run",
        "--config",
        str(root / "examples/privacy/collector.yaml"),
        "--sample",
        str(root / "examples/privacy/traces.pb"),
        "--pipeline",
        "traces",
        "--collector",
        str(binary),
        "--lock",
        str(lock),
        "--out",
        str(out),
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 5
        while not pidfile.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pidfile.exists()
        process.send_signal(signal.SIGTERM)
        _stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 130, stderr.decode()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    _assert_gone(pidfile)
    report = json.loads((out / "report.json").read_text())
    assert report["status"] == "interrupted"
    assert report["privacy_verdict"] == "not_evaluated"
