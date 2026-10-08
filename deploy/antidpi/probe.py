"""Small TLS-verified HTTPS probe to an exact public IPv4. Never follows redirects."""
import ipaddress
import socket
import ssl
import time

from .production import HOSTS, CONTROL_HOST


def https_probe(host, address, *, socks_port=None, password=None, timeout=10):
    if host not in tuple(HOSTS.values()) + (CONTROL_HOST,):
        raise ValueError('unapproved_probe_host')
    ip = ipaddress.IPv4Address(address)
    if (str(ip) != address or not ip.is_global or ip.is_reserved or ip.is_multicast
            or ip in ipaddress.IPv4Network('192.0.0.0/24')):
        raise ValueError('nonpublic_probe_address')
    if type(timeout) not in (int, float) or not 0 < timeout <= 10:
        raise ValueError('unbounded_probe_timeout')
    if socks_port not in (None, 1080, 1081, 1082, 1083, 1084):
        raise ValueError('unapproved_probe_port')
    began=time.monotonic(); deadline=began+timeout; peer=None
    result=dict(verdict='unknown',reason='probe_failed',latency_ms=None,http_status=None)
    def remaining():
        value=deadline-time.monotonic()
        if value <= 0: raise TimeoutError('probe_deadline')
        return value
    def send(data):
        peer.settimeout(remaining()); peer.sendall(data)
    def receive(size):
        data=bytearray()
        while len(data)<size:
            peer.settimeout(remaining()); block=peer.recv(size-len(data))
            if not block: raise ConnectionResetError('socks_closed')
            data.extend(block)
        return bytes(data)
    try:
        peer=socket.create_connection(('127.0.0.1',socks_port) if socks_port else (address,443),timeout=remaining())
        if socks_port:
            send(b'\x05\x01'+(b'\x02' if password else b'\x00'))
            if receive(2)!=(b'\x05\x02' if password else b'\x05\x00'):
                raise ValueError('socks_auth_failed')
            if password:
                from .runtime import validate_secret
                secret=validate_secret(password).encode('ascii')
                send(b'\x01\x07gateway'+bytes([len(secret)])+secret)
                if receive(2)!=b'\x01\x00': raise ValueError('socks_auth_failed')
                name=host.encode('ascii')
                target=b'\x03'+bytes([len(name)])+name
            else:
                target=b'\x01'+socket.inet_aton(address)
            send(b'\x05\x01\x00'+target+b'\x01\xbb')
            header=receive(4)
            if header[:3]!=b'\x05\x00\x00' or header[3] not in (1,4):
                raise ValueError('socks_connect_failed')
            receive(6 if header[3]==1 else 18)
        peer.settimeout(remaining())
        peer=ssl.create_default_context().wrap_socket(peer,server_hostname=host)
        send(('GET / HTTP/1.1\r\nHost: '+host+'\r\nUser-Agent: VPN-Gateway-Health/1.0\r\n'
              'Accept: text/html\r\nConnection: close\r\n\r\n').encode('ascii'))
        data=bytearray()
        while len(data)<32768 and b'\r\n\r\n' not in data:
            peer.settimeout(remaining()); block=peer.recv(min(8192,32768-len(data)))
            if not block: break
            data.extend(block)
        header=bytes(data).split(b'\r\n',1)[0].split()
        if (b'\r\n\r\n' not in data or len(header)<2 or header[0] not in (b'HTTP/1.0',b'HTTP/1.1')
                or len(header[1])!=3 or not header[1].isdigit() or not 100<=int(header[1])<=599):
            raise ValueError('invalid_http_response')
        status=int(header[1])
        result.update(verdict='success' if status==200 else 'http_error',
                      reason='verified' if status==200 else 'http_status',http_status=status)
    except ssl.SSLCertVerificationError:
        result.update(verdict='certificate_error',reason='certificate_invalid')
    except (TimeoutError,socket.timeout):
        result.update(verdict='transport_error',reason='timeout')
    except ConnectionResetError:
        result.update(verdict='transport_error',reason='reset')
    except ssl.SSLError:
        result.update(verdict='transport_error',reason='tls_transport')
    except (OSError,ValueError):
        pass  # Closed error taxonomy, no addresses/passwords/raw exceptions in logs.
    finally:
        if peer is not None: peer.close()
    elapsed=round((time.monotonic()-began)*1000)
    # Scheduler/process overhead must not manufacture an out-of-contract latency.
    if elapsed > 10000:
        if not (result['verdict']=='transport_error' and result['reason']=='timeout'):
            result.update(verdict='unknown',reason='probe_failed',http_status=None)
        result['latency_ms']=None
    else:
        result['latency_ms']=elapsed
    return result
