from __future__ import annotations

import socket
import unittest

from app.security import network_isolation as ni


class NetworkIsolationTest(unittest.TestCase):
    def setUp(self) -> None:
        ni._reset_for_tests()
        ni.enforce_offline_mode()
        self.addCleanup(ni._reset_for_tests)

    def test_localhost_resolution_allowed(self) -> None:
        # Should not raise.
        info = socket.getaddrinfo("127.0.0.1", 0)
        self.assertTrue(info)

    def test_external_host_blocked_at_getaddrinfo(self) -> None:
        with self.assertRaises(OSError) as ctx:
            socket.getaddrinfo("example.com", 80)
        self.assertIn("blocked", str(ctx.exception).lower())

    def test_external_host_blocked_at_connect(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with self.assertRaises(OSError):
                sock.connect(("1.1.1.1", 443))
        finally:
            sock.close()

    def test_private_network_blocked_by_default(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with self.assertRaises(OSError):
                sock.connect(("10.0.0.5", 80))
        finally:
            sock.close()

    def test_add_allowed_host_cidr(self) -> None:
        ni.add_allowed_host("10.0.0.0/8")
        # getaddrinfo of a numeric address in the allowed net should pass.
        socket.getaddrinfo("10.0.0.5", 0)  # does not raise

    def test_is_enforced_flag(self) -> None:
        self.assertTrue(ni.is_enforced())


if __name__ == "__main__":
    unittest.main()
