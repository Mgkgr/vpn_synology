import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / "maintenance/service.py").is_file(), "maintenance service is not implemented")
        self.m = importlib.import_module("maintenance.service")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = importlib.import_module("maintenance.store").JobStore(Path(self.temp.name) / "private/jobs.sqlite3")

    def test_service_persists_before_response_and_exports_only_safe_errors(self):
        service = self.m.MaintenanceService(self.store, executor_ready=lambda: True)
        params = {"job_id": "a" * 32, "actor": "admin", "request": {"action": "restart", "components": ["mihomo"], "expected_revision": "b" * 64, "release_ids": {}, "enable_stopped": [], "snapshot_id": None, "accept_data_loss": False}}
        raw = json.dumps({"version": 1, "method": "submit", "params": params}).encode() + b"\n"
        response = service.handle(raw, 10001)
        self.assertTrue(response["ok"])
        self.assertEqual(self.store.get_job(params["job_id"]).phase, "queued")
        self.assertEqual(service.handle(raw, 10001), response)
        with patch.object(self.store, "submit", side_effect=OSError("secret=/private/path")):
            result = service.handle(raw, 10001)
        self.assertEqual(result, {"ok": False, "error": "storage_unavailable"})

    def test_unconfigured_executor_refuses_submission_without_queuing(self):
        service = self.m.MaintenanceService(self.store)
        params = {"job_id": "a" * 32, "actor": "owner", "request": {"action": "restart", "components": ["mihomo"], "expected_revision": "b" * 64, "release_ids": {}, "enable_stopped": [], "snapshot_id": None, "accept_data_loss": False}}
        result = service.handle(json.dumps({"version": 1, "method": "submit", "params": params}).encode() + b'\n', 10001)
        self.assertEqual(result, {"ok": False, "error": "not_configured"})
        self.assertFalse(self.store.jobs())

    def test_socket_path_rejects_symlink_even_before_binding(self):
        with patch.object(Path, "is_symlink", return_value=True), self.assertRaises(self.m.ServiceError):
            self.m.verify_socket_parent(Path(self.temp.name) / "public/control.sock")

    @unittest.skipUnless(sys.platform == "linux" and getattr(os, "geteuid", lambda: 1)() == 0, "requires Linux root and SO_PEERCRED")
    def test_real_linux_peer_credentials_and_private_socket(self):
        import socket
        import threading
        public = Path(self.temp.name) / "public"
        public.mkdir(mode=0o750)
        os.chown(public, 0, 10001)
        stop = threading.Event()
        ready = threading.Event()
        server = self.m.UnixServer(self.m.MaintenanceService(self.store), public / "control.sock")
        thread = threading.Thread(target=server.serve, args=(stop, ready), daemon=True)
        thread.start()
        try:
            self.assertTrue(ready.wait(3))
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(3)
                client.connect(str(public / "control.sock"))
                client.sendall(b'{"version":1,"method":"jobs","params":{}}\n')
                result = json.loads(client.recv(65536))
            self.assertEqual(result, {"ok": True, "result": []})
            self.assertEqual((public / "control.sock").stat().st_mode & 0o777, 0o660)
        finally:
            stop.set()
            thread.join(5)


if __name__ == "__main__":
    unittest.main()
