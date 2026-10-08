"""Closed, independent DNS leases for five Anti-DPI control hosts only."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import ipaddress
import json
import math
import re
import socket
import ssl
import threading
import time

from antidpi.dns_pins import HOST
from antidpi.production import HOSTS, CONTROL_HOST, canonical, validate_lease

NAMES=tuple(HOSTS.values())+(CONTROL_HOST,)
PROVIDERS={'cloudflare':('cloudflare-dns.com','1.1.1.1','/dns-query'),
           'google':('dns.google','8.8.8.8','/resolve')}
REVISION=hashlib.sha256(canonical(dict(version=1,providers=PROVIDERS,hosts=NAMES))).hexdigest()


class PinnedHTTPS(http.client.HTTPSConnection):
    """TLS name verification with fixed bootstrap IP, no host DNS or proxy."""
    def __init__(self,host,address,timeout=4):
        super().__init__(host,443,timeout=timeout,context=ssl.create_default_context())
        self.address=address
        self.abort_socket=None

    def connect(self):
        raw=socket.create_connection((self.address,443),self.timeout)
        try:
            # A duplicate permits the deadline to interrupt even a slow HTTP
            # body/header when HTTPResponse owns the original socket file.
            self.abort_socket=raw.dup()
            self.sock=self._context.wrap_socket(raw,server_hostname=self.host)
        except Exception:
            raw.close(); self.close(); raise

    def abort(self):
        active=self.abort_socket
        if active is not None:
            try: active.shutdown(socket.SHUT_RDWR)
            except OSError: pass

    def close(self):
        super().close()
        if self.abort_socket is not None:
            self.abort_socket.close(); self.abort_socket=None


def _name(value):
    if not isinstance(value,str): raise ValueError('invalid_dns_name')
    value=value.rstrip('.').lower()
    if len(value)>253 or not HOST.fullmatch(value): raise ValueError('invalid_dns_name')
    return value


def parse_answer(host,value,*,started,completed,age=0):
    if (host not in NAMES or not isinstance(value,dict) or type(value.get('Status')) is not int
            or value['Status']!=0 or value.get('TC') is not False
            or type(age) is not int or not 0<=age<=2147483647
            or any(type(n) not in (int,float) or not math.isfinite(n) for n in (started,completed))
            or not started<=completed):
        raise ValueError('invalid_dns_response')
    questions=value.get('Question')
    if (not isinstance(questions,list) or len(questions)!=1 or not isinstance(questions[0],dict)
            or _name(questions[0].get('name'))!=host or type(questions[0].get('type')) is not int
            or questions[0]['type']!=1): raise ValueError('dns_question_mismatch')
    rows=value.get('Answer')
    if not isinstance(rows,list) or not 1<=len(rows)<=32: raise ValueError('invalid_dns_answer')
    aliases={}; addresses={}; ttls=[]; owners=set()
    for row in rows:
        if not isinstance(row,dict): raise ValueError('invalid_dns_answer')
        owner=_name(row.get('name')); kind=row.get('type'); ttl=row.get('TTL')
        if (type(ttl) is not int or not 0<ttl<=2147483647 or type(kind) is not int or kind not in (1,5)):
            raise ValueError('invalid_dns_record')
        owners.add(owner); ttls.append(ttl)
        if kind==5:
            if owner in aliases: raise ValueError('ambiguous_dns_alias')
            aliases[owner]=_name(row.get('data'))
        else:
            address=ipaddress.IPv4Address(row.get('data'))
            if (not address.is_global or address.is_reserved or address.is_multicast
                    or address in ipaddress.IPv4Network('192.0.0.0/24')):
                raise ValueError('nonpublic_dns_answer')
            addresses.setdefault(owner,set()).add(str(address))
    seen=set(); current=host
    while current in aliases:
        if current in seen or current in addresses: raise ValueError('ambiguous_dns_alias')
        seen.add(current); current=aliases[current]
    seen.add(current)
    values=addresses.get(current,set())
    if owners!=seen or set(addresses)!={current} or not 1<=len(values)<=16:
        raise ValueError('unrelated_dns_answer')
    expiry=started+min(600,min(ttls)-age)
    if expiry<=completed: raise ValueError('expired_dns_answer')
    return dict(addresses=sorted(values),expires_at=expiry)


def query_answers(provider,host):
    if provider not in PROVIDERS or host not in NAMES: raise ValueError('unapproved_dns_query')
    hostname,address,path=PROVIDERS[provider]
    connection=PinnedHTTPS(hostname,address,timeout=4)
    deadline=threading.Timer(6,connection.abort); deadline.daemon=True
    started=time.time(); monotonic=time.monotonic(); deadline.start()
    try:
        connection.request('GET',path+'?name='+host+'&type=A&cd=false&do=false&edns_client_subnet=0.0.0.0%2F0',
            headers={'Accept':'application/dns-json','Cache-Control':'no-cache','User-Agent':'VPN-Gateway-AntiDPI-DNS/1.0'})
        response=connection.getresponse()
        if response.status!=200: raise ValueError('dns_http_error')
        content_type=response.getheader('Content-Type','').split(';')[0].strip().lower()
        if content_type not in ('application/dns-json','application/json'): raise ValueError('dns_content_type')
        age=response.getheader('Age','0')
        if not re.fullmatch(r'[0-9]{1,10}',age): raise ValueError('invalid_dns_http_age')
        data=response.read(32769)
        if len(data)>32768: raise ValueError('dns_response_too_large')
        if time.monotonic()-monotonic>=6: raise ValueError('dns_request_deadline')
        return parse_answer(host,json.loads(data),started=started,completed=time.time(),age=int(age))
    finally:
        deadline.cancel(); connection.close()


class DnsRefresher:
    def __init__(self,query=query_answers,clock=time.time):
        self.query,self.clock=query,clock
        self.entries={}; self.next_attempt={host:0 for host in NAMES}
        self.last_clock=None

    def fetch_host(self,host,preferred):
        candidates=[]
        for provider in PROVIDERS:
            try:
                value=self.query(provider,host)
                now=self.clock()
                if value['expires_at']<=now: continue
                candidates.append(value)
                if value['expires_at']-now>=45 and (preferred is None or preferred in value['addresses']): break
            except Exception: continue
        now=self.clock()
        candidates=[v for v in candidates if v['expires_at']>now]
        if not candidates: raise ValueError('dns_providers_unavailable')
        stable=[v for v in candidates if preferred in v['addresses'] and v['expires_at']-now>=15]
        chosen=max(stable or candidates,key=lambda v:v['expires_at'])
        return dict(address=preferred if preferred in chosen['addresses'] else chosen['addresses'][0],
                    expires_at=chosen['expires_at'])

    def refresh(self):
        now=self.clock()
        if self.last_clock is not None and now<self.last_clock:
            self.entries.clear(); self.next_attempt={host:0 for host in NAMES}
        self.last_clock=now
        due=[host for host in NAMES if now>=self.next_attempt[host]]
        def fetch(host):
            try: return host,self.fetch_host(host,self.entries.get(host,{}).get('address'))
            except Exception: return host,None
        if due:
            with ThreadPoolExecutor(max_workers=5) as pool:
                results=list(pool.map(fetch,due))
            now=self.clock()
            if now<self.last_clock:
                self.entries.clear(); self.next_attempt={host:0 for host in NAMES}
            self.last_clock=now
            for host,value in results:
                if value is not None and value['expires_at']>now:
                    self.entries[host]=value
                    self.next_attempt[host]=now+max(1,min(30,(value['expires_at']-now)/3))
                else:
                    self.next_attempt[host]=now+2
        if set(self.entries)!=set(NAMES) or any(value['expires_at']<=now for value in self.entries.values()):
            raise ValueError('dns_snapshot_unavailable')
        return validate_lease(dict(version=2,created_at=now,
            expires_at=min(value['expires_at'] for value in self.entries.values()),source_revision=REVISION,
            hosts={host:[value['address']] for host,value in self.entries.items()}),now)
