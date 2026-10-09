"""Closed profile operations: opaque references only, never credentials."""
from dataclasses import dataclass
import math
import re

TARGETS = ('WG-IMP', 'HY2-USA')
PROBES = {'cloudflare': ('https://cp.cloudflare.com/generate_204', 204),
          'google': ('https://www.google.com/generate_204', 204),
          # Shared VPN exits routinely exhaust the unauthenticated API quota.
          'github': ('https://github.com/robots.txt', 200)}
ACTIONS = ('profile_check', 'profile_apply')
REASONS = ('timeout', 'tls_failed', 'connect_failed', 'http_status', 'invalid_response', 'probe_failed')


def identifier(value, size=32):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{%d}' % size, value):
        raise ValueError('invalid_reference')
    return value


def timestamp(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError('invalid_timestamp')
    return value


@dataclass(frozen=True)
class ProfileRequest:
    action: str
    draft_id: str
    expected_revision: str


def parse_profile_request(value):
    if not isinstance(value, dict) or set(value) != {'action', 'draft_id', 'expected_revision'} or value['action'] not in ACTIONS:
        raise ValueError('invalid_profile_request')
    return ProfileRequest(value['action'], identifier(value['draft_id']), identifier(value['expected_revision'], 64))


def observation(value):
    if not isinstance(value, dict) or set(value) != {'target','ok','latency_ms','http_status','reason','checked_at'}:
        raise ValueError('invalid_probe')
    if value['target'] not in PROBES or type(value['ok']) is not bool:
        raise ValueError('invalid_probe')
    timestamp(value['checked_at'])
    latency, status = value['latency_ms'], value['http_status']
    if latency is not None and (type(latency) is not int or not 0 <= latency <= 30000):
        raise ValueError('invalid_probe')
    if status is not None and (type(status) is not int or not 100 <= status <= 599):
        raise ValueError('invalid_probe')
    if value['reason'] is not None and value['reason'] not in REASONS:
        raise ValueError('invalid_probe')
    if value['ok'] and (latency is None or status != PROBES[value['target']][1] or value['reason'] is not None):
        raise ValueError('invalid_probe')
    if not value['ok'] and value['reason'] is None:
        raise ValueError('invalid_probe')
    return dict(value)


def acceptable(rows, rounds=3, successes=2):
    if not isinstance(rows, list) or len(rows) != rounds*len(PROBES): return False
    values = [observation(row) for row in rows]
    return all(sum(row['target']==target for row in values)==rounds and
               sum(row['target']==target and row['ok'] for row in values)>=successes for target in PROBES)
