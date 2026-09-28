#!/usr/bin/env python3
"""Install the reviewed, signature-verified Restic artifact without replacement.

No downloads, repository initialization, service operations or arbitrary CLI
arguments. See docs/diagnostics/2026-09-28-owner-discovery.md for provenance.
"""
import hashlib
import json
import os
from pathlib import Path
import platform
import stat
import subprocess
import sys
import tempfile


VERSION = '0.19.1'
BINARY_SHA256 = '20d4142678d0d95ec11a4759def1b73fd9190abc9ca19e4b62d067c0b387e639'
SOURCE = Path('/volume1/docker/vpn-gateway/.vless-maintenance/restic-0.19.1-linux-amd64')
DESTINATION = Path('/usr/local/bin/restic')
MAX_BINARY = 64 * 1024 * 1024


class InstallError(RuntimeError):
    pass


def version_probe(path):
    try:
        result = subprocess.run([str(path), 'version'], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10, shell=False,
            env={'PATH': '/usr/local/bin:/usr/bin:/bin', 'LC_ALL': 'C'})
        if result.returncode or len(result.stdout) > 1024:
            raise InstallError('version_probe_failed')
        return result.stdout.decode('ascii')
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        raise InstallError('version_probe_failed') from None


def install_verified(source, destination, digest, *, version_probe=version_probe):
    source, destination = Path(source), Path(destination)
    if destination.exists() or destination.is_symlink():
        raise InstallError('destination_already_exists')
    for path in (source, destination.parent):
        if not path.is_absolute() or any(item.is_symlink() for item in (path, *path.parents)):
            raise InstallError('unsafe_path')
    source_fd = os.open(str(source), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
    with os.fdopen(source_fd, 'rb') as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_BINARY:
            raise InstallError('invalid_artifact')
        data = stream.read(MAX_BINARY + 1)
    if len(data) > MAX_BINARY or hashlib.sha256(data).hexdigest() != digest:
        raise InstallError('artifact_digest_mismatch')
    if not data.startswith(b'\x7fELF\x02\x01') or data[18:20] != b'\x3e\x00':
        raise InstallError('artifact_not_linux_amd64')
    handle, filename = tempfile.mkstemp(prefix='.restic-verified-', dir=str(destination.parent))
    staged = Path(filename)
    try:
        with os.fdopen(handle, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(str(staged), 0o700)
        version = version_probe(staged).strip()
        if not version.startswith('restic ' + VERSION + ' compiled with ') or not version.endswith('on linux/amd64'):
            raise InstallError('unexpected_version_or_platform')
        os.chmod(str(staged), 0o755)
        # link() is atomic and refuses to replace a concurrently created target.
        os.link(str(staged), str(destination))
        if os.name == 'posix':
            directory = os.open(str(destination.parent), os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        return {'result': 'installed', 'version': version, 'sha256': digest, 'path': str(destination)}
    finally:
        staged.unlink()


def main():
    if (len(sys.argv) != 1 or os.name != 'posix' or os.geteuid() != 0
            or platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'amd64')):
        raise InstallError('linux_amd64_root_required_no_arguments')
    for parent in (DESTINATION.parent, *DESTINATION.parent.parents):
        info = parent.stat()
        if parent.is_symlink() or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
            raise InstallError('destination_parent_not_root_controlled')
    result = install_verified(SOURCE, DESTINATION, BINARY_SHA256)
    installed = DESTINATION.stat()
    if installed.st_uid != 0 or stat.S_IMODE(installed.st_mode) != 0o755:
        raise InstallError('installed_permissions_unexpected')
    print('RESTIC_INSTALL=' + json.dumps(result, sort_keys=True), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        reason = str(error) if isinstance(error, InstallError) else type(error).__name__
        print('RESTIC_INSTALL=failed; reason=' + reason, flush=True)
        sys.exit(1)
