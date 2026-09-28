#!/usr/bin/env python3
"""Read-only host metadata. No queue bind, module load or firewall commands."""

import datetime
import gzip
import json
import os
import pathlib
import platform
import re
import subprocess
import sys


FEATURES = {
    'nfnetlink': ('CONFIG_NETFILTER_NETLINK', 'nfnetlink'),
    'nfqueue': ('CONFIG_NETFILTER_NETLINK_QUEUE', 'nfnetlink_queue'),
    'nfqueue_target': ('CONFIG_NETFILTER_XT_TARGET_NFQUEUE', 'xt_NFQUEUE'),
    'conntrack': ('CONFIG_NF_CONNTRACK', 'nf_conntrack'),
    'conntrack_ipv4': ('CONFIG_NF_CONNTRACK_IPV4', 'nf_conntrack_ipv4'),
    'conntrack_marks': ('CONFIG_NF_CONNTRACK_MARK', ''),
    'connmark': ('CONFIG_NETFILTER_XT_CONNMARK', 'xt_connmark'),
    'owner_match': ('CONFIG_NETFILTER_XT_MATCH_OWNER', 'xt_owner'),
    'iptables_ipv4': ('CONFIG_IP_NF_IPTABLES', 'ip_tables'),
    'mangle_ipv4': ('CONFIG_IP_NF_MANGLE', 'iptable_mangle'),
}
DOCKER_PATHS = ('/usr/local/bin/docker', '/var/packages/ContainerManager/target/usr/bin/docker', '/usr/bin/docker')
OPTIONS = {option for option, _ in FEATURES.values()} | {'CONFIG_NET_NS'}
KERNEL_RE = re.compile(r'^[A-Za-z0-9._+\-]{1,128}$')


def classify_support(evidence):
    evidence = evidence if isinstance(evidence, dict) else {}
    kernel, arch = evidence.get('kernel'), evidence.get('architecture')
    result = {'state': 'unknown', 'architecture': arch, 'kernel': kernel, 'reasons': []}
    reasons = result['reasons']
    if not isinstance(kernel, str) or not KERNEL_RE.fullmatch(kernel) or arch not in ('x86_64', 'amd64'):
        reasons.append('host_metadata_incomplete_or_unapproved_platform')
        return result
    docker = evidence.get('docker') or {}
    if not isinstance(docker, dict) or docker.get('state') != 'ok':
        reasons.append('docker_metadata_unavailable')
    elif docker.get('kernel') != kernel or docker.get('architecture') not in ('x86_64', 'amd64'):
        reasons.append('docker_host_mismatch')
    if evidence.get('errors'):
        reasons.append('probe_incomplete')
    if evidence.get('net_namespace') is not True:
        reasons.append('network_namespace_unconfirmed')
    config = evidence.get('config') or {}
    modules = evidence.get('modules') or {}
    if not isinstance(config, dict) or not isinstance(modules, dict):
        reasons.append('invalid_feature_metadata')
        return result
    absent, uncertain = [], []
    for feature, (option, module_name) in FEATURES.items():
        metadata = modules.get(module_name) or {}
        metadata = metadata if isinstance(metadata, dict) else {}
        version = metadata.get('vermagic')
        matched_module = isinstance(version, str) and bool(version.split()) and version.split()[0] == kernel
        available = metadata.get('loaded') is True or matched_module
        setting = config.get(option)
        if setting == 'n':
            (uncertain if available else absent).append(feature)
        elif setting != 'y' and not available:
            uncertain.append(feature)
    # Any incomplete probe or contradictory evidence takes precedence over a guess.
    if reasons or uncertain:
        reasons.extend('unconfirmed:' + name for name in uncertain)
    elif absent:
        result['state'] = 'unsupported'
        reasons.extend('disabled_in_running_kernel_config:' + name for name in absent)
    else:
        result['state'] = 'candidate'
        reasons.append('isolated_queue_bind_not_tested')
    return result


