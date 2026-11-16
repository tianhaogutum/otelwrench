import copy
import json
from pathlib import Path
import tempfile
import unittest

import yaml

from otelwrench.tailsim.corpus import parse_corpus
from otelwrench.tailsim.policy import parse_config
from otelwrench.tailsim.validation import InputError
from .helpers import attr, document, policies, policy, record


class ConfigValidation(unittest.TestCase):
    def test_unsupported_policies(self):
        for kind in ['unknown', 'probabilistic', 'composite', 'rate_limiting', 'numeric_attribute', 'ottl_condition']:
            with self.subTest(kind=kind), self.assertRaises(InputError):
                policies(policy(kind))

    def test_unsupported_fields(self):
        for key, value in [('enabled_regex_matching', True), ('enabled_regex_matching', False),
                           ('invert_match', True), ('cache_max_size', 10), ('regex', 'a.*')]:
            with self.subTest(key=key, value=value), self.assertRaises(InputError):
                policies(policy('string_attribute', {'key': 'x', 'values': ['a'], key: value}))
        for text in ['receivers: {}', 'processors: {}', 'processors: {tail_sampling: {}}',
                     'processors: {tail_sampling: {policies: []}}', '', '[]',
                     'processors: {tail_sampling: {policies: null}}']:
            with self.subTest(text=text), self.assertRaises(InputError):
                parse_config(text)

    def test_strict_config_tree(self):
        root = {'processors': {'tail_sampling': {'policies': [policy('always_sample')]}}}
        mutations = [lambda d: d.update(service={}),
                     lambda d: d['processors'].update(batch={}),
                     lambda d: d['processors']['tail_sampling'].update(decision_wait='10s'),
                     lambda d: d['processors']['tail_sampling'].update(sample_on_first_match=True)]
        for mutate in mutations:
            value = copy.deepcopy(root)
            mutate(value)
            with self.subTest(value=value), self.assertRaises(InputError):
                parse_config(yaml.safe_dump(value))

    def test_malformed_policy_arguments(self):
        cases = [policy('latency', {'threshold_ms': -1}), policy('latency', {'threshold_ms': 0.5}),
                 policy('latency', {'threshold_ms': True}), policy('latency', {'threshold_ms': 1, 'upper_threshold_ms': 1}),
                 policy('status_code', {'status_codes': ['error']}), policy('status_code', {'status_codes': []}),
                 policy('string_attribute', {'key': 'x', 'values': [1]}),
                 policy('always_sample', {'unexpected': 1}), policy('and', {'and_sub_policy': []}),
                 policy('not', {'not_sub_policy': [policy('always_sample')]}),
                 policy('not', {'not_sub_policy': policy('drop', {'drop_sub_policy': [policy('always_sample')]})}),
                 policy('and', {'and_sub_policy': [policy('and', {'and_sub_policy': [policy('always_sample')]})]})]
        for item in cases:
            with self.subTest(item=item), self.assertRaises(InputError):
                policies(item)

    def test_duplicate_names_and_yaml_keys(self):
        with self.assertRaises(InputError):
            policies(policy('always_sample'), policy('always_sample'))
        for text in ['processors: {}\nprocessors: {}', '1: bad',
                     'processors: &x {tail_sampling: *x}', '!!python/object/apply:os.system ["echo bad"]']:
            with self.subTest(text=text), self.assertRaises(InputError):
                parse_config(text)


