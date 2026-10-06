#!/usr/bin/env python3
"""Rollback-capable replacement of the primary or reserve (Python 3.8+).

Credentials are accepted on stdin, never as command-line arguments. The legacy
outbound name deliberately remains stable so rules and fallback stay intact.
"""
import base64
import argparse
import gzip
import hashlib
import html
import json
import os
from pathlib import Path
import re
import stat
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qsl, urlsplit, urlencode, quote, unquote
from urllib.request import Request, ProxyHandler, build_opener
import uuid


def parse_profile(raw):
    value = html.unescape(raw).replace("\\://", "://").replace("\\@", "@").strip()
    if value.startswith(("hy2://", "hysteria2://")):
        return parse_hysteria_profile(value)
    return parse_vless_profile(value)


def parse_hysteria_profile(value):
    try:
        uri = urlsplit(value)
        if uri.scheme not in ("hy2", "hysteria2") or uri.password is not None or not uri.hostname:
            raise ValueError()
        if uri.path not in ("", "/"):
            raise ValueError()
        password = unquote(uri.username or "")
        pairs = parse_qsl(uri.query, keep_blank_values=True, strict_parsing=True)
        options = dict(pairs)
        if len(pairs) != len(options) or set(options) - {"obfs", "obfs-password", "sni", "mport"}:
            raise ValueError()
        if options.get("obfs") != "salamander":
            raise ValueError()
        for host in (uri.hostname, options["sni"]):
            if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host):
                raise ValueError()
        for secret in (password, options["obfs-password"]):
            if not secret or len(secret) > 4096 or any(ord(c) < 33 or ord(c) == 127 for c in secret):
                raise ValueError()
        port = uri.port if uri.port is not None else 443
        if not 1 <= port <= 65535:
            raise ValueError()
        profile = {
            "name": "HY2-USA", "type": "hysteria2", "server": uri.hostname,
            "port": port, "password": password, "obfs": "salamander",
            "obfs-password": options["obfs-password"], "sni": options["sni"],
            "skip-cert-verify": False, "udp": True,
        }
        if "mport" in options:
            ports = options["mport"]
            if not re.fullmatch(r"[0-9]{1,5}-[0-9]{1,5}", ports):
                raise ValueError()
            first, last = map(int, ports.split("-"))
            if not 1 <= first <= last <= 65535:
                raise ValueError()
            profile["ports"] = ports
        return profile
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ValueError("Invalid or unsupported Hysteria2 profile") from None


