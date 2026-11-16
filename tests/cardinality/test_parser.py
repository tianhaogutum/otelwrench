# SPDX-License-Identifier: Apache-2.0
import unittest
from pathlib import Path

from otelwrench.cardinality.analysis import report
from otelwrench.cardinality.parser import InputError, parse

ROOT = Path(__file__).resolve().parents[2]


class ParserTests(unittest.TestCase):
    def test_all_family_cardinalities_are_hand_counted(self):
        result = report(parse((ROOT / "examples/cardinality/families.prom").read_text()), [["debug_id"]])
        self.assertEqual(result["summary"]["observed_series"], 8)
        histogram, summary = result["summary"]["families"]
        self.assertEqual((histogram["name"], histogram["type"], histogram["observed_series"]), ("latency_seconds", "histogram", 4))
        self.assertEqual(histogram["metric_names"], ["latency_seconds_bucket", "latency_seconds_count", "latency_seconds_sum"])
        self.assertEqual({k: v["distinct_value_count"] for k, v in histogram["labels"].items()}, {"debug_id": 4, "le": 2, "route": 1})
        self.assertEqual(histogram["labels"]["le"]["absent_series"], 2)
        self.assertEqual(histogram["labels"]["le"]["values"], ["+Inf", "0.1"])
        self.assertEqual((summary["name"], summary["type"], summary["observed_series"]), ("payload_bytes", "summary", 4))
        self.assertEqual({k: v["distinct_value_count"] for k, v in summary["labels"].items()}, {"debug_id": 4, "quantile": 2, "region": 1})
        self.assertEqual(summary["labels"]["quantile"]["values"], ["0.5", "0.99"])
        self.assertEqual(result["plans"][0]["projected_series_by_family"], {"latency_seconds": 4, "payload_bytes": 4})

    def test_escaping_and_delimiters_do_not_merge_distinct_keys(self):
        text = r'''x{a="line\nnext",pod="a"} 1
x{a="line\\nnext",pod="b"} 1
x{a="q\"\\雪",pod="c"} 1
x{a="one,b=two",pod="d"} 1
x{a="one",b="two",pod="e"} 1
'''
        result = report(parse(text), [["pod"]])
        self.assertEqual(result["plans"][0]["projected_keys_after"], 5)
        self.assertFalse(result["unsafe"])
        self.assertEqual(result["summary"]["families"][0]["labels"]["a"]["values"], ["line\nnext", "line\\nnext", "one", "one,b=two", 'q"\\雪'])

    def test_empty_label_identity_and_statistics(self):
        scrape = parse('x{a="",b="1"} 1\nx{b="2"} 2\n')
        result = report(scrape, [["a"]])
        self.assertEqual(scrape.samples[0].key, ("x", (("b", "1"),)))
        stats = result["summary"]["families"][0]["labels"]["a"]
        self.assertEqual(stats, {"distinct_value_count": 1, "values": [""], "present_series": 1, "absent_series": 1, "empty_value_series": 1})
        self.assertFalse(result["unsafe"])
        with self.assertRaisesRegex(InputError, "duplicate input series"):
            parse('x{a=""} 1\nx 2\n')

    def test_empty_and_absent_merge_after_drop(self):
        plan = report(parse('x{a="",pod="a"} 1\nx{pod="b"} 2\n'), [["pod"]])["plans"][0]
        self.assertTrue(plan["unsafe"])
        self.assertEqual(plan["collisions"][0]["projected_key"], {"metric": "x", "labels": {}})
        self.assertEqual(plan["collisions"][0]["originals"][0]["original_labels"], {"a": "", "pod": "a"})

    def test_numeric_subset_and_timestamps(self):
        for value in ("1", "-2.5", "+.5", "1.", "1e-4", "NaN", "Inf", "+Inf", "-Inf"):
            with self.subTest(value=value):
                sample = parse(f"x {value} -9223372036854775808\n").samples[0]
                self.assertEqual(sample.value, value)
                self.assertEqual(sample.timestamp_ms, "-9223372036854775808")
        self.assertEqual(parse("x 1 +0009223372036854775807\n").samples[0].timestamp_ms, "+0009223372036854775807")

    def test_comments_help_and_trailing_comma(self):
        scrape = parse('# ordinary comment\n# HELP x line\\npath\\\\tail\n# TYPE x gauge\nx{a = "b", } 1\t42\n')
        self.assertEqual(scrape.help["x"], "line\npath\\tail")
        self.assertEqual(scrape.samples[0].labels, (("a", "b"),))
        self.assertEqual(scrape.samples[0].timestamp_ms, "42")

    def test_untyped_suffixes_are_not_guessed(self):
        result = report(parse('x_bucket{le="1"} 1\nx_count 1\n'))
        self.assertEqual([f["name"] for f in result["summary"]["families"]], ["x_bucket", "x_count"])

    def test_duplicate_timestamps_and_escaped_equivalent_identity(self):
        for text in ('x 1 1\nx 2 2\n', 'x{a="a",b="b"} 1\nx{b="b",a="a"} 1\n'):
            with self.subTest(text=text), self.assertRaisesRegex(InputError, "duplicate input series"):
                parse(text)

    def test_invalid_or_unsupported_syntax(self):
        invalid = [
            "", "# only comments\n", "x 1", "x 1\r\n", "x 1\x00\n", " x 1\n",
            'x{a="b",a="c"} 1\n', 'x{__name__="y"} 1\n', 'x{__foo="y"} 1\n',
            'x{a="\\t"} 1\n', 'x{a="\\q"} 1\n', 'x{a="b" 1\n', 'x{a="b",,} 1\n',
            'x{a="b"}1\n', 'x{a=b} 1\n', 'x{a "b"} 1\n', 'x{a="b} 1\n',
            "x nope\n", "x 1e999\n", "x 0x1p2\n", "x nan\n", "x Infinity\n",
            "x 1 1.1\n", "x 1 9223372036854775808\n", "x 1 -9223372036854775809\n",
            "x 1 0 more\n", "x 1\v2\n", "x 1\u00a02\n", '"雪" 1\n', "雪 1\n",
            '# TYPE x unknown\nx 1\n', '# TYPE x gauge\n# TYPE x gauge\nx 1\n',
            '# HELP x a\n# HELP x b\nx 1\n', '# HELP\nx 1\n', '# TYPE x\nx 1\n',
            '# HELP x bad\\q\nx 1\n', 'x 1\n# TYPE x gauge\n', 'x 1\n# HELP x late\n',
            '# TYPE x gauge\n# UNIT x seconds\nx 1\n', 'x 1\n# EOF\n',
            'x 1 # {trace_id="a"} 2\n', 'x {"native":1}\n',
        ]
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(InputError):
                parse(text)

    def test_invalid_family_shapes_and_ambiguity(self):
        invalid = [
            '# TYPE h histogram\nh 1\n', '# TYPE h histogram\nh_bucket 1\n',
            '# TYPE h histogram\nh_bucket{le="NaN"} 1\n',
            '# TYPE h histogram\nh_bucket{le="oops"} 1\n',
            '# TYPE h histogram\nh_sum{le="1"} 1\n',
            '# TYPE s summary\ns 1\n', '# TYPE s summary\ns{quantile="2"} 1\n',
            '# TYPE s summary\ns{quantile="NaN"} 1\n',
            '# TYPE s summary\ns_sum{quantile="0.5"} 1\n',
            '# TYPE h histogram\n# TYPE h_count gauge\nh_count 1\n',
            '# TYPE h histogram\n# TYPE h_bucket histogram\nh_bucket{le="1"} 1\n',
            '# TYPE h histogram\nh_count 1\n# HELP h late\n',
        ]
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(InputError):
                parse(text)


if __name__ == "__main__":
    unittest.main()
