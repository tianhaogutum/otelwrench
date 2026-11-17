"""Fetch an official small release, verify its archive, extract only its binary.

This is the only part of ``otelwrench privacy`` that uses the network, and it runs
only when invoked explicitly as ``otelwrench privacy install-collector``. Nothing is
downloaded or bundled when otelwrench itself is installed.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
from pathlib import Path
import platform
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.request

from .collector import identify, verify_pin
from .inputs import ReplayError


LIMIT = 50 * 1024 * 1024
KNOWN_ARCHIVES = {
    (
        "0.72.0",
        "darwin",
        "arm64",
    ): "77777ea9aaa73b5ffb0cf7fef689fdf9826dca1ccaed9cf9666135b38ad06e70",
}


def _promote(extracted: Path, lock_file: Path, dest: Path) -> None:
    target = dest / "otelcol-contrib"
    # Preserve this link outside temporary staging even if rollback itself fails.
    backup = dest / ".otelcol-contrib.previous"
    if backup.exists():
        raise OSError(f"previous recovery binary exists at {backup}; recover it before replacing")
    if target.exists():
        backup.hardlink_to(target)
    try:
        extracted.replace(target)
        lock_file.replace(dest / "collector.lock.json")
    except OSError:
        if backup.exists():
            try:
                backup.replace(target)
            except OSError as exc:
                raise OSError(
                    f"rollback could not rename the previous executable; it remains at {backup}"
                ) from exc
        else:
            target.unlink(missing_ok=True)
        raise
    backup.unlink(missing_ok=True)


def install(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="otelwrench privacy install-collector",
        description="Fetch an official small Collector release, verify its archive SHA-256 "
        "against the published checksums (and the built-in pin), extract only its binary "
        "and write .tools/collector.lock.json.",
    )
    parser.add_argument("--version", default="0.72.0")
    parser.add_argument("--dest", type=Path, default=Path(".tools"))
    parser.add_argument(
        "--force", action="store_true", help="replace an existing locally pinned binary"
    )
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", args.version):
        parser.error("version must be an exact numeric release")
    system = platform.system().lower()
    arch = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64"}.get(platform.machine())
    if system not in {"darwin", "linux"} or not arch:
        parser.error(
            "automatic installation supports macOS/Linux arm64/amd64 only; pin a supplied binary"
        )
    args.dest.mkdir(parents=True, exist_ok=True)
    existing, lock = args.dest / "otelcol-contrib", args.dest / "collector.lock.json"
    if existing.exists() and lock.exists() and not args.force:
        pin = verify_pin(existing, lock)
        if pin["version"] == args.version:
            print(
                f"Using existing Collector {args.version}; matches the local version/SHA-256 lock"
            )
            return
    name = f"otelcol-contrib_{args.version}_{system}_{arch}.tar.gz"
    base = (
        "https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/"
        f"v{args.version}/"
    )
    with urllib.request.urlopen(
        base + "opentelemetry-collector-releases_checksums.txt", timeout=20
    ) as response:
        checksums = response.read(1024 * 1024).decode()
    expected = next(
        (
            line.split()[0]
            for line in checksums.splitlines()
            if line.strip() and line.split()[-1] == name
        ),
        None,
    )
    if expected is None:
        parser.error("release does not publish a checksum for this platform")
    known = KNOWN_ARCHIVES.get((args.version, system, arch))
    if known and expected != known:
        parser.error("published checksum differs from the repository pin")
    with tempfile.TemporaryDirectory(prefix="download-", dir=args.dest) as temporary:
        tmp = Path(temporary)
        archive = tmp / name
        digest = hashlib.sha256()
        size = 0
        with (
            urllib.request.urlopen(base + name, timeout=30) as response,
            archive.open("wb") as output,
        ):
            if int(response.headers.get("Content-Length", "0")) > LIMIT:
                parser.error("archive exceeds the 50 MiB limit; use an already installed binary")
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > LIMIT:
                    parser.error("archive exceeds the 50 MiB limit")
                digest.update(chunk)
                output.write(chunk)
        if digest.hexdigest() != expected:
            parser.error("archive SHA-256 mismatch")
        extracted = tmp / "otelcol-contrib"
        with tarfile.open(archive) as tar:
            member = next(
                (m for m in tar if Path(m.name).name == "otelcol-contrib" and m.isfile()), None
            )
            if member is None:
                parser.error("archive lacks a regular Collector executable")
            if shutil.disk_usage(args.dest).free < member.size + 1024 * 1024:
                raise OSError(
                    errno.ENOSPC,
                    "not enough free space to extract the Collector; old install retained",
                )
            with tar.extractfile(member) as source, extracted.open("wb") as output:
                shutil.copyfileobj(source, output)
        extracted.chmod(0o755)
        pin = identify(extracted)
        if pin["version"] != args.version:
            parser.error("extracted binary reports an unexpected version")
        pin["archive_sha256"] = expected
        pin["archive_url"] = base + name
        prepared_lock = tmp / "collector.lock.json"
        prepared_lock.write_text(json.dumps(pin, indent=2) + "\n")
        _promote(extracted, prepared_lock, args.dest)
    print(f"Installed and pinned Collector {args.version}; verified {size} archive bytes")


def main(argv: list[str] | None = None) -> int:
    try:
        install(argv)
        return 0
    except (OSError, ReplayError, ValueError, tarfile.TarError) as exc:
        print(f"installation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
