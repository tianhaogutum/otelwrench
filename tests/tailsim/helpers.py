import json
import yaml

from otelwrench.tailsim.corpus import parse_corpus
from otelwrench.tailsim.policy import parse_config


def record(trace=1, sid=1, start=1_000_000_000, duration=10_000_000, status=1, attrs=None):
    return {'traceId': f'{trace:032x}', 'spanId': f'{sid:016x}',
            'startTimeUnixNano': str(start), 'endTimeUnixNano': str(start + duration),
            'status': {'code': status}, 'attributes': attrs or []}


def attr(key, value, kind='stringValue'):
    return {'key': key, 'value': {kind: value}}


def document(spans, attrs=None):
    return {'resourceSpans': [{'resource': {'attributes': attrs or []},
                               'scopeSpans': [{'spans': spans}]}]}


def corpus(spans, attrs=None):
    return parse_corpus(json.dumps(document(spans, attrs)))


def policy(kind, params=None, name=None):
    result = {'name': name or kind, 'type': kind}
    if params is not None:
        result[kind] = params
    return result


def policies(*items):
    return parse_config(yaml.safe_dump({'processors': {'tail_sampling': {'policies': list(items)}}}))
