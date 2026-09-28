#!/usr/bin/env python3
"""Build only new, offline staging images from a hash-pinned private context."""
import datetime
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import subprocess
import sys
import tarfile
import tempfile
import uuid


DOCKER = '/usr/local/bin/docker'
STAGING = Path('/volume1/docker/vpn-gateway/.vless-maintenance')
CONTEXT_SHA256 = '6e6a27ad8b561e663da9c0adba68a03c092746c1b517662ad8179e0a3d49457c'
BINARY_SHA256 = 'c70e87c6168af1832b21641a98bb53e3500b1daf6a5df8bfd22fac4a9294abda'
MEMBERS = {'Dockerfile.engine', 'Dockerfile.socks', '.dockerignore', 'runtime.py',
           'healthcheck.py', 'dns_pins.py', '.artifacts/ciadpi-x86_64', '.artifacts/LICENSE',
           '.artifacts/MIHOMO-LICENSE'}
BASES = {
    'python@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de':
        'sha256:25c5b8011a3425a140bf5fa73be0feabd3c0d5b323eecb19dc02437a368ae075',
    'metacubex/mihomo@sha256:e6acd921addecfd59a8e2d38203f88356d635b54de6c0673db0e015139989312':
        'sha256:69f758573310acf1aa78c4c6b2230fa315ae5dbb6abc4f4ff6b3098044b24214',
}
PRODUCTION = ('vpn-wireguard', 'vpn-mihomo', 'vpn-uptime-kuma', 'vpn-metacubexd', 'vpn-dashboard')


def verify_archive(blob, expected_digest, expected_members):
    if len(blob) > 2 * 1024 * 1024 or hashlib.sha256(blob).hexdigest() != expected_digest:
        raise ValueError('context checksum/size mismatch')
    result = {}
    with tarfile.open(fileobj=io.BytesIO(blob), mode='r:*') as archive:
        for member in archive:
            path = PurePosixPath(member.name)
            if (not member.isfile() or member.name not in expected_members
                    or path.is_absolute() or '..' in path.parts or '\\' in member.name
                    or member.name in result or member.size > 1024 * 1024):
                raise ValueError('unexpected context member')
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError('missing context content')
            result[member.name] = stream.read(1024 * 1024 + 1)
            if len(result[member.name]) != member.size:
                raise ValueError('truncated context member')
    if set(result) != set(expected_members):
        raise ValueError('incomplete context')
    return result


def command(argv, timeout=30):
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError('read-only Docker preflight failed')
    return result.stdout.decode('utf-8')


def materialize_context(root, contents):
    # The parent build directory is root-private; image source files are public code.
    # Docker COPY must preserve non-root readability, unlike the separate secret file.
    for name, content in contents.items():
        destination = root / name
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
        os.chmod(str(destination), 0o644)


def production_snapshot():
    template = '{{json .Id}}|{{json .State.Running}}|{{json .State.StartedAt}}|{{json .RestartCount}}'
    return {name: command([DOCKER, 'inspect', '--format', template, name]).strip()
            for name in PRODUCTION}


def image_info(reference):
    template = '{{json .Id}}|{{json .Os}}|{{json .Architecture}}|{{json .RepoDigests}}'
    raw = command([DOCKER, 'image', 'inspect', '--format', template, reference]).strip()
    image_id, system, architecture, digests = map(json.loads, raw.split('|'))
    return {'id': image_id, 'os': system, 'architecture': architecture, 'digests': digests or []}


def publish_report(report, destination):
    import pwd
    fd = os.open(str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(report, stream, sort_keys=True, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
        owner = pwd.getpwnam('prometei')
        os.fchown(stream.fileno(), owner.pw_uid, owner.pw_gid)


def main():
    if sys.argv[1:] != ['--approved']:
        sys.exit('Only --approved is accepted; builds new staging images, no production changes.')
    if os.geteuid() != 0 or platform.system() != 'Linux' or platform.machine() != 'x86_64':
        sys.exit('Existing NAS root console is required.')
    run_id = uuid.uuid4().hex
    report = {'run_id': run_id, 'observed_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'state': 'failed', 'scope': 'offline_staging_images_only',
              'context_sha256': CONTEXT_SHA256, 'images': {}, 'production_ready': False}
    before = None
    try:
        before = production_snapshot()
        report['before'] = before
        for reference, expected_id in BASES.items():
            info = image_info(reference)
            if (info['id'] != expected_id or info['os'] != 'linux'
                    or info['architecture'] != 'amd64' or reference not in info['digests']):
                raise ValueError('cached base image differs from reviewed inventory')
        path = STAGING / 'antidpi-staging-context.tar'
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            contents = verify_archive(stream.read(2 * 1024 * 1024 + 1), CONTEXT_SHA256, MEMBERS)
        if hashlib.sha256(contents['.artifacts/ciadpi-x86_64']).hexdigest() != BINARY_SHA256:
            raise ValueError('engine binary checksum mismatch')
        with tempfile.TemporaryDirectory(prefix='vpn-antidpi-build-', dir='/var/tmp') as directory:
            root = Path(directory)
            materialize_context(root, contents)
            for role in ('engine', 'socks'):
                tag = 'vpn-antidpi-' + role + ':staging-' + run_id
                environment = dict(os.environ, DOCKER_BUILDKIT='0')
                argv = [DOCKER, 'build', '--pull=false', '--network=none', '--memory=384m',
                        '--label', 'vpn.dashboard.scope=antidpi-staging',
                        '--label', 'vpn.dashboard.run=' + run_id,
                        '--file', str(root / ('Dockerfile.' + role)), '--tag', tag, str(root)]
                result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, env=environment, timeout=180, check=False)
                report['images'][role] = {'build_exit': result.returncode, 'tag': tag,
                                         'build_log': result.stdout.decode('utf-8', errors='replace')[-24000:]}
                if result.returncode:
                    raise RuntimeError('isolated image build failed')
                report['images'][role]['verified'] = image_info(tag)
        report['state'] = 'success'
    except Exception as error:
        report['error'] = type(error).__name__
    finally:
        try:
            report['after'] = production_snapshot()
            report['production_unchanged'] = before is not None and before == report['after']
            if not report['production_unchanged']:
                report['state'] = 'failed'
        except Exception as error:
            report['production_unchanged'] = False
            report['snapshot_error'] = type(error).__name__
            report['state'] = 'failed'
        destination = STAGING / ('antidpi-build-' + run_id + '.json')
        publish_report(report, destination)
        print('ANTIDPI_BUILD=' + report['state'], flush=True)
        print('REPORT=' + str(destination), flush=True)
    return 0 if report['state'] == 'success' else 1


if __name__ == '__main__':
    sys.exit(main())
