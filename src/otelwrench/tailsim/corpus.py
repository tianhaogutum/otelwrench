"""Validate and retain a complete OTLP/HTTP JSON ExportTraceServiceRequest."""

import base64
import binascii
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import re

from .validation import (InputError, array, canonical_bytes, finite_number, integer,
                         obj, parse_json, string)

STATUS = {0: 'UNSET', 1: 'OK', 2: 'ERROR'}


def identifier(value, path, length, empty=False):
    string(value, path)
    if empty and value == '':
        return value
    if not re.fullmatch(r'[0-9a-fA-F]{' + str(length) + '}', value) or int(value, 16) == 0:
        raise InputError(f'{path}: expected nonzero {length}-digit hexadecimal ID')
    return value.lower()


def count_fields(value, path):
    for key, v in value.items():
        if key.startswith('dropped') or key == 'flags':
            integer(v, f'{path}.{key}', maximum=2**32 - 1)


def any_value(value, path, depth=0):
    if depth > 64:
        raise InputError(f'{path}: attribute nesting exceeds 64 levels')
    obj(value, path, {'stringValue', 'boolValue', 'intValue', 'doubleValue',
                      'arrayValue', 'kvlistValue', 'bytesValue'})
    if len(value) > 1:
        raise InputError(f'{path}: AnyValue must contain at most one value type')
    for key, v in value.items():
        p = f'{path}.{key}'
        if key == 'stringValue':
            string(v, p)
        elif key == 'boolValue':
            if type(v) is not bool:
                raise InputError(f'{p}: expected boolean')
        elif key == 'intValue':
            integer(v, p, maximum=2**63 - 1, minimum=-(2**63), strings=True)
        elif key == 'doubleValue':
            finite_number(v, p)
        elif key == 'bytesValue':
            string(v, p)
            try:
                base64.b64decode(v, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise InputError(f'{p}: invalid base64 bytesValue') from exc
        elif key == 'arrayValue':
            obj(v, p, {'values'})
            for i, item in enumerate(array(v.get('values', []), p + '.values')):
                any_value(item, f'{p}.values[{i}]', depth + 1)
        else:
            obj(v, p, {'values'})
            attributes(v.get('values', []), p + '.values', depth + 1)


def attributes(values, path, depth=0):
    seen = set()
    result = {}
    for i, entry in enumerate(array(values, path)):
        p = f'{path}[{i}]'
        obj(entry, p, {'key', 'value'}, ('key', 'value'))
        key = string(entry['key'], p + '.key')
        if key in seen:
            raise InputError(f'{p}: duplicate attribute key {key!r}')
        seen.add(key)
        any_value(entry['value'], p + '.value', depth)
        if 'stringValue' in entry['value']:
            result[key] = entry['value']['stringValue']
    return result


def metadata(value, path, scope=False):
    allowed = {'attributes', 'droppedAttributesCount'} | ({'name', 'version'} if scope else set())
    obj(value, path, allowed)
    count_fields(value, path)
    for key in ('name', 'version'):
        if key in value:
            string(value[key], f'{path}.{key}')
    return attributes(value.get('attributes', []), path + '.attributes')


def span(value, path):
    obj(value, path, {'traceId', 'spanId', 'parentSpanId', 'traceState', 'flags',
                      'name', 'kind', 'startTimeUnixNano', 'endTimeUnixNano',
                      'attributes', 'droppedAttributesCount', 'events',
                      'droppedEventsCount', 'links', 'droppedLinksCount', 'status'},
        ('traceId', 'spanId', 'startTimeUnixNano', 'endTimeUnixNano'))
    trace_id = identifier(value['traceId'], path + '.traceId', 32)
    span_id = identifier(value['spanId'], path + '.spanId', 16)
    if 'parentSpanId' in value:
        identifier(value['parentSpanId'], path + '.parentSpanId', 16, empty=True)
    for key in ('name', 'traceState'):
        if key in value:
            string(value[key], f'{path}.{key}')
    if 'kind' in value:
        integer(value['kind'], path + '.kind', maximum=5)
    count_fields(value, path)
    start = integer(value['startTimeUnixNano'], path + '.startTimeUnixNano', strings=True)
    end = integer(value['endTimeUnixNano'], path + '.endTimeUnixNano', strings=True)
    if end < start:
        raise InputError(f'{path}: endTimeUnixNano precedes startTimeUnixNano')
    attrs = attributes(value.get('attributes', []), path + '.attributes')
    status = value.get('status', {})
    obj(status, path + '.status', {'code', 'message'})
    code = integer(status.get('code', 0), path + '.status.code', maximum=2)
    if 'message' in status:
        string(status['message'], path + '.status.message')
    for i, event in enumerate(array(value.get('events', []), path + '.events')):
        p = f'{path}.events[{i}]'
        obj(event, p, {'timeUnixNano', 'name', 'attributes', 'droppedAttributesCount'})
        if 'timeUnixNano' in event:
            integer(event['timeUnixNano'], p + '.timeUnixNano', strings=True)
        if 'name' in event:
            string(event['name'], p + '.name')
        attributes(event.get('attributes', []), p + '.attributes')
        count_fields(event, p)
    for i, link in enumerate(array(value.get('links', []), path + '.links')):
        p = f'{path}.links[{i}]'
        obj(link, p, {'traceId', 'spanId', 'traceState', 'attributes', 'droppedAttributesCount', 'flags'},
            ('traceId', 'spanId'))
        identifier(link['traceId'], p + '.traceId', 32)
        identifier(link['spanId'], p + '.spanId', 16)
        if 'traceState' in link:
            string(link['traceState'], p + '.traceState')
        attributes(link.get('attributes', []), p + '.attributes')
        count_fields(link, p)
    return trace_id, span_id, start, end, STATUS[code], attrs


@dataclass
class Trace:
    trace_id: str
    start: int
    end: int
    span_count: int = 0
    statuses: set = field(default_factory=set)
    string_attributes: dict = field(default_factory=dict)

    @property
    def duration_ns(self):
        return self.end - self.start

    @property
    def error(self):
        return 'ERROR' in self.statuses


@dataclass
class Corpus:
    document: dict
    traces: dict
    source_bytes: int
    source_sha256: str

    def retained_document(self, trace_ids, span_ids=None):
        """Filter traces and optionally (trace ID, span ID) pairs, keeping context."""
        resources = []
        for resource in self.document['resourceSpans']:
            scopes = []
            for scope in resource.get('scopeSpans', []):
                kept = [s for s in scope.get('spans', [])
                        if s['traceId'].lower() in trace_ids and
                        (span_ids is None or (s['traceId'].lower(), s['spanId'].lower()) in span_ids)]
                if kept:
                    scopes.append({**scope, 'spans': kept})
            if scopes:
                resources.append({**resource, 'scopeSpans': scopes})
        return {'resourceSpans': resources}

    def retained_bytes(self, trace_ids):
        return len(canonical_bytes(self.retained_document(trace_ids)))


def parse_corpus(text):
    value = parse_json(text)
    obj(value, 'OTLP request', {'resourceSpans'}, ('resourceSpans',))
    traces, seen = {}, set()
    for ri, resource in enumerate(array(value['resourceSpans'], 'resourceSpans')):
        rp = f'resourceSpans[{ri}]'
        obj(resource, rp, {'resource', 'scopeSpans', 'schemaUrl'})
        res_attrs = metadata(resource.get('resource', {}), rp + '.resource')
        if 'schemaUrl' in resource:
            string(resource['schemaUrl'], rp + '.schemaUrl')
        for si, scope in enumerate(array(resource.get('scopeSpans', []), rp + '.scopeSpans')):
            sp = f'{rp}.scopeSpans[{si}]'
            obj(scope, sp, {'scope', 'spans', 'schemaUrl'})
            metadata(scope.get('scope', {}), sp + '.scope', scope=True)
            if 'schemaUrl' in scope:
                string(scope['schemaUrl'], sp + '.schemaUrl')
            for i, record in enumerate(array(scope.get('spans', []), sp + '.spans')):
                p = f'{sp}.spans[{i}]'
                tid, sid, start, end, code, attrs = span(record, p)
                identity = (tid, sid)
                if identity in seen:
                    raise InputError(f'{p}: duplicate span ({tid}, {sid}); repeated or conflicting copies unsupported')
                seen.add(identity)
                trace = traces.setdefault(tid, Trace(tid, start, end))
                trace.start, trace.end = min(trace.start, start), max(trace.end, end)
                trace.span_count += 1
                trace.statuses.add(code)
                # Resource and span values both participate, including conflicting keys.
                for mapping in (res_attrs, attrs):
                    for key, val in mapping.items():
                        trace.string_attributes.setdefault(key, set()).add(val)
    # Reject lone surrogate strings instead of failing later during output encoding.
    try:
        canonical_bytes(value)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise InputError(f'OTLP request cannot be encoded as finite UTF-8 JSON: {exc}') from exc
    source = text.encode('utf-8')
    return Corpus(value, dict(sorted(traces.items())), len(source), hashlib.sha256(source).hexdigest())


def load_corpus(path):
    return parse_corpus(Path(path).read_bytes().decode('utf-8'))
