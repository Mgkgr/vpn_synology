#!/usr/bin/env python3
"""Manual bounded acceptance, never registers a route or connects VPN clients."""
import datetime
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import select
import subprocess
import sys
import tempfile
import time
import uuid

DOCKER = '/usr/local/bin/docker'
STAGING = Path('/volume1/docker/vpn-gateway/.vless-maintenance')
SCOPE = 'antidpi-pinned-acceptance'
SITES = ('www.youtube.com', 'discord.com', 'www.wikipedia.org')
STRATEGY_SITES = SITES + ('web.telegram.org', 'www.instagram.com')
READ_LIMIT, ATTEMPTS = 32768, 3

SOCKS_CLIENT = r'''
import json, socket, time
from runtime import read_secret, receive_exact
def connect(host, port=443):
    peer = socket.create_connection(('127.0.0.1', 1080), timeout=10)
    try:
        password = read_secret().encode('ascii')
        peer.sendall(b'\x05\x01\x02')
        if receive_exact(peer, 2) != b'\x05\x02':
            raise OSError('authentication method rejected')
        peer.sendall(b'\x01\x07gateway' + bytes([len(password)]) + password)
        if receive_exact(peer, 2) != b'\x01\x00':
            raise OSError('authentication rejected')
        try:
            address = b'\x01' + socket.inet_aton(host)
        except OSError:
            name = host.encode('ascii')
            address = b'\x03' + bytes([len(name)]) + name
        peer.sendall(b'\x05\x01\x00' + address + port.to_bytes(2, 'big'))
        header = receive_exact(peer, 4)
        if header[:2] != b'\x05\x00' or header[3] not in (1, 4):
            raise OSError('SOCKS connection rejected')
        receive_exact(peer, 6 if header[3] == 1 else 18)
        return peer
    except Exception:
        peer.close()
        raise
'''

SITE_CLIENT = SOCKS_CLIENT + r'''
import ssl
from runtime import read_dns_pins
pins = read_dns_pins()
results = []
for attempt in range(3):
    for host in ('www.youtube.com', 'discord.com', 'www.wikipedia.org'):
        for mode in ('direct_control', 'byedpi'):
            started = time.monotonic()
            row = {'host': host, 'attempt': attempt + 1, 'mode': mode, 'stage': 'connect', 'ok': False}
            peer = None
            try:
                peer = connect(host) if mode == 'byedpi' else socket.create_connection((pins['hosts'][host][0], 443), timeout=10)
                row['connect_ms'] = round((time.monotonic() - started) * 1000)
                row['stage'] = 'tls'
                peer.settimeout(10)
                peer = ssl.create_default_context().wrap_socket(peer, server_hostname=host)
                row['tls_ms'] = round((time.monotonic() - started) * 1000)
                row['tls_version'] = peer.version()
                row['stage'] = 'http'
                peer.sendall(('GET / HTTP/1.1\r\nHost: ' + host + '\r\nUser-Agent: VPN-Gateway-Acceptance/1.0\r\nAccept: text/html\r\nConnection: close\r\n\r\n').encode('ascii'))
                data = bytearray()
                deadline = time.monotonic() + 10
                while len(data) < 32768 and time.monotonic() < deadline:
                    chunk = peer.recv(min(8192, 32768 - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
                header = bytes(data).split(b'\r\n', 1)[0]
                parts = header.split()
                if len(parts) < 2 or not parts[0].startswith(b'HTTP/'):
                    raise OSError('invalid HTTP response')
                row['status'] = int(parts[1])
                row['received_bytes'] = len(data)
                row['ok'] = 200 <= row['status'] < 400
                row['stage'] = 'complete'
            except Exception as error:
                row['error_type'] = type(error).__name__
            finally:
                if peer is not None:
                    peer.close()
            row['elapsed_ms'] = round((time.monotonic() - started) * 1000)
            results.append(row)
            print(json.dumps(row), flush=True)
            time.sleep(0.2)
'''

