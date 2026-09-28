#!/usr/bin/env python3
"""Read-only owner/CONNMARK inventory; does not load modules or query autoload APIs."""
import datetime
import json
import os
from pathlib import Path
import platform
import re
import sys
import uuid


ROOTS = ('/usr/lib/modules', '/lib/modules', '/usr/syno/lib/modules',
         '/usr/lib/iptables', '/usr/lib/xtables', '/lib/xtables')
SYMBOL = re.compile(r'^(owner_mt(?:_[A-Za-z0-9_]+)?|connmark_(?:mt|tg)(?:_[A-Za-z0-9_]+)?)$')


def read_text(path):
    with path.open(encoding='utf-8') as stream:
        value = stream.read(16777217)
    if len(value) > 16777216:
        raise ValueError('diagnostic file exceeds limit')
    return value


def collect(root=Path('/')):
    def path(name):
        return root / name.lstrip('/')
    report = {'observed_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'kernel': platform.release(), 'architecture': platform.machine(),
              'assessment': 'unknown', 'packet_path_tested': False,
              'registrations': {}, 'module_files': {}, 'loaded': {}, 'config_candidates': {}}
    try:
        values = dict(re.findall(r'^([a-zA-Z0-9_]+)="([^"]*)"', read_text(path('/etc.defaults/VERSION')), re.M))
        report['dsm'] = {'state': 'ok', 'values': {k: values[k] for k in ('majorversion', 'minorversion', 'productversion', 'buildnumber', 'smallfixnumber') if k in values}}
    except (OSError, UnicodeError, ValueError) as error:
        report['dsm'] = {'state': 'unavailable', 'error': type(error).__name__}
    for kind, features in (('matches', ('owner', 'connmark')), ('targets', ('CONNMARK', 'MARK'))):
        try:
            registered = set(read_text(path('/proc/net/ip_tables_' + kind)).splitlines())
            report['registrations'][kind] = {'state': 'ok', **{feature: feature in registered for feature in features}}
        except (OSError, UnicodeError, ValueError) as error:
            report['registrations'][kind] = {'state': 'unavailable', 'error': type(error).__name__}
    symbols = {'state': 'ok', 'owner': [], 'connmark': []}
    try:
        for line in read_text(path('/proc/kallsyms')).splitlines():
            parts = line.split()
            name = parts[2] if len(parts) >= 3 else ''
            if SYMBOL.fullmatch(name):
                symbols['owner' if name.startswith('owner_') else 'connmark'].append(name)
    except (OSError, UnicodeError, ValueError) as error:
        symbols = {'state': 'unavailable', 'error': type(error).__name__}
    report['symbols'] = symbols
    for name in ('xt_owner', 'xt_connmark', 'nf_conntrack', 'nf_conntrack_ipv4', 'x_tables'):
        report['loaded'][name] = path('/sys/module/' + name).is_dir()
    for name in ROOTS:
        entry = {'state': 'ok', 'files': [], 'builtin_matches': []}
        directory = path(name)
        if not directory.is_dir():
            report['module_files'][name] = {'state': 'missing'}
            continue
        errors = []
        count = 0
        for current, directories, files in os.walk(str(directory), followlinks=False, onerror=errors.append):
            count += len(files) + len(directories)
            if count > 20000:
                errors.append(ValueError('scan limit'))
                break
            for filename in files:
                lower = filename.lower()
                if ('owner' not in lower and 'connmark' not in lower and 'config' not in lower
                        and filename not in ('modules.builtin', 'modules.builtin.modinfo')):
                    continue
                full = Path(current, filename)
                entry['files'].append('/' + full.relative_to(root).as_posix())
                if filename == 'modules.builtin':
                    try:
                        entry['builtin_matches'].extend(line for line in read_text(full).splitlines() if re.search(r'/xt_(owner|connmark)\.ko$', line, re.I))
                    except (OSError, UnicodeError, ValueError) as error:
                        errors.append(error)
        if errors:
            entry['state'] = 'incomplete'
            entry['errors'] = sorted({type(error).__name__ for error in errors})
        entry['files'].sort()
        report['module_files'][name] = entry
    for name in ('/proc/config.gz', '/boot/config-' + platform.release(),
                 '/usr/lib/modules/' + platform.release() + '/build/.config'):
        report['config_candidates'][name] = path(name).is_file()
    return report


def main():
    if sys.argv[1:] not in ([], ['--report']):
        sys.exit('Only optional --report is accepted; this command is read-only.')
    report = collect()
    if not sys.argv[1:]:
        print(json.dumps(report, sort_keys=True, indent=2))
        return
    import pwd
    if os.geteuid() != 0:
        sys.exit('The fixed private report requires the existing root console.')
    destination = Path('/volume1/docker/vpn-gateway/.vless-maintenance') / ('netfilter-inventory-' + uuid.uuid4().hex + '.json')
    fd = os.open(str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(report, stream, sort_keys=True, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
        owner = pwd.getpwnam('prometei')
        os.fchown(stream.fileno(), owner.pw_uid, owner.pw_gid)
    print('NETFILTER_INVENTORY=ready')
    print('REPORT=' + str(destination))


if __name__ == '__main__':
    main()
