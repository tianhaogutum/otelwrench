# SPDX-License-Identifier: Apache-2.0
import importlib.util
from pathlib import Path
import unittest

SOURCE = Path(__file__).resolve().parents[2] / "benchmarks/cardinality/experiment.py"
SPEC = importlib.util.spec_from_file_location("experiment", SOURCE)
EXPERIMENT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXPERIMENT)


class ExperimentTests(unittest.TestCase):
    def test_real_cli_small_experiment_with_independent_oracle(self):
        result = EXPERIMENT.run_experiment(routes=2, pods=3, repeats=1)
        baseline, safe, unsafe = result["results"]
        self.assertEqual([p["projected_keys"] for p in result["results"]], [12, 12, 4])
        self.assertEqual([p["unsafe"] for p in result["results"]], [False, False, True])
        self.assertEqual(unsafe["colliding_original_series"], 12)
        self.assertEqual(unsafe["collision_value_relations"], {"equal": 2, "different": 2})
        self.assertEqual(len(result["input_sha256"]), 64)
        self.assertEqual(EXPERIMENT.generate(2, 3)[0], EXPERIMENT.generate(2, 3)[0])

    def test_invalid_generator_options(self):
        for routes, pods in ((0, 3), (1, 1), (-1, 3)):
            with self.subTest(routes=routes, pods=pods), self.assertRaises(ValueError):
                EXPERIMENT.generate(routes, pods)
        with self.assertRaises(ValueError):
            EXPERIMENT.run_experiment(repeats=0)


if __name__ == "__main__":
    unittest.main()
