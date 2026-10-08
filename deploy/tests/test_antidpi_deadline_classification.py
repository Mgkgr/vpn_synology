"""Regression: socket deadline overhead must not erase an observed timeout."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from antidpi import probe
from maintenance.strategy_state import validated_observation


class DeadlineClassificationTests(unittest.TestCase):
    def test_deadline_timeout_keeps_transport_evidence_without_invalid_latency(self):
        clock = [100.0]

        def timed_out(address, *, timeout):
            self.assertEqual(address, ('162.159.135.232', 443))
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 10)
            clock[0] = 110.004
            raise TimeoutError('private transport detail must not be returned')

        with patch.object(probe.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(probe.socket, 'create_connection', side_effect=timed_out):
            result = probe.https_probe('discord.com', '162.159.135.232')
        self.assertEqual(result, {
            'verdict': 'transport_error', 'reason': 'timeout',
            'latency_ms': None, 'http_status': None,
        })
        validated_observation(dict(result, checked_at=1000, identity='a' * 64,
                                   context_id='b' * 64, infrastructure_ok=True))

    def test_late_success_does_not_become_valid_strategy_evidence(self):
        clock = [100.0]

        class Peer:
            def settimeout(self, value):
                if not 0 < value <= 10:
                    raise AssertionError('unbounded timeout')

            def sendall(self, value):
                pass

            def recv(self, size):
                clock[0] = 110.004
                return b'HTTP/1.1 200 OK\r\n\r\n'

            def close(self):
                pass

        peer = Peer()
        with patch.object(probe.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(probe.socket, 'create_connection', return_value=peer), \
                patch.object(probe.ssl, 'create_default_context') as context:
            context.return_value.wrap_socket.return_value = peer
            result = probe.https_probe('discord.com', '162.159.135.232')
        self.assertEqual(result, {
            'verdict': 'unknown', 'reason': 'probe_failed',
            'latency_ms': None, 'http_status': None,
        })


if __name__ == '__main__':
    unittest.main()
