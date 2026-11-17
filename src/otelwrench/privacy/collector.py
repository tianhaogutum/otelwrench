"""Run an unmodified, pinned Collector briefly, exclusively on loopback."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import signal as signals
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request

import yaml

from .inputs import ReplayError, SIGNAL_KEYS, bound_document, bounded_read, strict_json


class _LogCapture:
    """Drain output without blocking the Collector or allowing unlimited log files."""

    def __init__(self, stream, path: Path, limit: int = 1024 * 1024):
        self.stream = stream
        self.path = path
        self.limit = limit
        self.ready = threading.Event()
        self.failure: OSError | None = None
        self.total_bytes = 0
        self.captured_bytes = 0
        self.thread = threading.Thread(
            target=self._drain, name="collector-log-capture", daemon=True
        )
        self.thread.start()

    def _drain(self) -> None:
        window = b""
        try:
            with self.path.open("wb") as output:
                while chunk := self.stream.read1(8192):
                    self.total_bytes += len(chunk)
                    window = (window + chunk)[-65536:]
                    lines = window.split(b"\n")
                    window = lines.pop()
                    for line in lines:
                        try:
                            record = json.loads(line)
                        except (ValueError, UnicodeDecodeError, RecursionError):
                            continue
                        if (
                            isinstance(record, dict)
                            and record.get("level") == "info"
                            and record.get("msg")
                            == "Everything is ready. Begin running and processing data."
                        ):
                            self.ready.set()
                    saved = chunk[: max(0, self.limit - self.captured_bytes)]
                    if saved and self.failure is None:
                        try:
                            output.write(saved)
                            output.flush()
                            self.captured_bytes += len(saved)
                        except OSError as exc:
                            self.failure = exc
        except OSError as exc:
            self.failure = exc
        finally:
            self.stream.close()

    def join(self) -> None:
        self.thread.join(timeout=3)
        if self.thread.is_alive():
            raise ReplayError("Collector log reader did not stop after process termination")

    def metadata(self) -> dict:
        return {
            "captured_bytes": self.captured_bytes,
            "total_bytes": self.total_bytes,
            "truncated": self.total_bytes > self.captured_bytes,
        }


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def identify(binary: Path) -> dict:
    binary = binary.resolve(strict=True)
    try:
        result = subprocess.run(
            [str(binary), "--version"], capture_output=True, text=True, timeout=10, check=True
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise ReplayError(f"cannot execute Collector: {exc}") from exc
    version_text = (result.stdout + result.stderr).strip()
    match = re.search(r"\bversion\s+v?(\d+\.\d+\.\d+(?:[-+][\w.]+)?)\b", version_text)
    if not match:
        raise ReplayError(f"unrecognized Collector version output: {version_text[:200]}")
    return {"version": match.group(1), "sha256": digest(binary), "version_output": version_text}


def verify_pin(binary: Path, lock: Path) -> dict:
    expected = strict_json(bounded_read(lock, 8192))
    if not isinstance(expected, dict) or not {"version", "sha256"} <= expected.keys():
        raise ReplayError("Collector lock must contain version and sha256")
    actual = identify(binary)
    if any(actual[key] != expected[key] for key in ("version", "sha256")):
        raise ReplayError(
            "Collector does not match the version/SHA-256 lock; use the intended binary "
            "or explicitly create a new lock with 'otelwrench privacy pin'"
        )
    return actual


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _read_export(path: Path, signal: str) -> dict:
    key = SIGNAL_KEYS[signal]
    result: dict = {key: []}
    if not path.exists():
        return result
    total = 0
    with path.open("rb") as stream:
        while line := stream.readline(32 * 1024 * 1024 + 1):
            total += len(line)
            if total > 32 * 1024 * 1024:
                raise ReplayError("Collector output exceeded 32 MiB")
            if not line.strip():
                continue
            try:
                data = strict_json(line)
                result[key].extend(data.get(key, []))
            except (json.JSONDecodeError, TypeError, AttributeError) as exc:
                raise ReplayError("Collector file exporter produced invalid OTLP JSON") from exc
    bound_document(result)
    return result


def _check_response(body: bytes, content_type: str, signal: str) -> None:
    if not body:
        return
    if "json" in content_type:
        result = strict_json(body)
        partial = result.get("partialSuccess") or result.get("partial_success")
    else:
        from google.protobuf.message import DecodeError
        from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceResponse
        from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
            ExportMetricsServiceResponse,
        )
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceResponse,
        )

        response = {
            "traces": ExportTraceServiceResponse,
            "logs": ExportLogsServiceResponse,
            "metrics": ExportMetricsServiceResponse,
        }[signal]()
        try:
            response.ParseFromString(body)
        except DecodeError as exc:
            raise ReplayError("invalid protobuf OTLP response from Collector") from exc
        partial = str(response.partial_success) if response.HasField("partial_success") else None
    if partial:
        raise ReplayError(f"Collector reported OTLP partial success: {partial}")


def run_stage(
    binary: Path,
    signal: str,
    messages: list[bytes],
    content_type: str,
    workdir: Path,
    processor: tuple[str, dict] | None,
    timeout: float = 15,
) -> dict:
    """Each process normalizes or executes exactly one actual upstream processor."""
    return run_pipeline(
        binary,
        signal,
        messages,
        content_type,
        workdir,
        [processor] if processor else [],
        timeout,
    )


def run_pipeline(
    binary: Path,
    signal: str,
    messages: list[bytes],
    content_type: str,
    workdir: Path,
    processors: list[tuple[str, dict]],
    timeout: float = 15,
) -> dict:
    """Execute the actual configured processor chain, without production I/O."""
    workdir.mkdir(parents=True)
    workdir = workdir.resolve()
    port = _free_port()
    receiver = {"protocols": {"http": {"endpoint": f"127.0.0.1:{port}"}}}
    exported = workdir / "output.jsonl"
    configuration = {
        "receivers": {"otlp/replay": receiver},
        "exporters": {"file/replay": {"path": str(exported), "format": "json"}},
        "service": {
            "telemetry": {
                "logs": {"level": "info", "encoding": "json"},
                "metrics": {"level": "none"},
            },
            "pipelines": {
                signal: {
                    "receivers": ["otlp/replay"],
                    "processors": [name for name, _ in processors],
                    "exporters": ["file/replay"],
                }
            },
        },
    }
    if processors:
        configuration["processors"] = dict(processors)
    cfg = workdir / "collector.yaml"
    cfg.write_text(yaml.safe_dump(configuration, sort_keys=False))
    deadline = time.monotonic() + timeout
    process = None
    capture = None
    # Do not inherit OTEL_* configuration or secrets from the invoking environment.
    env = {"PATH": os.defpath, "GOMAXPROCS": "2", "GOMEMLIMIT": "256MiB"}
    log = workdir / "collector.log"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        process = subprocess.Popen(
            [str(binary.resolve()), f"--config={cfg}"],
            cwd=workdir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        capture = _LogCapture(process.stdout, log)
        while True:
            if process.poll() is not None:
                capture.join()
                detail = bounded_read(log, 1024 * 1024).decode(errors="replace")[-6000:]
                raise ReplayError(
                    f"Collector failed to start (unsupported signal/config?): {detail}"
                )
            if capture.failure:
                raise ReplayError(f"cannot capture Collector logs: {capture.failure}")
            if capture.ready.is_set():
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    pass
            if time.monotonic() >= deadline:
                raise ReplayError("Collector startup timed out before its readiness message")
            time.sleep(0.03)
        for message in messages:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ReplayError("Collector replay timed out")
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/{signal}",
                data=message,
                headers={"Content-Type": content_type},
                method="POST",
            )
            try:
                with opener.open(request, timeout=min(remaining, 5)) as response:
                    body = response.read(64 * 1024)
                    response_type = response.headers.get("Content-Type", "")
            except urllib.error.HTTPError as exc:
                detail = exc.read(4096).decode(errors="replace")
                raise ReplayError(
                    f"Collector rejected OTLP request ({exc.code}): {detail}"
                ) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise ReplayError(f"OTLP replay failed: {exc}") from exc
            _check_response(body, response_type, signal)
        process.send_signal(signals.SIGTERM)
        try:
            process.wait(timeout=max(0.2, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            raise ReplayError("Collector shutdown timed out; output is incomplete") from exc
        capture.join()
        if capture.failure:
            raise ReplayError(f"cannot capture Collector logs: {capture.failure}")
        if process.returncode != 0:
            detail = bounded_read(log, 1024 * 1024).decode(errors="replace")[-6000:]
            raise ReplayError(f"Collector exited {process.returncode}: {detail}")
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if capture is not None:
            capture.join()
            (workdir / "log-capture.json").write_text(
                json.dumps(capture.metadata(), indent=2) + "\n"
            )
    return _read_export(exported, signal)
