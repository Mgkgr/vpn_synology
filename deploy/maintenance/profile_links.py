"""Strict standard-link import. Exceptions and summaries never include secrets."""
import base64
import html
import ipaddress
import re
from urllib.parse import parse_qsl, unquote, urlsplit
import uuid


def public_ip(value):
    address = ipaddress.ip_address(value)
    if (not address.is_global or address.is_reserved or address.is_multicast
            or (address.version == 4 and address in ipaddress.ip_network('192.0.0.0/24'))):
        raise ValueError('nonpublic_endpoint')
    return str(address)


def hostname(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 253 or not value.isascii():
        raise ValueError('invalid_host')
    value = value.lower()
    try: return public_ip(value)
    except ValueError: pass
    labels = value.split('.')
    if (len(labels)<2 or not re.fullmatch(r'[a-z][a-z0-9-]*', labels[-1])
            or labels[-1] in ('local','localhost','internal','lan','home','invalid','test')
            or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in labels)):
        raise ValueError('invalid_host')
    return value


def _secret(value):
    if not isinstance(value,str) or not 1<=len(value)<=4096 or any(not c.isprintable() or c.isspace() for c in value):
        raise ValueError('invalid_secret')
    return value


def parse_link(raw):
    try:
        if not isinstance(raw,str) or not 1<=len(raw)<=8192: raise ValueError()
        value = html.unescape(raw).replace('\\://','://').replace('\\@','@').replace('\\&','&').strip()
        if any(not c.isprintable() or c.isspace() for c in value): raise ValueError()
        uri = urlsplit(value)
        if uri.scheme not in ('vless','hy2','hysteria2') or uri.password is not None or uri.path not in ('','/'):
            raise ValueError()
        server = hostname(uri.hostname)
        port = uri.port if uri.port is not None else 443
        if not 1<=port<=65535: raise ValueError()
        pairs = parse_qsl(uri.query, keep_blank_values=True, strict_parsing=True)
        opts = dict(pairs)
        if len(pairs)!=len(opts) or any(not c.isprintable() for _, item in pairs for c in item): raise ValueError()
        sni = hostname(opts['sni'])
        if uri.scheme in ('hy2','hysteria2'):
            if set(opts)-{'obfs','obfs-password','sni','mport'} or opts.get('obfs')!='salamander': raise ValueError()
            result = dict(name='HY2-USA', type='hysteria2', server=server, port=port,
                          password=_secret(unquote(uri.username or '')), obfs='salamander',
                          sni=sni, udp=True)
            result.update({'obfs-password':_secret(opts['obfs-password']), 'skip-cert-verify':False})
            if 'mport' in opts:
                ports = opts['mport']
                if not re.fullmatch(r'[0-9]{1,5}-[0-9]{1,5}',ports): raise ValueError()
                first,last = map(int,ports.split('-'))
                if not 1<=first<=last<=65535: raise ValueError()
                result['ports'] = ports
            return result
        if set(opts)-{'type','security','encryption','flow','sni','fp','pbk','sid','spx'}: raise ValueError()
        if any(opts.get(key)!=expected for key,expected in
               (('type','tcp'),('security','reality'),('encryption','none'),('flow','xtls-rprx-vision'))): raise ValueError()
        identifier = str(uuid.UUID(unquote(uri.username or '')))
        if opts['fp'] not in ('chrome','firefox','safari','ios','android','edge','random','randomized'): raise ValueError()
        key = opts['pbk']
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}',key) or len(base64.urlsafe_b64decode(key+'='))!=32: raise ValueError()
        if not re.fullmatch(r'(?:[a-fA-F0-9]{2}){1,8}',opts['sid']): raise ValueError()
        if 'spx' in opts and (not opts['spx'].startswith('/') or len(opts['spx'])>512): raise ValueError()
        return {'name':'WG-IMP','type':'vless','server':server,'port':port,'uuid':identifier,
                'network':'tcp','udp':True,'tls':True,'skip-cert-verify':False,'flow':'xtls-rprx-vision',
                'servername':sni,'client-fingerprint':opts['fp'],'packet-encoding':'xudp',
                'reality-opts':{'public-key':key,'short-id':opts['sid'].lower()}}
    except (ValueError,KeyError,TypeError,AttributeError):
        raise ValueError('invalid_profile_link') from None


def safe_summary(profile):
    return dict(target=profile['name'],protocol=profile['type'],server=profile['server'],port=profile['port'])