def parse_vless_profile(raw):
    try:
        value = html.unescape(raw).replace("\\://", "://").replace("\\@", "@").strip()
        uri = urlsplit(value)
        if uri.scheme != "vless" or uri.password or not uri.hostname or not uri.port:
            raise ValueError()
        identifier = str(uuid.UUID(uri.username))
        pairs = parse_qsl(uri.query, keep_blank_values=True, strict_parsing=True)
        options = dict(pairs)
        if len(pairs) != len(options):
            raise ValueError()
        for name, expected in (("type", "tcp"), ("security", "reality"),
                               ("encryption", "none"), ("flow", "xtls-rprx-vision")):
            if options.get(name) != expected:
                raise ValueError()
        for name in (uri.hostname, options["sni"]):
            if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", name):
                raise ValueError()
        fingerprint = options["fp"]
        if fingerprint not in ("chrome", "firefox", "safari", "ios", "android", "edge", "random", "randomized"):
            raise ValueError()
        public_key = options["pbk"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", public_key):
            raise ValueError()
        if len(base64.urlsafe_b64decode(public_key + "=")) != 32:
            raise ValueError()
        short_id = options["sid"]
        if not re.fullmatch(r"(?:[0-9a-fA-F]{2}){1,8}", short_id):
            raise ValueError()
        return {
            "name": "WG-IMP", "type": "vless", "server": uri.hostname,
            "port": uri.port, "uuid": identifier, "network": "tcp", "udp": True,
            "tls": True, "skip-cert-verify": False, "flow": options["flow"],
            "servername": options["sni"], "client-fingerprint": fingerprint,
            "packet-encoding": "xudp",
            "reality-opts": {"public-key": public_key, "short-id": short_id},
        }
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ValueError("Invalid or unsupported VLESS TCP REALITY profile") from None


def replace_outbound(text, profile):
    name = profile["name"]
    if (name, profile["type"]) not in (("WG-IMP", "vless"), ("HY2-USA", "hysteria2")):
        raise ValueError("Only the known primary and reserve can be replaced")
    sections = list(re.finditer(r"(?m)^proxies:[ \t]*(?:#.*)?\r?$", text))
    if len(sections) != 1:
        raise ValueError("Expected one block-style proxies section")
    section_start = sections[0].end() + 1
    next_section = re.search(r"(?m)^[A-Za-z][A-Za-z0-9_-]*:", text[section_start:])
    section_end = section_start + next_section.start() if next_section else len(text)
    section = text[section_start:section_end]
    items = list(re.finditer(r"(?m)^( *)- name:[ \t]*([^\r\n]+)\r?$", section))
    targets = [(i, item) for i, item in enumerate(items)
               if item.group(2).strip().strip("\"'") == name]
    if len(targets) != 1:
        raise ValueError("Expected exactly one target outbound; no changes made")
    i, item = targets[0]
    start = section_start + item.start()
    end = section_start + (items[i + 1].start() if i + 1 < len(items) else len(section))
    indent = item.group(1)
    lines = [indent + "- name: " + name, indent + "  type: " + profile["type"]]
    for key, value in profile.items():
        if key in ("name", "type"):
            continue
        if isinstance(value, dict):
            lines.append(indent + "  " + key + ":")
            for nested_key, nested_value in value.items():
                lines.append(indent + "    " + nested_key + ": " + json.dumps(nested_value))
        else:
            lines.append(indent + "  " + key + ": " + json.dumps(value))
    newline = "\r\n" if "\r\n" in text else "\n"
    return text[:start] + newline.join(lines) + newline + text[end:]


def read_text(path):
    return Path(path).read_bytes().decode("utf-8")


def atomic_write(path, content, mode=0o600):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            os.chmod(temporary, mode)
            handle.write(content.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def commit_candidate(path, before, candidate, apply, verify):
    path = Path(path)
    if path.is_symlink() or read_text(path) != before:
        raise RuntimeError("Configuration changed concurrently; not applying")
    mode = stat.S_IMODE(path.stat().st_mode)
    try:
        apply(candidate)
        verify()
        if read_text(path) != before:
            raise RuntimeError("Configuration changed concurrently; not overwriting")
        atomic_write(path, candidate, mode)
    except BaseException:
        # Roll back to the current disk configuration, including administrator edits.
        apply(read_text(path))
        raise


TARGETS = (("cloudflare", "https://cp.cloudflare.com/generate_204", 204),
           ("google", "https://www.google.com/generate_204", 204),
           ("github", "https://api.github.com/", 200))
CONFIG = Path("/volume1/docker/vpn-gateway/mihomo/config.yaml")
WORK = Path("/volume1/docker/vpn-gateway/.vless-maintenance")
BINARY_URL = "https://github.com/MetaCubeX/mihomo/releases/download/v1.19.28/mihomo-linux-amd64-v1-go123-v1.19.28.gz"
BINARY_SHA256 = "0a7b83be7251c111a543dcaac3a3505559c9cf7d8a2ba343bf8fb32442757271"


def isolated_config(profile, port):
    return {"mixed-port": port, "allow-lan": False, "bind-address": "127.0.0.1",
            "mode": "rule", "log-level": "warning", "ipv6": False,
            "tun": {"enable": False}, "dns": {"enable": False},
            "proxies": [profile], "rules": ["MATCH," + profile["name"]]}


def probes_acceptable(results):
    return all(sum(item.get("ok") is True and item.get("target") == name
                   for item in results) >= 2 for name, _, _ in TARGETS)


def status(job, **fields):
    path = job / "status.json"
    current = json.loads(read_text(path)) if path.exists() else {}
    current.update(fields)
    current["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    atomic_write(path, json.dumps(current, ensure_ascii=True, indent=2) + "\n")


def binary(job):
    executable = job / "mihomo"
    opener = build_opener(ProxyHandler({}))
    with opener.open(BINARY_URL, timeout=30) as response:
        data = response.read(32 * 1024 * 1024)
    if hashlib.sha256(data).hexdigest() != BINARY_SHA256:
        raise RuntimeError("Binary checksum mismatch")
    executable.write_bytes(gzip.decompress(data))
    executable.chmod(0o700)
    return executable


def preflight(profile, job):
    status(job, stage="isolated_download")
    executable = binary(job)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    isolated = job / "isolated"
    isolated.mkdir(mode=0o700)
    config = isolated / "config.yaml"
    atomic_write(config, json.dumps(isolated_config(profile, port)))
    log_path = job / "isolated-private.log"
    process = None
    results = []
    try:
        with log_path.open("wb") as log:
            completed = subprocess.run([str(executable), "-t", "-d", str(isolated)],
                                       stdout=log, stderr=log, timeout=40)
            if completed.returncode:
                raise RuntimeError("Isolated configuration validation failed")
            process = subprocess.Popen([str(executable), "-d", str(isolated)],
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=log)
            for _ in range(40):
                if process.poll() is not None:
                    raise RuntimeError("Isolated process exited")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    time.sleep(0.25)
            status(job, stage="isolated_probes")
            proxy = build_opener(ProxyHandler({"http": "http://127.0.0.1:%d" % port,
                                               "https": "http://127.0.0.1:%d" % port}))
            for round_number in range(3):
                for name, url, expected in TARGETS:
                    started = time.monotonic()
                    result = {"target": name, "round": round_number + 1, "ok": False}
                    try:
                        with proxy.open(Request(url, headers={"User-Agent": "VPN-Gateway-Check/1.0"}), timeout=12) as response:
                            result.update(ok=response.status == expected, http=response.status)
                            response.read(65536)
                    except Exception as error:
                        result["error_type"] = type(error).__name__
                    result["milliseconds"] = round((time.monotonic() - started) * 1000)
                    results.append(result)
                    status(job, isolated_probes=results)
            country = "unknown"
            try:
                with proxy.open("https://www.cloudflare.com/cdn-cgi/trace", timeout=12) as response:
                    trace = response.read(8192).decode("ascii", errors="ignore")
                match = re.search(r"(?m)^loc=([A-Z]{2})$", trace)
                if match:
                    country = match.group(1)
            except Exception:
                pass
            status(job, exit_country=country)
            if not probes_acceptable(results):
                raise RuntimeError("Isolated probe gate failed; production unchanged")
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        config.unlink(missing_ok=True)
        executable.unlink(missing_ok=True)
    return results


class Controller:
    def __init__(self, configuration):
        match = re.search(r"(?m)^secret:[ \t]*(.+?)\s*$", configuration)
        if not match:
            raise RuntimeError("Controller secret is missing")
        value = match.group(1)
        self.secret = json.loads(value) if value.startswith('"') else value.strip("'")
        self.opener = build_opener(ProxyHandler({}))

    def request(self, path, payload=None):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request("http://192.168.2.103:9091" + path, data=body,
                          method="GET" if payload is None else "PUT",
                          headers={"Authorization": "Bearer " + self.secret,
                                   "Content-Type": "application/json"})
        with self.opener.open(request, timeout=60 if payload is not None else 15) as response:
            data = response.read()
        return json.loads(data) if data else {}

    def apply(self, configuration):
        self.request("/configs", {"payload": configuration})

    def inspect(self):
        proxies = self.request("/proxies")["proxies"]
        return {"primary_type": proxies.get("WG-IMP", {}).get("type"),
                "reserve_type": proxies.get("HY2-USA", {}).get("type"),
                "fallback_order": proxies.get("VPS-FALLBACK", {}).get("all"),
                "fallback_selected": proxies.get("VPS-FALLBACK", {}).get("now"),
                "rule_count": len(self.request("/rules").get("rules", []))}

    def probes(self, name, rounds=2):
        results = []
        for round_number in range(rounds):
            for target, url, _ in TARGETS:
                result = {"target": target, "round": round_number + 1, "ok": False}
                try:
                    data = self.request("/proxies/" + quote(name, safe="") + "/delay?" +
                                        urlencode({"url": url, "timeout": 10000}))
                    result.update(ok=isinstance(data.get("delay"), (int, float)) and data["delay"] > 0,
                                  milliseconds=data.get("delay"))
                except Exception as error:
                    result["error_type"] = type(error).__name__
                results.append(result)
        return results


def encrypted_backup(configuration, job):
    keys = Path.home() / ".vpn-gateway-backup-keys"
    keys.mkdir(mode=0o700, exist_ok=True)
    if stat.S_IMODE(keys.stat().st_mode) & 0o077:
        raise RuntimeError("Backup key directory is not private")
    key = keys / (job.name + ".key")
    atomic_write(key, os.urandom(32).hex() + "\n")
    destination = job / "config-before.enc"
    arguments = ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt", "-pass", "file:" + str(key)]
    encrypted = subprocess.run(arguments, input=configuration.encode("utf-8"), stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=20, check=True).stdout
    destination.write_bytes(encrypted)
    destination.chmod(0o600)
    decrypted = subprocess.run(arguments + ["-d"], input=encrypted, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=20, check=True).stdout
    if decrypted != configuration.encode("utf-8"):
        raise RuntimeError("Backup restoration verification failed")
    status(job, encrypted_backup=str(destination), backup_key_file=str(key), backup_verified=True)


def run(profile, job, apply_changes):
    target = profile["name"]
    changed_key = "primary_type" if target == "WG-IMP" else "reserve_type"
    preserved_key = "reserve_type" if target == "WG-IMP" else "primary_type"
    before = read_text(CONFIG)
    candidate = replace_outbound(before, profile)
    controller = Controller(before)
    baseline = controller.inspect()
    status(job, state="running", production_changed=False, target=target, baseline=baseline)
    preflight(profile, job)
    if not apply_changes:
        status(job, state="success", stage="preflight_complete")
        return
    if baseline["fallback_order"] != ["WG-IMP", "HY2-USA"]:
        raise RuntimeError("Unexpected fallback order; production unchanged")
    if read_text(CONFIG) != before:
        raise RuntimeError("Configuration changed during preflight; not applying")
    encrypted_backup(before, job)
    status(job, stage="runtime_apply")

    def verify():
        actual = controller.inspect()
        if (str(actual[changed_key]).lower() != profile["type"] or
                actual[preserved_key] != baseline[preserved_key] or
                actual["fallback_order"] != baseline["fallback_order"] or
                actual["rule_count"] != baseline["rule_count"]):
            raise RuntimeError("Runtime invariants failed")
        status(job, stage="runtime_verify", actual=actual)
        probes = controller.probes(target)
        status(job, runtime_probes=probes)
        if not probes_acceptable(probes):
            raise RuntimeError("Runtime probe gate failed")

    try:
        commit_candidate(CONFIG, before, candidate, controller.apply, verify)
    except BaseException:
        status(job, production_changed=read_text(CONFIG) != before, actual=controller.inspect())
        raise
    companion = "HY2-USA" if target == "WG-IMP" else "WG-IMP"
    status(job, state="success", stage="complete", production_changed=True,
           actual=controller.inspect(), companion=companion,
           companion_probes=controller.probes(companion, rounds=1),
           config_sha256=hashlib.sha256(CONFIG.read_bytes()).hexdigest())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    request = json.loads(sys.stdin.read())
    profile = parse_profile(request["uri"])
    WORK.mkdir(mode=0o700, exist_ok=True)
    job = Path(tempfile.mkdtemp(prefix="job-", dir=str(WORK)))
    status(job, state="starting", production_changed=False)
    pid = os.fork()
    if pid:
        print(json.dumps({"worker_pid": pid, "status_file": str(job / "status.json")}), flush=True)
        return
    os.setsid()
    with open(os.devnull, "rb") as source, open(os.devnull, "ab") as destination:
        os.dup2(source.fileno(), 0)
        os.dup2(destination.fileno(), 1)
        os.dup2(destination.fileno(), 2)
    try:
        run(profile, job, args.apply)
    except BaseException as error:
        # Never put exception messages or controller response bodies in public status.
        status(job, state="failed", error_type=type(error).__name__)
        os._exit(1)
    os._exit(0)


if __name__ == "__main__":
    main()