FIXTURE = r'''
import json, socket, threading, time
from pathlib import Path
from runtime import receive_exact
def handler(peer):
    with peer:
        peer.settimeout(90)
        try:
            header = receive_exact(peer, 2)
            methods = receive_exact(peer, header[1])
            peer.sendall(b'\x05\x00')
            header = receive_exact(peer, 4)
            if header[3] == 1:
                target = socket.inet_ntoa(receive_exact(peer, 4))
            elif header[3] == 3:
                target = receive_exact(peer, receive_exact(peer, 1)[0]).decode('ascii')
            else:
                return
            port = int.from_bytes(receive_exact(peer, 2), 'big')
            with open('/tmp/targets.jsonl', 'a', encoding='utf-8') as stream:
                stream.write(json.dumps({'atype': header[3], 'target': target, 'port': port}) + '\n')
            if header[3] != 1 or target != '142.250.74.206' or port != 443:
                return
            peer.sendall(b'\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00')
            while True:
                data = peer.recv(65536)
                if not data:
                    return
                peer.sendall(data)
        except OSError:
            return
server = socket.socket()
server.bind(('127.0.0.1', 1081))
server.listen(16)
while True:
    threading.Thread(target=handler, args=(server.accept()[0],), daemon=True).start()
'''

OFFLINE_CLIENT = SOCKS_CLIENT + r'''
for host, port in [('unknown.example',443), ('198.18.0.1',443), ('127.0.0.1',443), ('8.8.8.8',443), ('www.youtube.com',80)]:
    try:
        with connect(host,port) as peer:
            peer.settimeout(1)
            peer.sendall(b'probe')
            if peer.recv(5) == b'probe':
                raise RuntimeError('forbidden target carried data')
    except OSError:
        pass
peer = connect('www.youtube.com')
payload = bytes(range(256)) * 256
for _ in range(4):
    peer.sendall(payload)
    if receive_exact(peer, len(payload)) != payload:
        raise RuntimeError('native hosts did not forward the pinned IPv4')
print(json.dumps({'stage':'stream_ready','bytes':262144,'negative_targets':5}), flush=True)
deadline = time.monotonic() + 70
closed = False
while time.monotonic() < deadline:
    try:
        peer.sendall(b'tick')
        if receive_exact(peer,4) != b'tick':
            closed = True
            break
    except OSError:
        closed = True
        break
    time.sleep(0.2)
peer.close()
print(json.dumps({'stage':'closed','closed':closed}), flush=True)
raise SystemExit(0 if closed else 1)
'''

DNS_QUERY = r'''
import json, urllib.request
from app.settings import Settings
settings = Settings.from_env()
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
result = {}
for host in PROBE_HOSTS:
    request = urllib.request.Request(str(settings.mihomo_url).rstrip('/') + '/dns/query?type=A&name=' + host,
        headers={'Authorization':'Bearer ' + settings.mihomo_api_secret.get_secret_value()})
    with opener.open(request, timeout=12) as response:
        result[host] = json.loads(response.read(32769))
print(json.dumps(result))
'''


def dns_query(hosts):
    if (not isinstance(hosts, tuple) or not 1 <= len(hosts) <= len(STRATEGY_SITES)
            or len(set(hosts)) != len(hosts) or any(host not in STRATEGY_SITES for host in hosts)):
        raise ValueError('invalid_dns_probe_hosts')
    return 'PROBE_HOSTS = ' + repr(hosts) + '\n' + DNS_QUERY


def engine_network_args(network):
    if network not in ('none', 'bridge'):
        raise ValueError('only a new isolated namespace is allowed')
    return ['--network', network]


def answer_ipv4(response):
    answers = response.get('Answer') if isinstance(response, dict) else None
    if not isinstance(answers, list) or not 1 <= len(answers) <= 32:
        raise ValueError('no bounded DNS answer')
    result = set()
    for row in answers:
        if row.get('type') != 1:
            continue
        address = ipaddress.IPv4Address(row['data'])
        if (not address.is_global or address.is_reserved or address.is_multicast
                or address in ipaddress.IPv4Network('192.0.0.0/24')):
            raise ValueError('nonpublic or FakeIP DNS answer')
        result.add(str(address))
    if not 1 <= len(result) <= 16:
        raise ValueError('no real IPv4 DNS answer')
    return sorted(result)


def comparison_hosts(answers, hosts=SITES):
    # Native hosts chooses randomly from a list. Pin exactly the same A record
    # for both legs; different CDN addresses would invalidate the comparison.
    return {host: [answers[host][0]] for host in hosts}


def acceptance_exit(report):
    if report.get('state') != 'success':
        return 1
    if report.get('mode') == 'sites' and report.get('site_acceptance') is not True:
        return 2
    if report.get('mode') in ('strategies', 'confirm'):
        matrix = report.get('matrix', {})
        if matrix.get('stop_reason') != 'completed' or not matrix.get('accepted_candidates'):
            return 2
    return 0


