"""Host-owned bounded probes. Sleeping peer handshakes are not health failures."""

import time
from dataclasses import dataclass

REQUIRED = {
    "wireguard": ("interface", "identity", "api"),
    "mihomo": ("controller", "dns_fakeip", "isolated_outbounds"),
    "antidpi": ("engine", "namespace", "authenticated_socks", "tcp_e2e"),
    "uptime-kuma": ("http", "database"),
    "metacubexd": ("http",),
    "dashboard": ("http", "database"),
}


class ReadinessError(RuntimeError):
    pass


@dataclass(frozen=True)
class CheckResult:
    name: str
    state: str
    reason_code: str
    observed_at: float


class Readiness:
    """Probe functions come from the root manifest adapter, not the browser.

    Each function accepts a timeout in seconds and must bound its own IO. The
    NAS adapter cannot depend on dashboard exec: dashboard is restarted last.
    """
    def __init__(self, checks, *, monotonic=time.monotonic, sleep=time.sleep, clock=time.time):
        self.checks, self.monotonic, self.sleep, self.clock = checks, monotonic, sleep, clock

    def preflight(self, components):
        for component in components:
            if component not in REQUIRED or any(not callable(self.checks.get(component, {}).get(name)) for name in REQUIRED[component]):
                raise ReadinessError("host_probes_not_configured")

    def check(self, component, deadline=None):
        if component not in REQUIRED:
            raise ReadinessError("component_not_allowed")
        deadline = self.monotonic() + 30 if deadline is None else deadline
        result = []
        for name in REQUIRED[component]:
            callback = self.checks.get(component, {}).get(name)
            state, reason = "unknown", "probe_not_configured"
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                reason = "readiness_timeout"
            elif callable(callback):
                try:
                    value = callback(timeout=min(5, remaining))
                    if self.monotonic() > deadline:
                        state, reason = "unknown", "readiness_timeout"
                    elif value is True:
                        state, reason = "pass", "verified"
                    elif value is False:
                        state, reason = "fail", "probe_failed"
                    else:
                        state, reason = "unknown", "probe_unavailable"
                except Exception:
                    state, reason = "unknown", "probe_unavailable"
            result.append(CheckResult(name, state, reason, self.clock()))
        return tuple(result)

    def wait(self, component):
        self.preflight((component,))
        deadline = self.monotonic() + 120
        while True:
            result = self.check(component, deadline)
            if result and all(item.state == "pass" for item in result):
                return result
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                raise ReadinessError("readiness_timeout")
            self.sleep(min(2, remaining))
