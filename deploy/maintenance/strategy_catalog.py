"""Closed, versioned anti-DPI controls. No client-supplied commands or targets."""

from dataclasses import dataclass
import hashlib
import json
import re

SERVICES = {
    'youtube': ('YouTube', 'www.youtube.com'),
    'discord': ('Discord', 'discord.com'),
    'telegram': ('Telegram Web', 'web.telegram.org'),
    'instagram': ('Instagram', 'www.instagram.com'),
}
STRATEGIES = ('tlsrec-sni', 'disorder-1', 'disorder-sni', 'split-1',
              'split-sni', 'oob-sni', 'disoob-sni', 'fake-md5')
INTERVALS = (5, 15, 30, 60)
CATALOG_VERSION = 'byedpi-https-v1'
CATALOG_ID = hashlib.sha256(json.dumps([CATALOG_VERSION, SERVICES, STRATEGIES],
    sort_keys=True).encode('utf-8')).hexdigest()
DIGEST = re.compile(r'[a-f0-9]{64}\Z')
ACTIONS = ('strategy_check', 'strategy_tune', 'strategy_configure', 'strategy_apply', 'strategy_rollback')


def digest(value):
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise ValueError('invalid_revision')
    return value


def service(value):
    if not isinstance(value, str) or value not in SERVICES:
        raise ValueError('invalid_service')
    return value


def strategy(value):
    if not isinstance(value, str) or value not in STRATEGIES:
        raise ValueError('invalid_strategy')
    return value


def settings(value):
    if not isinstance(value, dict) or set(value) != {'enabled', 'mode', 'interval_minutes', 'daily_enabled'}:
        raise ValueError('invalid_settings')
    if type(value['enabled']) is not bool or type(value['daily_enabled']) is not bool:
        raise ValueError('invalid_boolean')
    if value['mode'] not in ('auto', 'pinned') or type(value['interval_minutes']) is not int or value['interval_minutes'] not in INTERVALS:
        raise ValueError('invalid_policy')
    return dict(value)


@dataclass(frozen=True)
class StrategyRequest:
    action: str
    service_id: str
    expected_revision: str
    settings: dict


def parse_strategy_request(value):
    if not isinstance(value, dict) or set(value) != {'action', 'service_id', 'expected_revision', 'settings'}:
        raise ValueError('invalid_fields')
    if value['action'] not in ACTIONS or not isinstance(value['settings'], dict):
        raise ValueError('invalid_action')
    action, options = value['action'], value['settings']
    if action == 'strategy_configure':
        options = settings(options)
    elif action == 'strategy_apply':
        if set(options) != {'strategy_id', 'mode'} or options['mode'] not in ('auto', 'pinned'):
            raise ValueError('invalid_settings')
        strategy(options['strategy_id'])
    elif options:
        raise ValueError('invalid_settings')
    return StrategyRequest(action, service(value['service_id']), digest(value['expected_revision']), dict(options))


def catalog():
    return {'version': CATALOG_VERSION, 'catalog_id': CATALOG_ID,
            'strategies': list(STRATEGIES), 'intervals': list(INTERVALS),
            'daily_time': '05:30', 'timezone': 'Asia/Yekaterinburg',
            'scope': 'https_tcp_443_only', 'freshness_seconds': 180,
            'failure_threshold': 3, 'failure_spacing_seconds': 30,
            'candidate_successes': 3, 'candidate_spacing_seconds': 10,
            'cooldown_seconds': 900, 'max_changes_per_hour': 2}
