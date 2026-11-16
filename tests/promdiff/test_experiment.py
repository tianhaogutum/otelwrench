import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class ExperimentTests(unittest.TestCase):
    def test_reproduce_detection_matrix_and_retained_fixtures(self):
        with tempfile.TemporaryDirectory() as parent:
            directory = Path(parent) / "generated"
            result = subprocess.run([sys.executable, "benchmarks/promdiff/run.py", "--output-dir", str(directory)],
                                    cwd=ROOT, text=True, capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            expected = json.loads((ROOT / "benchmarks/promdiff/results.json").read_text())
            # The recorded run used prometheus-client 0.25.0; newer releases are allowed.
            data.pop("prometheus_client_version"); expected.pop("prometheus_client_version")
            self.assertEqual(data, expected)
            self.assertEqual(data["summary"], {
                "cases": 9, "known_violations": 4, "compatible_cases": 5,
                "single_snapshot_detected_violations": 0, "guard_detected_violations": 4,
                "guard_false_violations": 0, "all_oracle_checks_passed": True,
            })
            self.assertEqual(len(list(directory.glob("*.prom"))), 10)
            self.assertEqual(len(list(directory.glob("*.report.json"))), 9)
            # Independently inspect wire data rather than regenerating it with
            # production code or trusting the report's compatibility status.
            baseline = (directory / "baseline.prom").read_text()
            mutated = (directory / "label_keys.prom").read_text()
            self.assertEqual(baseline.count(',region="eu"'), 0)
            self.assertEqual(mutated.count(',region="eu"'), 5)
            growth = (directory / "growth.prom").read_text()
            self.assertEqual(sum(not line.startswith("#") for line in growth.splitlines()), 125)


if __name__ == "__main__":
    unittest.main()