class CorpusValidation(unittest.TestCase):
    def assert_bad_span(self, key, value):
        s = record()
        s[key] = value
        with self.assertRaises(InputError):
            parse_corpus(json.dumps(document([s])))

    def test_ids_invalid_partial_zero_and_base64(self):
        for key, values in [('traceId', ['a', '0'*32, 'g'*32, 'AQEBAQEBAQEBAQEBAQEBAQ==', None]),
                            ('spanId', ['a', '0'*16, 'g'*16]), ('parentSpanId', ['a', '0'*16])]:
            for value in values:
                with self.subTest(key=key, value=value):
                    self.assert_bad_span(key, value)

    def test_reversed_invalid_and_missing_timestamps(self):
        for value in ['-1', -1, 1.5, True, 'abc', str(2**64), None]:
            with self.subTest(value=value):
                self.assert_bad_span('startTimeUnixNano', value)
        self.assert_bad_span('endTimeUnixNano', '1')
        s = record()
        del s['endTimeUnixNano']
        with self.assertRaises(InputError):
            parse_corpus(json.dumps(document([s])))

    def test_duplicate_spans_rejected_even_identical_or_different_context(self):
        for second in [record(), record(status=2)]:
            with self.subTest(second=second), self.assertRaisesRegex(InputError, 'duplicate span'):
                parse_corpus(json.dumps(document([record(), second])))

    def test_unknown_fields_and_wrong_formats(self):
        for text in ['{}', '[]', '{"resourceSpans":null}', '{"resourceSpans":[],"logs":[]}',
                     '{"resourceSpans":[]}\n{"resourceSpans":[]}', '{"resourceSpans":[],"resourceSpans":[]}',
                     '{"resourceSpans":[{"instrumentationLibrarySpans":[]}]}']:
            with self.subTest(text=text), self.assertRaises(InputError):
                parse_corpus(text)
        self.assert_bad_span('unknown', 'no')
        self.assert_bad_span('status', {'code': 'STATUS_CODE_ERROR'})
        self.assert_bad_span('status', {'code': 3})
        self.assert_bad_span('kind', 6)

    def test_attributes_strict(self):
        cases = [[attr('x', 'one'), attr('x', 'two')],
                 [{'key': 'x', 'value': {'stringValue': 'x', 'intValue': '1'}}],
                 [attr('x', 'notbase64', 'bytesValue')], [attr('x', 'NaN', 'doubleValue')],
                 [attr('x', 1.5, 'intValue')], [attr('x', float('inf'), 'doubleValue')],
                 [attr('x', 1, 'boolValue')], [attr('x', {'bad': []}, 'arrayValue')]]
        for values in cases:
            with self.subTest(values=values):
                self.assert_bad_span('attributes', values)

    def test_valid_metadata_events_links_and_nested_values_are_preserved(self):
        s = record()
        s.update(parentSpanId='', flags=1, traceState='a=b', name='λ', kind=2,
                 droppedAttributesCount=1,
                 events=[{'timeUnixNano': '1000000000', 'name': 'event', 'attributes': [attr('raw', 'AA==', 'bytesValue')]}],
                 links=[{'traceId': 'A'*32, 'spanId': 'B'*16, 'flags': 1, 'attributes': []}])
        s['attributes'] = [attr('array', {'values': [{'intValue': '-42'}, {'boolValue': True}, {}]}, 'arrayValue'),
                           attr('map', {'values': [attr('x', 'v')]}, 'kvlistValue')]
        d = document([s])
        d['resourceSpans'][0]['schemaUrl'] = 'https://example.invalid/schema'
        c = parse_corpus(json.dumps(d))
        self.assertEqual(c.retained_document(set(c.traces)), d)

    def test_case_insensitive_ids_group_across_resource_groups(self):
        a = record(sid=1)
        a['traceId'] = 'A'*32
        b = record(sid=2, status=2)
        b['traceId'] = 'a'*32
        d = document([a])
        d['resourceSpans'].extend(document([b])['resourceSpans'])
        c = parse_corpus(json.dumps(d))
        self.assertEqual(len(c.traces), 1)
        self.assertEqual(c.traces['a'*32].span_count, 2)
        self.assertTrue(c.traces['a'*32].error)

    def test_empty_corpus_and_zero_length_span(self):
        self.assertEqual(parse_corpus('{"resourceSpans":[]}').traces, {})
        c = parse_corpus(json.dumps(document([record(start=0, duration=0)])))
        self.assertEqual(next(iter(c.traces.values())).duration_ns, 0)

    def test_extreme_numbers_and_unicode_fail_cleanly(self):
        for number in [10**400, float('nan')]:
            with self.subTest(number=str(number)[:20]):
                self.assert_bad_span('attributes', [attr('x', number, 'doubleValue')])
        self.assert_bad_span('name', '\ud800')

    def test_nested_attribute_depth_limit(self):
        value = {'stringValue': 'end'}
        for _ in range(66):
            value = {'arrayValue': {'values': [value]}}
        self.assert_bad_span('attributes', [{'key': 'deep', 'value': value}])

    def test_source_byte_count_includes_crlf_and_unicode(self):
        from otelwrench.tailsim.corpus import load_corpus
        content = json.dumps(document([record(attrs=[attr('x', 'λ')])]),
                             ensure_ascii=False, indent=2).replace('\n', '\r\n').encode()
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'traces.json'
            p.write_bytes(content)
            self.assertEqual(load_corpus(p).source_bytes, len(content))

    def test_invalid_events_links_and_resource_fields(self):
        self.assert_bad_span('events', [{'unknown': 'bad'}])
        self.assert_bad_span('links', [{'traceId': '1'*32, 'spanId': 'bad'}])
        self.assert_bad_span('flags', True)
        self.assert_bad_span('droppedEventsCount', -1)
        value = document([record()])
        value['resourceSpans'][0]['resource']['unknown'] = 'bad'
        with self.assertRaises(InputError):
            parse_corpus(json.dumps(value))
