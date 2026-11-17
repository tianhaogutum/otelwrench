"""Create real OTLP JSON/JSONL fixtures with the installed Collector's file exporter.

Run from the repository root after `otelwrench privacy install-collector`:

    python examples/privacy/export_json_fixtures.py
"""

import json
from pathlib import Path
import tempfile

from otelwrench.privacy.collector import run_stage, verify_pin
from otelwrench.privacy.inputs import sample_messages


def main() -> None:
    root = Path(__file__).resolve().parents[2]  # repository root; .tools/ holds the pinned Collector
    binary = root / ".tools/otelcol-contrib"
    runtime = verify_pin(binary, root / ".tools/collector.lock.json")
    with tempfile.TemporaryDirectory(prefix="fixture-export-", dir=root / ".tools") as temporary:
        for signal in ("traces", "logs", "metrics"):
            messages, content_type = sample_messages(root / f"examples/privacy/{signal}.pb", signal)
            workdir = Path(temporary) / signal
            actual = run_stage(binary, signal, messages, content_type, workdir, None)
            (root / f"examples/privacy/{signal}.json").write_text(json.dumps(actual, indent=2) + "\n")
            (root / f"examples/privacy/{signal}.jsonl").write_bytes((workdir / "output.jsonl").read_bytes())
    print(f"Exported genuine JSON/JSONL fixtures with Collector {runtime['version']}")


if __name__ == "__main__":
    main()
