#!/usr/bin/env python3
"""Exercise real fallback on localhost only; never reconfigure production."""
import argparse
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, ProxyHandler, build_opener


def build_config(primary, reserve, mixed_port, api_port, secret, failed_port, primary_down=False):
    if primary.get("name") != "WG-IMP" or reserve.get("name") != "HY2-USA":
        raise ValueError("Unexpected profile identifiers")
    if len({mixed_port, api_port, failed_port}) != 3 or not all(1024 <= p <= 65535 for p in (mixed_port, api_port, failed_port)):
        raise ValueError("Three distinct unprivileged ports are required")
    profiles = copy.deepcopy([primary, reserve])
    if primary_down:
        profiles[0].update(server="127.0.0.1", port=failed_port)
    return {
        "mixed-port": mixed_port, "allow-lan": False, "bind-address": "127.0.0.1",
        "external-controller": "127.0.0.1:%d" % api_port, "secret": secret,
        "mode": "rule", "log-level": "warning", "ipv6": False,
        "tun": {"enable": False}, "dns": {"enable": False}, "proxies": profiles,
        "proxy-groups": [{"name": "MAINT-FALLBACK", "type": "fallback",
                          "proxies": ["WG-IMP", "HY2-USA"],
                          "url": "https://cp.cloudflare.com/generate_204", "interval": 2,
                          "timeout": 4000, "lazy": False, "max-failed-times": 1}],
        "rules": ["MATCH,MAINT-FALLBACK"],
    }


def run(helper, primary, reserve):
    before = helper.CONFIG.read_bytes()
    source = before.decode("utf-8")
    for profile in (primary, reserve):
        if helper.replace_outbound(source, profile) != source:
            raise RuntimeError("Test credentials do not match the applied configuration")
    job = Path(tempfile.mkdtemp(prefix="isolated-fallback-", dir=str(helper.WORK)))
    helper.status(job, state="running", production_changed=False, test_interval_seconds=2)
    print(json.dumps({"status_file": str(job / "status.json")}), flush=True)
    process = None
    reserved = []
    config = job / "config.json"
    executable = job / "mihomo"
    try:
        for _ in range(3):
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            reserved.append(listener)
        mixed_port, api_port, failed_port = [s.getsockname()[1] for s in reserved]
        secret = os.urandom(32).hex()
        normal = build_config(primary, reserve, mixed_port, api_port, secret, failed_port)
        failed = build_config(primary, reserve, mixed_port, api_port, secret, failed_port, primary_down=True)
        helper.status(job, stage="download_pinned_binary")
        executable = helper.binary(job)
        helper.atomic_write(config, json.dumps(normal))
        direct = build_opener(ProxyHandler({}))
        proxy = build_opener(ProxyHandler({"https": "http://127.0.0.1:%d" % mixed_port}))

        def api(path, payload=None, timeout=10):
            body = None if payload is None else json.dumps(payload).encode("utf-8")
            request = Request("http://127.0.0.1:%d%s" % (api_port, path), data=body,
                              headers={"Authorization": "Bearer " + secret, "Content-Type": "application/json"},
                              method="GET" if payload is None else "PUT")
            with direct.open(request, timeout=timeout) as response:
                data = response.read(256 * 1024)
            return json.loads(data) if data else {}

        def wait_state(expected, primary_alive):
            started = time.monotonic()
            while time.monotonic() - started < 45:
                proxies = api("/proxies")["proxies"]
                group = proxies["MAINT-FALLBACK"]
                p, r = proxies["WG-IMP"], proxies["HY2-USA"]
                if (group.get("now") == expected and not group.get("fixed") and
                        p.get("history") and p.get("alive") is primary_alive and
                        r.get("history") and r.get("alive") is True):
                    return {"selected": expected, "primary_alive": primary_alive,
                            "reserve_alive": True, "observed_after_ms": round((time.monotonic() - started) * 1000)}
                time.sleep(.5)
            raise RuntimeError("Isolated fallback state did not converge")

        def https_check():
            with proxy.open("https://cp.cloudflare.com/generate_204", timeout=12) as response:
                if response.status != 204:
                    raise RuntimeError("Isolated HTTPS check failed")
                return response.status

        def geography():
            result = {}
            try:
                with proxy.open("https://www.cloudflare.com/cdn-cgi/trace", timeout=12) as response:
                    trace = response.read(8192).decode("ascii", errors="ignore")
                match = re.search(r"(?m)^loc=([A-Z]{2})$", trace)
                result["cloudflare_country"] = match.group(1) if match else "unknown"
            except Exception as error:
                result["cloudflare_error"] = type(error).__name__
            try:
                with proxy.open("https://ipinfo.io/json", timeout=12) as response:
                    data = json.loads(response.read(16384))
                country = data.get("country")
                result["ipinfo_country"] = country if isinstance(country, str) and re.fullmatch(r"[A-Z]{2}", country) else "unknown"
            except Exception as error:
                result["ipinfo_error"] = type(error).__name__
            return result

        with (job / "private-process.log").open("wb") as log:
            check = subprocess.run([str(executable), "-t", "-f", str(config), "-d", str(job)],
                                   stdout=log, stderr=log, timeout=30)
            if check.returncode:
                raise RuntimeError("Isolated configuration validation failed")
            for listener in reserved[:2]:
                listener.close()
            process = subprocess.Popen([str(executable), "-f", str(config), "-d", str(job)],
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=log)
            for attempt in range(60):
                if process.poll() is not None:
                    raise RuntimeError("Isolated process exited")
                try:
                    api("/version", timeout=1)
                    break
                except Exception:
                    if attempt == 59:
                        raise RuntimeError("Isolated API did not start")
                    time.sleep(.25)
            helper.status(job, stage="both_healthy")
            initial = wait_state("WG-IMP", True)
            initial["http"] = https_check()
            helper.status(job, initial=initial, stage="isolated_primary_unavailable")
            api("/configs", {"payload": json.dumps(failed)})
            fallback = wait_state("HY2-USA", False)
            fallback["http"] = https_check()
            fallback.update(geography())
            helper.status(job, fallback=fallback, stage="isolated_primary_restored")
            api("/configs", {"payload": json.dumps(normal)})
            restored = wait_state("WG-IMP", True)
            restored["http"] = https_check()
            if helper.CONFIG.read_bytes() != before:
                raise RuntimeError("Production configuration changed during verification")
            helper.status(job, state="success", stage="complete", restored=restored,
                          production_unchanged=True, config_sha256=hashlib.sha256(before).hexdigest())
    except BaseException as error:
        helper.status(job, state="failed", error_type=type(error).__name__,
                      production_unchanged=helper.CONFIG.read_bytes() == before)
        raise
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for listener in reserved:
            listener.close()
        config.unlink(missing_ok=True)
        executable.unlink(missing_ok=True)
        helper.status(job, cleanup_complete=True)
    print((job / "status.json").read_text(encoding="utf-8"), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--helper", type=Path, default=Path(__file__).with_name("replace-primary-vless.py"))
    args = parser.parse_args()
    os.umask(0o077)
    spec = importlib.util.spec_from_file_location("outbound_maintenance", args.helper)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    payload = json.loads(sys.stdin.read())
    try:
        run(helper, helper.parse_profile(payload["primary_uri"]), helper.parse_profile(payload["reserve_uri"]))
    except Exception as error:
        print(json.dumps({"state": "failed", "error_type": type(error).__name__}), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
