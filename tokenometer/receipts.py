"""Validate private source-counter evidence independently of the adapters."""
from __future__ import annotations
import re
from .models import UsageEvent, utc_timestamp

CODEX_FIELDS = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
                'output_tokens', 'reasoning_output_tokens', 'total_tokens')
AGY_FIELDS = ('input', 'cache_creation', 'cache_read', 'output_total', 'visible_output', 'reasoning')
ALLOWED = {'schema', 'source_refs', 'counters', 'baseline', 'current', 'last',
           'observations', 'source_timestamps', 'timestamp_basis', 'model_enum_ids', 'provider_enum_ids', 'field_presence'}


def vector(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError('Invalid source counter vector')
    if any(v is not None and (type(v) is not int or v < 0) for v in value.values()):
        raise ValueError('Source counters require nonnegative integers or null')
    return value


def validate_receipt(event: UsageEvent):
    receipt = event.counter_receipt
    if not isinstance(receipt, dict) or set(receipt) - ALLOWED:
        raise ValueError('Invalid source evidence fields')
    refs = receipt.get('source_refs')
    if not isinstance(refs, list) or not refs:
        raise ValueError('Missing source evidence references')
    for ref in refs:
        if not isinstance(ref, dict) or set(ref) != {'source', 'kind', 'index', 'retry'}:
            raise ValueError('Invalid source reference')
        if not re.fullmatch(r'[a-f0-9]{64}', ref['source']) or ref['kind'] not in ('jsonl', 'steps', 'gen_metadata'):
            raise ValueError('Unsafe source identity')
        if any(type(ref[k]) is not int or ref[k] < 0 for k in ('index', 'retry')):
            raise ValueError('Invalid source position')
    if 'timestamp_basis' in receipt and receipt['timestamp_basis'] not in ('record', 'legacy_snapshot', 'merged_source_metadata', 'unknown'):
        raise ValueError('Invalid timestamp provenance')
    for key in ('model_enum_ids', 'provider_enum_ids'):
        if key in receipt and (not isinstance(receipt[key], list) or any(type(v) is not int or v < 0 for v in receipt[key])):
            raise ValueError('Invalid enum evidence')
    if 'field_presence' in receipt:
        presence = receipt['field_presence']
        if not isinstance(presence, dict) or any(k not in set(CODEX_FIELDS + AGY_FIELDS) or type(v) not in (bool, int) or v < 0 for k, v in presence.items()):
            raise ValueError('Invalid field-presence evidence')
    schema = receipt.get('schema')
    if event.platform == 'codex':
        if schema not in ('codex.response.v1', 'codex.legacy_delta.v1'):
            raise ValueError('Invalid Codex receipt schema')
        counters = vector(receipt.get('counters'), CODEX_FIELDS)
        required = CODEX_FIELDS[:4]
        if any(counters[k] is None for k in required):
            raise ValueError('Incomplete Codex counter evidence')
        if schema == 'codex.legacy_delta.v1':
            current = vector(receipt.get('current'), CODEX_FIELDS)
            last = vector(receipt.get('last'), CODEX_FIELDS)
            baseline = receipt.get('baseline')
            if baseline is not None: vector(baseline, CODEX_FIELDS)
            if any(v is None for values in (current, last, counters) for v in values.values()):
                raise ValueError('Incomplete legacy counters')
            if baseline is not None and any(v is None for v in baseline.values()):
                raise ValueError('Incomplete legacy baseline')
            delta = {k: current[k] - (baseline[k] if baseline is not None else 0) for k in CODEX_FIELDS}
            if delta != counters or delta != last:
                raise ValueError('Legacy delta differs from source snapshots')
        fresh = counters['input_tokens'] - counters['cached_input_tokens'] - counters['cache_write_input_tokens']
        expected = (fresh, counters['cached_input_tokens'], counters['cache_write_input_tokens'], counters['output_tokens'], counters['reasoning_output_tokens'])
        if counters['total_tokens'] is not None and counters['total_tokens'] != counters['input_tokens'] + counters['output_tokens']:
            raise ValueError('Source total differs from input plus output')
    elif schema == 'antigravity.protobuf.v1':
        observations = receipt.get('observations')
        if not isinstance(observations, list) or not observations:
            raise ValueError('Missing protobuf observations')
        normalized = []
        for obs in observations:
            vector(obs, AGY_FIELDS)
            output = max(obs['output_total'] or 0, (obs['visible_output'] or 0) + (obs['reasoning'] or 0))
            visible = max(obs['visible_output'] or 0, output - (obs['reasoning'] or 0))
            reasoning = max(obs['reasoning'] or 0, output - visible)
            normalized.append((obs['input'] or 0, obs['cache_read'] or 0, obs['cache_creation'] or 0, output, reasoning))
        expected = tuple(max(values) for values in zip(*normalized))
        if 'source_timestamps' not in receipt:
            raise ValueError('Missing per-observation timestamp evidence')
        if 'source_timestamps' in receipt:
            timestamps = receipt['source_timestamps']
            if not isinstance(timestamps, list) or len(timestamps) != len(observations) or len(refs) != len(observations) or any(v is not None and (not isinstance(v, str) or utc_timestamp(v) != v) for v in timestamps):
                raise ValueError('Invalid raw source timestamp evidence')
            known = {v for v in timestamps if v is not None}
            expected_time = next(iter(known)) if len(known) == 1 else None
            if event.timestamp != expected_time:
                raise ValueError('Event date differs from source timestamps')
    else:
        raise ValueError('Invalid Antigravity receipt schema')
    if expected != (event.fresh_input, event.cache_read, event.cache_write, event.output, event.reasoning):
        raise ValueError('Normalized event differs from source counter evidence')
