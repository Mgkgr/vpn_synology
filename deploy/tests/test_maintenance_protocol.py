import importlib
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
JOB = "a" * 32
REVISION = "b" * 64


def request(**changes):
    result = dict(action="restart", components=["mihomo"], expected_revision=REVISION,
                  release_ids={}, enable_stopped=[], snapshot_id=None, accept_data_loss=False)
    result.update(changes)
    return result


def frame(**changes):
    value = {"version": 1, "method": "submit", "params": {"job_id": JOB, "actor": "admin", "request": request()}}
    value.update(changes)
    return json.dumps(value).encode() + b"\n"


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / "maintenance/protocol.py").is_file(), "closed protocol is not implemented")
        self.m = importlib.import_module("maintenance.protocol")

    def test_protocol_rejects_injection(self):
        self.assertEqual(self.m.decode_frame(frame(), 10001)["method"], "submit")
        for data, uid in ((frame(extra="sh"), 10001), (frame(version=2), 10001), (frame(), 1234), (frame(method="exec"), 0), (b" " * 65537, 0), (frame() + frame(), 0), (b'{"version":1,"version":2}\n', 0)):
            with self.subTest(data=data[:100]), self.assertRaises(self.m.ProtocolError):
                self.m.decode_frame(data, uid)
        for params in (request(components=["../docker"]), request(command="sh"), request(release_ids={"mihomo": "latest"}), request(components=["mihomo", "mihomo"]), request(accept_data_loss=True), request(enable_stopped=["wireguard"])):
            with self.subTest(params=params), self.assertRaises(self.m.ProtocolError):
                self.m.parse_request(params)

    def test_telemetry_does_not_change_configuration_revision(self):
        before = {"images": {"mihomo": "sha256:" + "a" * 64}, "clients": [{"id": 1, "name": "pc", "enabled": True, "received_bytes": 0, "latest_handshake_at": None}], "rules": ["DOMAIN,example.com,DIRECT"], "telemetry": {"uptime": 1}, "sessions": []}
        after = json.loads(json.dumps(before))
        after["clients"][0].update(received_bytes=4096, latest_handshake_at="2026-09-27T00:00:00Z")
        after.update(telemetry={"uptime": 200}, sessions=[{"id": 17}], probes=[{"ok": True}])
        self.assertEqual(self.m.compute_revision(before), self.m.compute_revision(after))
        after["clients"][0]["name"] = "new-name"
        self.assertNotEqual(self.m.compute_revision(before), self.m.compute_revision(after))
        after = dict(before, admin_revision=2)
        self.assertNotEqual(self.m.compute_revision(before), self.m.compute_revision(after))

    def test_cancel_is_closed_and_hash_is_canonical(self):
        first = self.m.parse_request(request())
        other_order = dict(reversed(list(request().items())))
        self.assertEqual(self.m.request_hash(first), self.m.request_hash(self.m.parse_request(other_order)))
        self.assertEqual(self.m.parse_request({"action": "cancel", "job_id": JOB}).job_id, JOB)
        with self.assertRaises(self.m.ProtocolError):
            self.m.parse_request({"action": "cancel", "job_id": JOB, "command": "sh"})


if __name__ == "__main__":
    unittest.main()
