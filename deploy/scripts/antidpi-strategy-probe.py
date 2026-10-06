"""Reviewed, bounded ByeDPI candidates for manual isolated tests, never apply.

Options: official hufrea/byedpi commit 7efde1b1296eaaa187b70e951894dde17527489c.
This is not the production strategy registry or an automatic rotation algorithm.
"""
import time


SITES = ('www.youtube.com', 'discord.com', 'web.telegram.org', 'www.instagram.com', 'www.wikipedia.org')
CANDIDATES = {
    'split-1': ('--split', '1'),
    'split-sni': ('--split', '1+s'),
    'disorder-1': ('--disorder', '1'),
    'disorder-sni': ('--disorder', '1+s'),
    'tlsrec-sni': ('--tlsrec', '1+s'),
    'oob-sni': ('--oob', '1+s'),
    'disoob-sni': ('--disoob', '1+s'),
    'fake-md5': ('--fake', '-1', '--md5sig'),
}
ATTEMPT_SECONDS = 10
MATRIX_SECONDS = 480  # shorter than the existing 600-second DNS pin lease
READ_LIMIT = 32768


def probe_https(host, mode):
    import socket
    import ssl
    from runtime import read_dns_pins
    if host not in SITES or mode not in ('direct_control', 'byedpi'):
        raise ValueError('invalid_probe_target')
    pins = read_dns_pins()  # validates public addresses, expiry and exact hosts
    if host not in pins['hosts'] or len(pins['hosts'][host]) != 1:
        raise ValueError('one_pinned_destination_required')
    began = time.monotonic()
    deadline = began + ATTEMPT_SECONDS
    row = {'ok': False, 'tls_ok': False, 'stage': 'connect'}
    peer = None

    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError('attempt_deadline')
        return value

    def receive(size):
        result = bytearray()
        while len(result) < size:
            peer.settimeout(remaining())
            chunk = peer.recv(size - len(result))
            if not chunk:
                raise ConnectionError('short_socks_reply')
            result.extend(chunk)
        return bytes(result)

    try:
        endpoint = ('127.0.0.1', 1080) if mode == 'byedpi' else (pins['hosts'][host][0], 443)
        peer = socket.create_connection(endpoint, timeout=remaining())
        if mode == 'byedpi':
            from runtime import read_secret
            password = read_secret().encode('ascii')
            peer.settimeout(remaining())
            peer.sendall(b'\x05\x01\x02')
            if receive(2) != b'\x05\x02':
                raise ConnectionError('auth_method_rejected')
            peer.settimeout(remaining())
            peer.sendall(b'\x01\x07gateway' + bytes([len(password)]) + password)
            if receive(2) != b'\x01\x00':
                raise ConnectionError('auth_rejected')
            name = host.encode('ascii')
            peer.settimeout(remaining())
            peer.sendall(b'\x05\x01\x00\x03' + bytes([len(name)]) + name + b'\x01\xbb')
            header = receive(4)
            if header[:2] != b'\x05\x00' or header[3] not in (1, 4):
                raise ConnectionError('socks_connect_rejected')
            receive(6 if header[3] == 1 else 18)
        row['connect_ms'] = round((time.monotonic() - began) * 1000)
        row['stage'] = 'tls'
        peer.settimeout(remaining())
        peer = ssl.create_default_context().wrap_socket(peer, server_hostname=host)
        row['tls_ok'] = True
        row['tls_version'] = peer.version()
        row['tls_ms'] = round((time.monotonic() - began) * 1000)
        row['stage'] = 'http'
        peer.settimeout(remaining())
        peer.sendall(('GET / HTTP/1.1\r\nHost: ' + host + '\r\n'
                      'User-Agent: VPN-Gateway-Acceptance/1.0\r\n'
                      'Accept: text/html\r\nConnection: close\r\n\r\n').encode('ascii'))
        data = bytearray()
        # Headers suffice for this small reachability probe. No redirect or media download.
        while len(data) < READ_LIMIT and b'\r\n\r\n' not in data:
            peer.settimeout(remaining())
            block = peer.recv(min(8192, READ_LIMIT - len(data)))
            if not block:
                break
            data.extend(block)
        header = bytes(data).split(b'\r\n', 1)[0].split()
        if (b'\r\n\r\n' not in data or len(header) < 2
                or header[0] not in (b'HTTP/1.0', b'HTTP/1.1') or len(header[1]) != 3):
            raise ValueError('invalid_http_response')
        status = int(header[1])
        if not 100 <= status <= 599:
            raise ValueError('invalid_http_status')
        row.update(status=status, received_bytes=len(data), ok=200 <= status < 400,
                   redirect=300 <= status < 400, stage='complete')
    except Exception as error:
        row['error_type'] = type(error).__name__
    finally:
        if peer is not None:
            peer.close()
    row['elapsed_ms'] = round((time.monotonic() - began) * 1000)
    return row


def engine_argv(strategy):
    if not isinstance(strategy, str) or strategy not in CANDIDATES:
        raise ValueError('unreviewed_strategy')
    return ['/usr/local/bin/ciadpi', '--ip', '127.0.0.1', '--port', '1081',
            '--conn-ip', '0.0.0.0', '--no-udp', '--no-domain', '--max-conn', '128',
            '--timeout', '10'] + list(CANDIDATES[strategy])


def run_matrix(host, start, measure, stop, *, now=time.monotonic,
               max_attempts=60, cancelled=None, candidates=tuple(CANDIDATES)):
    if host not in SITES or type(max_attempts) is not int or not 1 <= max_attempts <= 60:
        raise ValueError('invalid_bounded_test_request')
    if (not isinstance(candidates, tuple) or not candidates or len(candidates)>len(CANDIDATES)
            or any(not isinstance(name,str) or name not in CANDIDATES for name in candidates)
            or len(set(candidates))!=len(candidates)):
        raise ValueError('unreviewed_strategy_selection')
    began = now()
    result = {'host': host, 'measurements': [], 'accepted_candidates': [],
              'stop_reason': 'completed', 'applied': False}

    def can_continue():
        if cancelled is not None and cancelled():
            result['stop_reason'] = 'cancelled'
        elif now() - began >= MATRIX_SECONDS - ATTEMPT_SECONDS:
            result['stop_reason'] = 'time_limit'
        elif len(result['measurements']) >= max_attempts:
            result['stop_reason'] = 'attempt_limit'
        else:
            return True
        return False

    groups = [('control-before', 'split-1', 'direct_control')]
    groups += [(name, name, 'byedpi') for name in candidates]
    groups += [('control-after', 'split-1', 'direct_control')]
    for label, strategy, mode in groups:
        if not can_continue():
            break
        handle = start(strategy)
        try:
            for attempt in range(1, 4):
                if not can_continue():
                    break
                row = dict(measure(handle, host, mode), candidate=label, mode=mode,
                           host=host, attempt=attempt)
                result['measurements'].append(row)
        finally:
            stop(handle)
        if result['stop_reason'] != 'completed':
            break
    # An interrupted run is not accepted even if a few individual probes worked.
    if result['stop_reason'] == 'completed':
        for strategy in candidates:
            rows = [r for r in result['measurements'] if r['candidate'] == strategy]
            if len(rows) == 3 and all(r.get('ok') is True for r in rows):
                result['accepted_candidates'].append(strategy)
    result['elapsed_seconds'] = round(now() - began, 3)
    return result
