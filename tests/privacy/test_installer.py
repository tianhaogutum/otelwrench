import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
from types import SimpleNamespace

import pytest


@pytest.fixture
def installer():
    from otelwrench.privacy import install

    return install


def test_install_promotion_rolls_back_binary_if_lock_cannot_be_committed(
    installer, local_tmp, monkeypatch
):
    dest = local_tmp / "dest"
    staging = local_tmp / "staging"
    dest.mkdir()
    staging.mkdir()
    (dest / "otelcol-contrib").write_text("previous executable")
    (dest / "collector.lock.json").write_text("previous lock")
    binary = staging / "otelcol-contrib"
    lock = staging / "collector.lock.json"
    binary.write_text("next executable")
    lock.write_text("next lock")
    original = Path.replace

    def fail_lock(self, target):
        if self == lock:
            raise OSError("simulated lock promotion failure")
        return original(self, target)

    monkeypatch.setattr(Path, "replace", fail_lock)
    with pytest.raises(OSError):
        installer._promote(binary, lock, dest)
    assert (dest / "otelcol-contrib").read_text() == "previous executable"
    assert (dest / "collector.lock.json").read_text() == "previous lock"


def _fake_release(installer, monkeypatch, dest, checksum_valid=True, no_space=False):
    content = (f"#!{sys.executable}\nprint('otelcol-contrib version 1.2.3')\n").encode()
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        info = tarfile.TarInfo("otelcol-contrib")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
        ignored = tarfile.TarInfo("../must-not-be-extracted")
        ignored.size = 7
        archive.addfile(ignored, io.BytesIO(b"ignored"))
    data = stream.getvalue()
    checksum = hashlib.sha256(data).hexdigest() if checksum_valid else "0" * 64
    name = "otelcol-contrib_1.2.3_darwin_arm64.tar.gz"

    def get(url, **_kwargs):
        body = f"\n{checksum}  {name}\n".encode() if url.endswith(".txt") else data
        response = io.BytesIO(body)
        response.headers = {"Content-Length": str(len(body))}
        return response

    monkeypatch.setattr(installer.urllib.request, "urlopen", get)
    monkeypatch.setattr(installer.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(installer.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(
        sys, "argv", ["install", "--version", "1.2.3", "--dest", str(dest), "--force"]
    )
    if no_space:
        monkeypatch.setattr(installer.shutil, "disk_usage", lambda _p: SimpleNamespace(free=0))


def test_checksum_installer_extracts_only_binary_and_pins_it(installer, local_tmp, monkeypatch):
    dest = local_tmp / "tools"
    _fake_release(installer, monkeypatch, dest)
    assert installer.main() == 0
    assert sorted(path.name for path in dest.iterdir()) == [
        "collector.lock.json",
        "otelcol-contrib",
    ]
    assert not (local_tmp / "must-not-be-extracted").exists()
    assert json.loads((dest / "collector.lock.json").read_text())["version"] == "1.2.3"


def test_bad_checksum_leaves_no_partial_install(installer, local_tmp, monkeypatch):
    dest = local_tmp / "tools"
    _fake_release(installer, monkeypatch, dest, checksum_valid=False)
    with pytest.raises(SystemExit) as exc:
        installer.main()
    assert exc.value.code == 2
    assert not list(dest.iterdir())


def test_disk_budget_failure_retains_existing_install(installer, local_tmp, monkeypatch):
    dest = local_tmp / "tools"
    dest.mkdir()
    (dest / "otelcol-contrib").write_text("original binary")
    (dest / "collector.lock.json").write_text("original lock")
    _fake_release(installer, monkeypatch, dest, no_space=True)
    assert installer.main() == 1
    assert (dest / "otelcol-contrib").read_text() == "original binary"
    assert (dest / "collector.lock.json").read_text() == "original lock"


def test_failed_rollback_keeps_recovery_binary_outside_temporary_staging(
    installer, local_tmp, monkeypatch
):
    dest = local_tmp / "dest"
    staging = local_tmp / "temporary-download"
    dest.mkdir()
    staging.mkdir()
    (dest / "otelcol-contrib").write_text("previous executable")
    (dest / "collector.lock.json").write_text("previous lock")
    binary, lock = staging / "otelcol-contrib", staging / "collector.lock.json"
    binary.write_text("next executable")
    lock.write_text("next lock")
    original = Path.replace

    def full_metadata(self, target):
        if self == lock or self.name == ".otelcol-contrib.previous":
            raise OSError("persistent metadata write failure")
        return original(self, target)

    monkeypatch.setattr(Path, "replace", full_metadata)
    with pytest.raises(OSError, match="remains at"):
        installer._promote(binary, lock, dest)
    assert (dest / ".otelcol-contrib.previous").read_text() == "previous executable"
    assert (dest / "collector.lock.json").read_text() == "previous lock"
