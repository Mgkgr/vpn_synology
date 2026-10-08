#!/usr/bin/env python3
"""Sudo entry point: verify one source archive, install and publish a safe status."""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tarfile
import tempfile
import time


def main():
    if os.geteuid()!=0 or len(sys.argv)!=2 or not re.fullmatch('[a-f0-9]{64}',sys.argv[1]):
        raise ValueError('root_and_source_hash_required')
    root=Path(__file__).resolve().parent
    if root.parent!=Path('/volume1/docker/vpn-gateway/.vless-maintenance') or not re.fullmatch('antidpi-install-[a-f0-9]{32}',root.name):
        raise ValueError('invalid_installation_directory')
    archive=root/'runtime.tar'
    fd=os.open(str(archive),os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'rb') as stream: data=stream.read(4194305)
    if len(data)>4194304 or hashlib.sha256(data).hexdigest()!=sys.argv[1]: raise ValueError('source_hash_changed')
    result=dict(state='failed',started_at=time.time(),automation_enabled=False)
    status=root/'install-status.json'
    staging=Path(tempfile.mkdtemp(prefix='vpn-antidpi-bootstrap-',dir='/tmp'))
    os.chmod(str(staging),0o700)
    try:
        seen=set()
        with tarfile.open(fileobj=io.BytesIO(data),mode='r:') as tar:
            for member in tar.getmembers():
                if (not member.isfile() or not re.fullmatch(r'deploy/(?:maintenance/[a-z_]+\.py|antidpi/(?:[a-z_]+\.py|versions\.json))',member.name)
                        or not 0<=member.size<=1048576 or member.name in seen):
                    raise ValueError('invalid_archive_member')
                seen.add(member.name); target=staging/member.name[7:]
                target.parent.mkdir(mode=0o700,exist_ok=True)
                fd=os.open(str(target),os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                with os.fdopen(fd,'wb') as stream: stream.write(tar.extractfile(member).read())
        pinned=staging/'runtime.tar'
        fd=os.open(str(pinned),os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as stream: stream.write(data)
        sys.path.insert(0,str(staging))
        from maintenance.host_install import install
        result.update(install(pinned,sys.argv[1])); result['state']='success'
    except Exception as error:
        reason=str(error) if type(error) is ValueError and re.fullmatch('[a-z_]{1,80}',str(error)) else type(error).__name__
        result['reason']=reason
    finally:
        result['finished_at']=time.time()
        fd=os.open(str(status),os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o644)
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            os.fchmod(stream.fileno(),0o644); json.dump(result,stream)
        # Staging holds source, not secrets; remove only our own created tree.
        import shutil
        if staging.parent==Path('/tmp') and staging.name.startswith('vpn-antidpi-bootstrap-') and not staging.is_symlink():
            shutil.rmtree(str(staging))
    print(json.dumps(result),flush=True)
    return 0 if result['state']=='success' else 1


if __name__=='__main__': sys.exit(main())
