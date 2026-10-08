"""Private, acknowledged reload of the auth child; no TCP controller is opened."""
import http.client
import json
import os
from pathlib import Path
import socket
import stat
import threading
import time

from .production import canonical


def unix_request(path, method, target, payload, timeout):
    path=Path(path)
    if (method,target) not in (('GET','/version'),('PUT','/configs?force=false')):
        raise ValueError('unapproved_auth_control_operation')
    if (not path.is_absolute() or path.name!='control.sock'
            or any(item.is_symlink() for item in (path,*path.parents))):
        raise ValueError('unsafe_auth_control_path')
    parent=path.parent.stat();node=path.stat()
    if (os.name!='posix' or not stat.S_ISDIR(parent.st_mode) or parent.st_uid!=os.getuid()
            or stat.S_IMODE(parent.st_mode)!=0o700 or not stat.S_ISSOCK(node.st_mode)
            or node.st_uid!=os.getuid()):
        raise ValueError('unsafe_auth_control_permissions')
    os.chmod(str(path),0o600)

    class Connection(http.client.HTTPConnection):
        def connect(self):
            self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
            self.sock.settimeout(timeout);self.sock.connect(str(path))

    connection=Connection('localhost',timeout=timeout)
    def abort():
        peer=connection.sock
        if peer is not None:
            try: peer.shutdown(socket.SHUT_RDWR)
            except OSError: pass
        connection.close()
    timer=threading.Timer(timeout,abort);timer.daemon=True;timer.start()
    try:
        connection.request(method,target,body=payload,
                           headers={'Content-Type':'application/json','Connection':'close'})
        response=connection.getresponse();data=response.read(16385)
        if len(data)>16384: raise ValueError('auth_control_response_too_large')
        return response.status,data
    finally:
        timer.cancel();connection.close()


class MihomoUnixControl:
    def __init__(self,path,request=unix_request,clock=time.monotonic,sleep=time.sleep):
        self.path,self.request,self.clock,self.sleep=Path(path),request,clock,sleep

    def apply(self,value):
        payload=canonical({'payload':canonical(value).decode('utf-8')})
        if len(payload)>16384: raise ValueError('auth_control_payload_too_large')
        deadline=self.clock()+10
        while True:
            try:
                status,body=self.request(self.path,'GET','/version',None,0.5)
                if status!=200 or json.loads(body).get('version')!='v1.19.28':
                    raise ValueError('auth_control_version_mismatch')
                break
            except (OSError,http.client.HTTPException):
                if self.clock()>=deadline: raise ValueError('auth_control_unavailable') from None
                self.sleep(0.1)
        # A lost response is ambiguous: never repeat this PUT. The supervisor
        # closes its child on failure, then starts from the current trusted files.
        status,body=self.request(self.path,'PUT','/configs?force=false',payload,3)
        if status!=204 or body:
            raise ValueError('auth_control_reload_unconfirmed')