def parse_request(args):
    if (len(args) not in (3, 4) or args[0] != '--approved'
            or not re.fullmatch(r'[a-f0-9]{32}', args[1])):
        raise ValueError('invalid_request')
    if len(args) == 3 and args[2] in ('offline', 'sites'):
        return args[1], args[2], None
    if len(args) == 4 and args[2] in ('strategies', 'confirm') and args[3] in STRATEGY_SITES:
        return args[1], args[2], args[3]
    raise ValueError('invalid_request')


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    build_id, mode, target = parse_request(sys.argv[1:])
    if os.geteuid() != 0:
        raise SystemExit('Existing root console is required.')
    import fcntl
    lock = os.open('/var/run/vpn-antidpi-pinned-acceptance.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    builder, old_probe = load('build-antidpi-staging'), load('probe-antidpi-staging')
    command = builder.command
    run_id = uuid.uuid4().hex
    report = {'run_id': run_id, 'build_id': build_id, 'mode': mode, 'state': 'failed', 'stage': 'preflight',
              'production_ready': False, 'checks': {}, 'cleanup': {}, 'observed_at': datetime.datetime.now(datetime.timezone.utc).isoformat()}
    created, removed, children = [], set(), []
    def snapshot():
        result = {'containers': builder.production_snapshot(), 'network': {}}
        for kind, args in (('firewall4',['/sbin/iptables-save']), ('firewall6',['/sbin/ip6tables-save']),
                           ('routes4',['/sbin/ip','-4','route','show','table','all']), ('routes6',['/sbin/ip','-6','route','show','table','all']),
                           ('rules4',['/sbin/ip','-4','rule','show']), ('rules6',['/sbin/ip','-6','rule','show'])):
            result['network'][kind] = hashlib.sha256(old_probe.normalize_network_state(command(args),kind).encode()).hexdigest()
        result['config_sha256'] = hashlib.sha256(Path('/volume1/docker/vpn-gateway/mihomo/config.yaml').read_bytes()).hexdigest()
        return result
    def identity(container):
        labels = json.loads(command([DOCKER,'inspect','--format','{{json .Config.Labels}}',container]))
        if labels.get('vpn.dashboard.scope') != SCOPE or labels.get('vpn.dashboard.run') != run_id:
            raise ValueError('test_identity_changed')
    before = snapshot()
    report['before'] = before
    with tempfile.TemporaryDirectory(prefix='vpn-antidpi-pins-', dir='/var/tmp') as temporary:
        root = Path(temporary)
        secret, pins_path = root / 'socks_password', root / 'dns-pins.json'
        def private_file(path, content):
            # Rewrite the same test inode intentionally so a mounted lease drift is visible.
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd,'w',encoding='utf-8') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
                os.fchown(stream.fileno(),10002,10002)
        def contract(hosts, seconds):
            now = time.time()
            return {'version':2,'created_at':now,'expires_at':now+seconds,'hosts':hosts,'source_revision':before['config_sha256']}
        def create(role, image, owner=None, *, code=None):
            args = old_probe.create_args(run_id,role,image,owner)
            args[args.index('vpn.dashboard.scope=antidpi-offline-probe')] = 'vpn.dashboard.scope=' + SCOPE
            if role == 'engine':
                args[args.index('--network')+1] = engine_network_args('none' if mode == 'offline' else 'bridge')[1]
            extras = ['--mount','type=bind,src='+str(secret)+',dst=/run/secrets/socks_password,readonly',
                      '--mount','type=bind,src='+str(pins_path)+',dst=/run/config/dns-pins.json,readonly']
            tail = []
            if code is not None:
                extras += ['--entrypoint','python3']
                tail = ['-B','-u','-c',code]
            container = command(args[:-1]+extras+args[-1:]+tail).strip()
            if not re.fullmatch(r'[a-f0-9]{64}',container):
                raise ValueError('invalid_test_container_id')
            created.append(container)
            identity(container)
            command([DOCKER,'start',container])
            return container
        def healthy(container, role):
            for _ in range(16):
                p = subprocess.run([DOCKER,'exec',container,'python3','-B','/app/healthcheck.py',role],
                                   stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5)
                if p.returncode == 0:
                    return
                time.sleep(.3)
            raise RuntimeError('test_runtime_not_ready')
        def stop_remove(container):
            identity(container)
            command([DOCKER,'rm','--force',container],timeout=20)
            removed.add(container)
            report['cleanup'][container] = 'removed'
        try:
            build = json.loads((STAGING / ('antidpi-build-'+build_id+'.json')).read_text())
            if (build.get('state')!='success' or not build.get('production_unchanged')
                    or build.get('context_sha256')!=builder.CONTEXT_SHA256):
                raise ValueError('unaccepted_build')
            images = {r:build['images'][r]['verified']['id'] for r in ('engine','socks')}
            for image in images.values():
                labels = json.loads(command([DOCKER,'image','inspect','--format','{{json .Config.Labels}}',image]))
                if labels.get('vpn.dashboard.run')!=build_id or labels.get('vpn.dashboard.scope')!='antidpi-staging':
                    raise ValueError('test_image_identity_mismatch')
            hosts = {'www.youtube.com':['142.250.74.206']}
            if mode in ('sites', 'strategies', 'confirm'):
                report['stage'] = 'gateway_dns_snapshot'
                requested_hosts = (target,) if target is not None else SITES
                dns = json.loads(command([DOCKER,'exec','vpn-dashboard','python','-B','-c',dns_query(requested_hosts)],timeout=50))
                hosts = {host:answer_ipv4(dns[host]) for host in requested_hosts}
                if hashlib.sha256(Path('/volume1/docker/vpn-gateway/mihomo/config.yaml').read_bytes()).hexdigest()!=before['config_sha256']:
                    raise ValueError('gateway_dns_config_changed')
                report['dns_addresses'] = hosts
                hosts = comparison_hosts(hosts, requested_hosts)
                report['comparison_addresses'] = hosts
            private_file(secret,secrets.token_urlsafe(32))
            pins = contract(hosts,120 if mode=='offline' else 600)
            private_file(pins_path,json.dumps(pins))
            report['stage'] = 'start_test_runtime'
            if mode in ('strategies', 'confirm'):
                strategies = load('antidpi-strategy-probe')
                source = Path(strategies.__file__).read_text(encoding='utf-8')
                candidates = ('tlsrec-sni',) if mode=='confirm' else tuple(strategies.CANDIDATES)
                report['strategy_source_sha256'] = hashlib.sha256(source.encode('utf-8')).hexdigest()
                report['candidate_options'] = {key:list(strategies.CANDIDATES[key]) for key in candidates}
                report['runtime_states'] = []
                def start_candidate(strategy):
                    print('ANTIDPI_CANDIDATE='+strategy+'|HOST='+target,flush=True)
                    argv = strategies.engine_argv(strategy)
                    owner = create('engine',images['engine'],code='import os; os.execv('+repr(argv[0])+','+repr(argv)+')')
                    healthy(owner,'engine')
                    auth = create('socks',images['socks'],owner)
                    healthy(auth,'socks')
                    namespaces = [os.stat('/proc/{}/ns/net'.format(int(command(
                        [DOCKER,'inspect','--format','{{.State.Pid}}',container])))).st_ino for container in (owner,auth)]
                    if len(set(namespaces))!=1 or namespaces[0]==os.stat('/proc/self/ns/net').st_ino:
                        raise RuntimeError('invalid_test_namespace')
                    return owner,auth
                def measure_candidate(handle, host, method):
                    code = source+'\nimport json; print(json.dumps(probe_https('+repr(host)+','+repr(method)+')))\n'
                    row = json.loads(command([DOCKER,'exec',handle[0],'python3','-B','-c',code],timeout=15))
                    print('ANTIDPI_PROBE='+method+'|STAGE='+row['stage']+'|OK='+str(row['ok'])
                          +'|MS='+str(row['elapsed_ms'])+'|STATUS='+str(row.get('status','-'))
                          +'|ERROR='+row.get('error_type','-'),flush=True)
                    return row
                def stop_candidate(handle):
                    for container in reversed(handle):
                        state = json.loads(command([DOCKER,'inspect','--format','{{json .State}}',container]))
                        report['runtime_states'].append({'oom_killed':state.get('OOMKilled',False),
                            'running':state.get('Running',False),'exit_code':state.get('ExitCode')})
                        stop_remove(container)
                        if state.get('OOMKilled') or not state.get('Running'):
                            raise RuntimeError('test_runtime_died')
                report['stage'] = 'bounded_strategy_matrix'
                report['matrix'] = strategies.run_matrix(target,start_candidate,measure_candidate,stop_candidate,
                                                        candidates=candidates)
                report['state'] = 'success'
            else:
                owner = create('engine',images['engine'],code=FIXTURE if mode=='offline' else None)
                healthy(owner,'engine')
                auth = create('socks',images['socks'],owner)
                healthy(auth,'socks')
                peer = create('peer',images['engine'],owner,code='import time; time.sleep(900)')
                namespaces = []
                for container in (owner,auth,peer):
                    pid = int(command([DOCKER,'inspect','--format','{{.State.Pid}}',container]))
                    namespaces.append(os.stat('/proc/{}/ns/net'.format(pid)).st_ino)
                if len(set(namespaces))!=1 or namespaces[0]==os.stat('/proc/self/ns/net').st_ino:
                    raise RuntimeError('invalid_test_namespace')
                report['namespace'] = namespaces[0]
                if mode=='sites':
                    report['stage'] = 'bounded_https_probes'
                    print('ANTIDPI_STAGE=bounded_https_probes',flush=True)
                    output = command([DOCKER,'exec',peer,'python3','-B','-u','-c',SITE_CLIENT],timeout=550)
                    rows = [json.loads(line) for line in output.splitlines()]
                    if len(rows)!=18:
                        raise RuntimeError('incomplete_site_measurements')
                    report['measurements'] = rows
                    report['site_acceptance'] = all(row['ok'] for row in rows if row['mode']=='byedpi')
                else:
                    for check in ('drift','expiry'):
                        report['stage'] = 'native_hosts_' + check
                        print('ANTIDPI_STAGE='+report['stage'],flush=True)
                        if check=='expiry':
                            stop_remove(auth)
                            pins = contract(hosts,45)
                            private_file(pins_path,json.dumps(pins))
                            auth = create('socks',images['socks'],owner)
                            healthy(auth,'socks')
                        child = subprocess.Popen([DOCKER,'exec',peer,'python3','-B','-u','-c',OFFLINE_CLIENT],
                            stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
                        children.append(child)
                        readable,_,_ = select.select([child.stdout],[],[],20)
                        if not readable:
                            raise RuntimeError('native_hosts_stream_timeout')
                        ready = json.loads(child.stdout.readline())
                        if ready.get('stage')!='stream_ready' or ready.get('bytes')!=262144:
                            raise RuntimeError('native_hosts_echo_failed')
                        if check=='drift':
                            changed = dict(pins,source_revision='b'*64)
                            private_file(pins_path,json.dumps(changed))
                        output,_ = child.communicate(timeout=70)
                        if child.returncode!=0 or json.loads(output).get('closed') is not True:
                            raise RuntimeError('dns_guard_did_not_close_existing_stream')
                        state = command([DOCKER,'inspect','--format','{{.State.Running}}|{{.State.ExitCode}}',auth]).strip()
                        if state!='false|4':
                            raise RuntimeError('dns_guard_supervisor_did_not_exit')
                        report['checks'][check] = dict(ready,auth_exit=4,existing_stream_closed=True)
                    targets = [json.loads(line) for line in command([DOCKER,'exec',owner,'cat','/tmp/targets.jsonl']).splitlines()]
                    if len(targets)!=2 or any(t!={'atype':1,'target':'142.250.74.206','port':443} for t in targets):
                        raise RuntimeError('second_dns_lookup_or_forbidden_destination')
                    report['checks']['native_socks_targets'] = targets
            report['state'] = 'success'
        except Exception as error:
            report['error_type'] = type(error).__name__
            if error.args and isinstance(error.args[0],str) and re.fullmatch(r'[a-z_]{1,80}',error.args[0]):
                report['error_code'] = error.args[0]
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
            for container in reversed(created):
                if container in removed:
                    continue
                try:
                    stop_remove(container)
                except Exception as error:
                    report['cleanup'][container] = type(error).__name__
                    report['state'] = 'failed'
            for _ in range(10):
                after = snapshot()
                if before==after:
                    break
                time.sleep(.5)
            report['after'] = after
            report['production_unchanged'] = before==after
            if not report['production_unchanged']:
                report['state'] = 'failed'
            destination = STAGING / ('antidpi-pinned-'+run_id+'.json')
            builder.publish_report(report,destination)
            print('ANTIDPI_PINNED='+report['state'],flush=True)
            if mode == 'sites':
                print('SITE_ACCEPTANCE=' + ('passed' if report.get('site_acceptance') is True else 'failed'),flush=True)
            if mode in ('strategies', 'confirm'):
                matrix = report.get('matrix', {})
                print('STRATEGY_ACCEPTANCE=' + ('passed' if acceptance_exit(report)==0 else 'failed'),flush=True)
                print('STRATEGY_STOP_REASON=' + matrix.get('stop_reason','runtime_error'),flush=True)
                print('STRATEGY_CANDIDATES=' + ','.join(matrix.get('accepted_candidates',[])),flush=True)
            print('REPORT='+str(destination),flush=True)
    os.close(lock)
    return acceptance_exit(report)


if __name__=='__main__':
    sys.exit(main())
