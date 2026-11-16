import unittest

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, Summary, generate_latest

from otelwrench.promdiff.parser import InputError, parse_snapshot


COMPOSITES = r'''# HELP requests_total Completed\nrequests with \\ paths.
# TYPE requests_total counter
requests_total{method="get",path="C:\\tmp\n\"quoted\""} 10 123
# HELP latency_seconds Latency.
# TYPE latency_seconds histogram
latency_seconds_bucket{method="get",le="0.5"} 1
latency_seconds_bucket{method="get",le="+Inf"} 2
latency_seconds_sum{method="get"} 0.7
latency_seconds_count{method="get"} 2
# TYPE rpc_seconds summary
rpc_seconds{method="post",quantile="0.5"} 0.1
rpc_seconds{method="post",quantile="0.99"} 0.2
rpc_seconds_sum{method="post"} 0.3
rpc_seconds_count{method="post"} 2
'''


class ParserTests(unittest.TestCase):
    def assert_invalid(self, text, message=None):
        with self.assertRaises(InputError) as context:
            parse_snapshot(text, "fixture.prom")
        self.assertIn("fixture.prom", str(context.exception))
        if message:
            self.assertIn(message, str(context.exception))

    def test_counter_histogram_summary_and_escapes(self):
        snapshot = parse_snapshot(COMPOSITES)
        self.assertEqual(set(snapshot.families), {"requests_total", "latency_seconds", "rpc_seconds"})
        self.assertEqual(len(snapshot.series), 9)
        counter = snapshot.families["requests_total"]
        self.assertEqual(counter.help, "Completed\nrequests with \\ paths.")
        self.assertEqual(counter.type, "counter")
        self.assertIsNone(counter.unit)
        self.assertEqual(counter.schemas, {"requests_total": ("method", "path")})
        identity = next(iter(counter.series))
        self.assertEqual(dict(identity[1])["path"], 'C:\\tmp\n"quoted"')
        self.assertEqual(snapshot.families["latency_seconds"].schemas, {
            "latency_seconds_bucket": ("le", "method"),
            "latency_seconds_sum": ("method",), "latency_seconds_count": ("method",),
        })
        self.assertEqual(snapshot.families["rpc_seconds"].schemas["rpc_seconds"], ("method", "quantile"))

    def test_real_client_generated_exposition(self):
        registry = CollectorRegistry()
        Gauge("jobs", "Jobs.", ["queue"], registry=registry).labels("a").set(4)
        Counter("completed", "Completed.", registry=registry).inc(2)
        Histogram("duration", "Duration.", buckets=[0.1, 1], registry=registry).observe(0.3)
        Summary("payload", "Payload.", registry=registry).observe(10)
        snapshot = parse_snapshot(generate_latest(registry).decode("utf-8"))
        self.assertEqual(set(snapshot.families), {
            "jobs", "completed_total", "completed_created", "duration", "duration_created", "payload", "payload_created",
        })
        self.assertEqual(len(snapshot.families["duration"].series), 5)
        self.assertEqual(len(snapshot.families["payload"].series), 2)
        self.assertEqual(len(snapshot.series), 12)

    def test_counter_wire_names_not_invented(self):
        for name in ["requests", "requests_total", "requests_total_total"]:
            with self.subTest(name=name):
                snap = parse_snapshot(f"# TYPE {name} counter\n{name} 2\n")
                self.assertEqual(set(snap.families), {name})
                self.assertEqual(snap.families[name].schemas, {name: ()})
                self.assertEqual(snap.series, {(name, ())})

    def test_untyped_suffixes_are_not_guessed(self):
        snapshot = parse_snapshot("h_bucket{le=\"+Inf\"} 1\nh_sum 1\nh_count 1\n")
        self.assertEqual(set(snapshot.families), {"h_bucket", "h_sum", "h_count"})
        self.assertTrue(all(f.type == "untyped" for f in snapshot.families.values()))

    def test_empty_and_metadata_only_preserved(self):
        self.assertEqual(parse_snapshot("").summary(), {"families": 0, "observed_series": 0})
        snapshot = parse_snapshot("# comment\n# HELP h A histogram.\n# TYPE h histogram\n")
        self.assertEqual(snapshot.families["h"].contract(), {
            "type": "histogram", "help": "A histogram.", "unit": None, "samples": [],
        })

    def test_help_only_and_empty_help(self):
        snapshot = parse_snapshot("# HELP x\nx 1\n# HELP y\n# TYPE y gauge\ny 1\n")
        self.assertEqual(snapshot.families["x"].help, "")
        self.assertEqual(snapshot.families["y"].help, "")
        self.assertEqual(snapshot.families["x"].type, "untyped")

    def test_whitespace_trailing_comma_and_unicode_values(self):
        snapshot = parse_snapshot(' \t# TYPE x gauge\n  x { a = "队列", } \t1\t-5 \n')
        self.assertEqual(snapshot.series, {("x", (("a", "队列"),))})

    def test_numeric_values_and_timestamps(self):
        for value in ["1", "-1.5", "+.5", "1e-3", ".12E+4", "NaN", "+Inf", "-Inf", "Inf"]:
            with self.subTest(value=value):
                self.assertEqual(len(parse_snapshot(f"x {value} -9223372036854775808\n").series), 1)
        for timestamp in ["9223372036854775808", "-9223372036854775809", "1.1", "NaN"]:
            self.assert_invalid(f"x 1 {timestamp}\n")

    def test_conflicting_or_duplicate_metadata(self):
        for text in [
            "# TYPE x gauge\n# TYPE x counter\nx 1\n",
            "# TYPE x gauge\n# TYPE x gauge\nx 1\n",
            "# HELP x One\n# HELP x Two\nx 1\n",
        ]:
            self.assert_invalid(text, "duplicate/conflicting")

    def test_late_or_noncontiguous_metadata(self):
        for text in [
            "x 1\n# TYPE x gauge\n",
            "x 1\n# HELP x Late\n",
            "# TYPE h histogram\nh_bucket{le=\"+Inf\"} 1\n# HELP h Late\nh_count 1\nh_sum 1\n",
            'x{a="1"} 1\ny 1\nx{a="2"} 1\n',
            "# HELP x X\n# TYPE y gauge\n# TYPE x gauge\n",
        ]:
            self.assert_invalid(text)

    def test_duplicate_series_regardless_label_order_timestamp_value(self):
        self.assert_invalid('x{a="1",b="2"} 1 1\nx{b="2",a="1"} 9 2\n', "duplicate series")
        self.assert_invalid('x{a="1",a="2"} 1\n')

    def test_malformed_label_and_help_escapes(self):
        for text in [r'x{a="bad\t"} 1', r'x{a="bad\q"} 1', r'x{a="bad\"} 1',
                     r'# HELP x bad\t', r'# HELP x bad\"', 'x{a="line\nfeed"} 1']:
            self.assert_invalid(text + "\n")

    def test_unsupported_openmetrics(self):
        for text in ["# EOF\n", "# UNIT x seconds\nx 1\n", "# CREATED x 3\n",
                     "# TYPE x info\nx_info 1\n", "# TYPE x stateset\nx 1\n",
                     "# TYPE x gaugehistogram\nx 1\n", 'x 1 # {trace_id="a"} 2\n',
                     'x{a="b"} 1 # {trace_id="a"} 2\n']:
            self.assert_invalid(text)

    def test_invalid_or_unsupported_grammar(self):
        for text in [
            "x 1", "x 1\r\n", "\ufeffx 1\n", "x 1\x00\n", 'x{a="b"} 1 2 3\n',
            'x{a="b"}1\n', 'x{a=b} 1\n', 'x{a="b",,} 1\n', 'x{,a="b"} 1\n',
            'x{a="b" b="c"} 1\n', 'x{a="b"} garbage\n', 'x 1_000\n', 'x nan\n',
            'x{__name__="y"} 1\n', 'x{__reserved="y"} 1\n', 'x{a:b="y"} 1\n',
            '{"utf.metric",a="b"} 1\n', 'x{"utf.label"="y"} 1\n',
            '# TYPE x gauge extra\nx 1\n', '# TYPE "x" gauge\nx 1\n', '# TYPE x\n',
        ]:
            with self.subTest(text=text):
                self.assert_invalid(text)

    def test_inconsistent_family_schemas(self):
        for text in ['x{a="1"} 1\nx{b="2"} 2\n',
                     '# TYPE h histogram\nh_bucket{a="b",le="+Inf"} 1\nh_sum{b="a"} 1\nh_count{a="b"} 1\n']:
            self.assert_invalid(text, "inconsistent")

    def test_composite_structure_errors(self):
        for text in [
            '# TYPE h histogram\nh 1\n',
            '# TYPE h histogram\nh_bucket 1\nh_sum 1\nh_count 1\n',
            '# TYPE h histogram\nh_bucket{le="1"} 1\nh_sum 1\nh_count 1\n',
            '# TYPE h histogram\nh_bucket{le="+Inf"} 1\nh_count 1\n',
            '# TYPE h histogram\nh_bucket{le="bad"} 1\nh_sum 1\nh_count 1\n',
            '# TYPE h histogram\nh_bucket{le="NaN"} 1\nh_sum 1\nh_count 1\n',
            '# TYPE s summary\ns 1\ns_sum 1\ns_count 1\n',
            '# TYPE s summary\ns{quantile="1.5"} 1\ns_sum 1\ns_count 1\n',
            '# TYPE s summary\ns{quantile="0.5",a="b"} 1\ns_sum{a="c"} 1\ns_count{a="c"} 1\n',
            '# TYPE s summary\ns_sum{quantile="0.5"} 1\ns_count{quantile="0.5"} 1\n',
        ]:
            with self.subTest(text=text):
                self.assert_invalid(text)

    def test_conflicting_composite_component_ownership(self):
        for text in [
            '# TYPE h histogram\n# TYPE h_bucket gauge\nh_bucket 1\n',
            '# HELP h_count Count\n# TYPE h histogram\n',
            '# TYPE s summary\n# TYPE s_sum gauge\n',
        ]:
            self.assert_invalid(text)

    def test_metadata_help_not_leaked_between_families(self):
        snapshot = parse_snapshot('# HELP x X help\nx 1\ny 1\n# TYPE z gauge\nz 1\n')
        self.assertIsNone(snapshot.families["y"].help)
        self.assertIsNone(snapshot.families["z"].help)
        self.assertEqual(snapshot.families["x"].help, "X help")


if __name__ == "__main__":
    unittest.main()
