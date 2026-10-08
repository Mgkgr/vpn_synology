"""Run in the pinned auth image with --network none, never against client traffic."""
import json
from pathlib import Path
import socket
import threading
import time

from antidpi.runtime_service import AuthService


def receive(peer,size):
    value=b''
    while len(value)<size:
        block=peer.recv(size-len(value))
        if not block: raise ValueError('stream_closed')
        value+=block
    return value


def echo_socks(listener):
    with listener.accept()[0] as peer:
        peer.settimeout(15)
        header=receive(peer,2);receive(peer,header[1]);peer.sendall(b'\x05\x00')
        header=receive(peer,4)
        if header[3]==1: receive(peer,6)
        elif header[3]==3: receive(peer,receive(peer,1)[0]+2)
        else: raise ValueError('unexpected_destination')
        peer.sendall(b'\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00')
        for _ in range(2): peer.sendall(receive(peer,8))


def main():
    config=dict(version=1,identity='a'*64,generation='b'*64,
        selections=dict(youtube='tlsrec-sni',discord='tlsrec-sni',telegram='tlsrec-sni',instagram='disorder-sni'))
    now=time.time()
    # Synthetic, never-networked fixture; not evidence of real DNS or site reachability.
    lease=dict(version=2,created_at=now,expires_at=now+300,source_revision='c'*64,
        hosts={'www.youtube.com':['142.250.74.206'],'discord.com':['162.159.135.232'],
               'web.telegram.org':['149.154.167.99'],'www.instagram.com':['157.240.16.174'],
               'www.wikipedia.org':['185.15.59.224']})
    service=AuthService(Path('/tmp/auth'))
    listener=socket.socket();listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    listener.bind(('127.0.0.1',1081));listener.listen(1);listener.settimeout(15)
    thread=threading.Thread(target=echo_socks,args=(listener,),daemon=True);thread.start()
    client=None
    try:
        service.tick('s'*48,config,lease)
        end=time.monotonic()+10
        while True:
            try: client=socket.create_connection(('127.0.0.1',1080),timeout=2);break
            except OSError:
                if time.monotonic()>end: raise
                time.sleep(0.05)
        client.settimeout(5);client.sendall(b'\x05\x01\x02')
        assert receive(client,2)==b'\x05\x02'
        client.sendall(b'\x01\x07gateway\x30'+b's'*48)
        assert receive(client,2)==b'\x01\x00'
        host=b'www.youtube.com'
        client.sendall(b'\x05\x01\x00\x03'+bytes([len(host)])+host+b'\x01\xbb')
        header=receive(client,4);assert header[:3]==b'\x05\x00\x00'
        receive(client,6 if header[3]==1 else 18)
        client.sendall(b'before!!');assert receive(client,8)==b'before!!'
        original=service.child.pid
        lease['hosts']['www.instagram.com']=['157.240.16.175']
        service.tick('s'*48,config,lease)
        if service.child.pid!=original: raise ValueError('SHARED_AUTH_PROCESS_REPLACED')
        client.sendall(b'after!!!');assert receive(client,8)==b'after!!!'
        assert service.child.poll() is None
        print(json.dumps(dict(shared_pid_preserved=True,existing_stream_preserved=True,
                              controller_transport='private_unix',network='none')),flush=True)
    finally:
        if client is not None: client.close()
        service.stop();listener.close();thread.join(timeout=2)


if __name__=='__main__':
    try: main()
    except Exception as exc:
        reason=str(exc)
        if reason!='SHARED_AUTH_PROCESS_REPLACED': reason=type(exc).__name__
        print('OFFLINE_RELOAD_TEST=failed|'+reason,flush=True)
        raise SystemExit(1)
