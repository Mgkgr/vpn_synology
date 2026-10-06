"""Bounded receive-buffer tuning for DSM docker-proxy UDP/51820.

No daemon or container is restarted by this script. Existing sockets keep their
old buffers until recreated. The original values are retained for explicit undo.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import tempfile

TARGET = 4 * 1024 * 1024
KEYS = ('rmem_default', 'rmem_max')
MARKER = '# VPN Gateway: UDP receive buffers (managed)\n'


def atomic_write(path, text, mode=0o644):
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError('refusing symlink destination')
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name + '-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, str(path))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_values(proc):
    result = {key: int((proc / key).read_text().strip()) for key in KEYS}
    if any(not 4096 <= value <= 134217728 for value in result.values()):
        raise RuntimeError('unexpected receive-buffer size')
    return result


def write_values(proc, values):
    # Do not temporarily lower the maximum below a live/default value.
    max_path = proc / 'rmem_max'
    current_default = int((proc / 'rmem_default').read_text())
    max_path.write_text(str(max(values['rmem_max'], current_default)) + '\n', encoding='ascii')
    (proc / 'rmem_default').write_text(str(values['rmem_default']) + '\n', encoding='ascii')
    max_path.write_text(str(values['rmem_max']) + '\n', encoding='ascii')
    if read_values(proc) != values:
        raise RuntimeError('kernel did not accept receive-buffer settings')


def managed_config(config):
    if config.is_symlink():
        raise RuntimeError('unmanaged sysctl symlink')
    if not config.exists():
        return None
    text = config.read_text(encoding='utf-8')
    if not text.startswith(MARKER):
        raise RuntimeError('unmanaged sysctl file: refusing overwrite')
    expected = {'net.core.' + key for key in KEYS}
    lines = [line.strip() for line in text.splitlines()[1:] if line.strip()]
    if len(lines) != 2 or {line.split('=')[0].strip() for line in lines} != expected:
        raise RuntimeError('unmanaged settings in sysctl file')
    for line in lines:
        if not line.split('=', 1)[1].strip().isdigit():
            raise RuntimeError('unmanaged sysctl value')
    return text


def apply_buffers(proc, config, backup):
    old = read_values(proc)
    previous_file = managed_config(config)
    new = {key: max(TARGET, old[key]) for key in KEYS}
    new['rmem_max'] = max(new.values())
    if not backup.exists():
        atomic_write(backup, json.dumps(dict(values=old, config=previous_file)), 0o600)
    body = MARKER + ''.join('net.core.%s = %s\n' % (key, new[key]) for key in KEYS)
    atomic_write(config, body)
    try:
        write_values(proc, new)
    except Exception:
        try:
            write_values(proc, old)
        finally:
            if previous_file is None:
                config.unlink()
            else:
                atomic_write(config, previous_file)
        raise
    return new


def restore_buffers(proc, config, backup):
    managed_config(config)
    saved = json.loads(backup.read_text(encoding='utf-8'))
    values = saved['values']
    if set(values) != set(KEYS) or any(not isinstance(v, int) or not 4096 <= v <= 134217728 for v in values.values()):
        raise RuntimeError('invalid backup')
    write_values(proc, values)
    if saved['config'] is None:
        if config.exists():
            config.unlink()
    else:
        atomic_write(config, saved['config'])
    return values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['apply', 'restore'])
    action = parser.parse_args().action
    if os.geteuid() != 0:
        raise RuntimeError('run as root on DSM')
    core = Path('/proc/sys/net/core')
    config = Path('/etc/sysctl.d/90-vpn-gateway-udp-receive.conf')
    backup_dir = Path('/usr/local/etc/vpn-gateway')
    for directory, mode in [(config.parent, 0o755), (backup_dir, 0o700)]:
        directory.mkdir(mode=mode, parents=True, exist_ok=True)
        s = directory.lstat()
        if directory.is_symlink() or s.st_uid != 0 or s.st_mode & 0o022:
            raise RuntimeError('settings directory is not root-controlled')
    backup = backup_dir / 'udp-receive-before.json'
    values = (apply_buffers if action == 'apply' else restore_buffers)(core, config, backup)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        effective = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
    if effective != values['rmem_default']:
        raise RuntimeError('new UDP socket did not inherit the expected buffer')
    print('UDP_RECEIVE_BUFFER=' + str(effective), flush=True)
    print('UDP_BUFFER_ACTION=' + action + '|RESULT=success', flush=True)


if __name__ == '__main__':
    main()