def parse_config(text):
    result = {}
    for line in text.splitlines():
        match = re.fullmatch(r'(CONFIG_[A-Z0-9_]+)=([ymn])', line.strip())
        disabled = re.fullmatch(r'# (CONFIG_[A-Z0-9_]+) is not set', line.strip())
        if match and match.group(1) in OPTIONS:
            result[match.group(1)] = match.group(2)
        elif disabled and disabled.group(1) in OPTIONS:
            result[disabled.group(1)] = 'n'
    return result


def command_json(argv):
    # Only this fixed, read-only Docker template is accepted. No caller commands.
    if argv[0] not in DOCKER_PATHS or argv[1:] != ['info', '--format', DOCKER_FORMAT]:
        return None
    # Docker stderr is suppressed: permission errors and daemon details are not logs.
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env={'PATH': '/usr/bin:/bin:/usr/local/bin'}, shell=False)
        output, _ = process.communicate(timeout=10)
        if process.returncode or len(output) > 8192:
            return None
        value = json.loads(output.decode('utf-8'))
        return value if isinstance(value, dict) else None
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
    except (OSError, UnicodeError, ValueError):
        pass
    return None


DOCKER_FORMAT = '{"ServerVersion":{{json .ServerVersion}},"KernelVersion":{{json .KernelVersion}},"Architecture":{{json .Architecture}},"OSType":{{json .OSType}}}'


def collect_evidence():
    kernel = platform.release()
    evidence = {
        'observed_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'architecture': platform.machine(), 'kernel': kernel,
        'python': platform.python_version(), 'read_only': True,
        'config': {}, 'config_source': 'unavailable', 'modules': {},
        'docker': {'state': 'unavailable'}, 'net_namespace': pathlib.Path('/proc/self/ns/net').exists(),
        'errors': [],
    }
    if not KERNEL_RE.fullmatch(kernel):
        evidence['errors'].append('invalid_kernel_metadata')
        return evidence
    for name in ('/proc/config.gz', '/boot/config-' + kernel):
        try:
            opener = gzip.open if name.endswith('.gz') else open
            with opener(name, 'rb') as source:
                data = source.read(2097153)
            if len(data) > 2097152:
                continue
            evidence['config'] = parse_config(data.decode('ascii'))
            evidence['config_source'] = name
            break
        except (OSError, UnicodeError, EOFError):
            continue
    for _, module_name in FEATURES.values():
        if not module_name:
            continue
        metadata = {'loaded': pathlib.Path('/sys/module', module_name).is_dir(), 'file_present': False, 'vermagic': None}
        for root in ('/lib/modules', '/usr/lib/modules'):
            candidates = [pathlib.Path(root, module_name + '.ko')]
            for subdir in ('net/netfilter', 'net/ipv4/netfilter'):
                candidates.append(pathlib.Path(root, kernel, 'kernel', subdir, module_name + '.ko'))
            for path in candidates:
                try:
                    if not path.is_file():
                        continue
                    metadata['file_present'] = True
                    with path.open('rb') as source:
                        data = source.read(8388609)
                    if len(data) > 8388608 or not data.startswith(b'\x7fELF'):
                        continue
                    found = re.search(rb'(?:\x00)vermagic=([^\x00]{1,256})\x00', data)
                    if found:
                        metadata['vermagic'] = found.group(1).decode('ascii')
                        break
                except (OSError, UnicodeError):
                    continue
            if metadata['vermagic']:
                break
        evidence['modules'][module_name] = metadata
    for binary in DOCKER_PATHS:
        info = command_json([binary, 'info', '--format', DOCKER_FORMAT])
        if info and info.get('OSType') == 'linux':
            evidence['docker'] = {'state': 'ok', 'version': info.get('ServerVersion'), 'kernel': info.get('KernelVersion'), 'architecture': info.get('Architecture')}
            break
    return evidence


if __name__ == '__main__':
    if len(sys.argv) != 1:
        sys.exit('No arguments accepted; this command only observes the current host.')
    observed = collect_evidence()
    print(json.dumps({'evidence': observed, 'assessment': classify_support(observed)}, sort_keys=True, indent=2))
