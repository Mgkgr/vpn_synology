#!/usr/bin/env python3
"""Approved one-shot bootstrap/backup from an exact reviewed source archive."""
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

STAGING = Path('/volume1/docker/vpn-gateway/.vless-maintenance')
PRIVATE = Path('/volume1/docker/vpn-dashboard-maintenance/private')
ARCHIVE_SHA256 = '8fb0fb76c7c2676daf5d46654679d840728cae9e5a251bff39b6db129a0f4ab3'
MEMBERS = {'maintenance/' + name + '.py' for name in
           ('__init__', 'protocol', 'store', 'catalog', 'backups', 'backup_cli', 'bootstrap_backup')}


def prepare_private_directory(path):
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError('unsafe_private_root')
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.stat()
    if info.st_uid != 0:
        raise ValueError('private_root_owner')
    if info.st_mode & 0o777 != 0o700:
        if any(path.iterdir()):
            raise ValueError('nonempty_private_root_permissions')
        # DSM may inherit a Synology ACL despite mkdir(0700). Never rewrite ACL
        # on a populated/existing project: only this empty root-owned new folder.
        acl = Path('/usr/syno/bin/synoacltool')
        if acl.is_file():
            subprocess.run([str(acl), '-del', str(path)], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=True)
        os.chmod(str(path), 0o700)
    if path.stat().st_mode & 0o777 != 0o700:
        raise ValueError('private_root_permissions')


def main():
    if sys.argv[1:] != ['--approved'] or os.geteuid() != 0:
        raise SystemExit('Only --approved in the existing root console is permitted.')
    import fcntl
    spec = importlib.util.spec_from_file_location('reviewed_builder', STAGING / 'build-antidpi-staging.py')
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    run_id = uuid.uuid4().hex
    report = {'run_id': run_id, 'result': 'failed', 'scope': 'encrypted_backup_only', 'source_archive': ARCHIVE_SHA256}
    fd = os.open('/var/run/vpn-dashboard-reviewed-backup.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        archive_fd = os.open(str(STAGING / 'reviewed-backup-code.tar'), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(archive_fd, 'rb') as stream:
            contents = helper.verify_archive(stream.read(2097153), ARCHIVE_SHA256, MEMBERS)
        # No caller-supplied destination, symlinks, shared/group-readable code or reuse.
        for path in (PRIVATE.parent, PRIVATE, PRIVATE / 'code'):
            prepare_private_directory(path)
        root = PRIVATE / 'code' / run_id
        root.mkdir(mode=0o700)
        for name, content in contents.items():
            path = root / name
            path.parent.mkdir(mode=0o700, exist_ok=True)
            out = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(out, 'wb') as target:
                target.write(content)
        sys.path.insert(0, str(root))
        from maintenance.bootstrap_backup import run
        print('BACKUP_STAGE=capture_encrypt_restore_verify', flush=True)
        report.update(run())
    except Exception as error:
        report['error_type'] = type(error).__name__
        # Only explicit machine codes, never raw command/DB/exception output.
        if error.args and isinstance(error.args[0], str) and re.fullmatch(r'[a-z_]{1,80}', error.args[0]):
            report['error_code'] = error.args[0]
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        destination = STAGING / ('reviewed-backup-' + run_id + '.json')
        helper.publish_report(report, destination)
        print('BACKUP=' + report['result'], flush=True)
        print('REPORT=' + str(destination), flush=True)
    return 0 if report['result'] == 'verified' else 1


if __name__ == '__main__':
    sys.exit(main())
