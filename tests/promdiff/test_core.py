import unittest

from otelwrench.promdiff.compare import compare
from otelwrench.promdiff.parser import InputError, parse_snapshot


class CoreTests(unittest.TestCase):
    def test_value_only(self):
        report = compare(parse_snapshot('x{a="b"} 1\n'), parse_snapshot('x{a="b"} NaN\n'))
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["changes"], [])

    def test_schema_change(self):
        report = compare(parse_snapshot('x{a="b"} 1\n'), parse_snapshot('x{a="b",r="eu"} 2\n'))
        self.assertEqual(report["status"], "fail")
        self.assertIn("label_schema_changed", [c["kind"] for c in report["changes"]])

    def test_malformed(self):
        for text in ['x{a="bad\\q"} 1\n', 'x 1\nx 2\n', '# EOF\n', '# TYPE x gauge\n# TYPE x counter\nx 1\n']:
            with self.subTest(text=text), self.assertRaises(InputError):
                parse_snapshot(text)


if __name__ == "__main__":
    unittest.main()
