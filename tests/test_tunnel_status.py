"""Tests for the tunnel-status indicator logic in app._do_tunnel_check.

The check must return:
  up    — interface exists with an IPv4, fresh handshake, AND transmission
          is bound to that interface IPv4.
  down  — any of those failing.
  error — wg binary missing, psutil exception, or bind setting unreadable.

These tests mock the three external signals (psutil, the wg dump
subprocess via _wg_show_dump, and the transmission RPC bind read) so the
verdict logic is exercised in isolation from the host running the tests.
"""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("USE_MOCK", "true")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

import app  # noqa: E402
import config  # noqa: E402


class _FakeStats:
    def __init__(self, isup=True):
        self.isup = isup


class _FakeAddr:
    def __init__(self, family, address):
        self.family = family
        self.address = address


def _addrs(ipv4):
    import socket as _socket
    if ipv4 is None:
        return []
    return [_FakeAddr(_socket.AF_INET, ipv4)]


def _dump_with_handshake_age(seconds_ago):
    """Build a parsed `_wg_show_dump` return matching one peer whose latest
    handshake happened `seconds_ago` ago. None means no handshake yet."""
    return {
        "last_handshake_seconds": seconds_ago,
        "rx_bytes": 0,
        "tx_bytes": 0,
        "endpoint": None,
    }


class TunnelCheckTests(unittest.TestCase):
    def setUp(self):
        self.iface = config.TUNNEL_IFACE
        # Default: wg dump succeeds with a fresh handshake. Tests that need
        # different behaviour patch over this one.
        self.p_dump = patch(
            "app._wg_show_dump",
            return_value=_dump_with_handshake_age(15),
        )
        self.p_dump.start()
        # _disk_target is cached at module level; reset between tests so a
        # cached "/" doesn't leak across cases.
        app._disk_target_cache["at"] = 0.0

    def tearDown(self):
        patch.stopall()

    def _patch_iface(self, *, exists=True, up=True, ipv4="10.99.0.5"):
        stats = {self.iface: _FakeStats(isup=up)} if exists else {}
        addrs = {self.iface: _addrs(ipv4)} if exists else {}
        return [
            patch("app.psutil.net_if_stats", return_value=stats),
            patch("app.psutil.net_if_addrs", return_value=addrs),
        ]

    def _patch_wg_output(self, latest_handshake_seconds_ago):
        """Override the default _wg_show_dump patch with one whose newest
        handshake matches the test's expected age."""
        return patch(
            "app._wg_show_dump",
            return_value=_dump_with_handshake_age(latest_handshake_seconds_ago),
        )

    def _patch_bind(self, bind):
        """Patch the transmission client's bind read. bind=None simulates an
        unreadable session; otherwise it's the bind-address-ipv4 value."""
        if bind is None:
            return patch(
                "app.client.get_session_bind_address",
                side_effect=Exception("rpc unavailable"),
            )
        return patch(
            "app.client.get_session_bind_address",
            return_value=bind,
        )

    # -- the four cases the spec calls out --

    def test_interface_missing(self):
        for p in self._patch_iface(exists=False):
            p.start()
        self._patch_wg_output(10).start()
        self._patch_bind("0.0.0.0").start()
        r = app._do_tunnel_check()
        self.assertEqual(r["status"], "down")
        self.assertIn("not found", r["error"])

    def test_interface_present_no_handshake(self):
        for p in self._patch_iface():
            p.start()
        self._patch_wg_output(None).start()
        self._patch_bind("10.99.0.5").start()
        r = app._do_tunnel_check()
        self.assertEqual(r["status"], "down")
        self.assertIn("handshake", r["error"])

    def test_interface_present_stale_handshake(self):
        stale = config.WG_HANDSHAKE_STALE_SEC + 60
        for p in self._patch_iface():
            p.start()
        self._patch_wg_output(stale).start()
        self._patch_bind("10.99.0.5").start()
        r = app._do_tunnel_check()
        self.assertEqual(r["status"], "down")
        self.assertIn("handshake", r["error"])
        self.assertGreaterEqual(r["last_handshake_seconds"], stale - 1)

    def test_interface_present_fresh_handshake_and_bound(self):
        for p in self._patch_iface(ipv4="10.99.0.5"):
            p.start()
        self._patch_wg_output(15).start()
        self._patch_bind("10.99.0.5").start()
        r = app._do_tunnel_check()
        self.assertEqual(r["status"], "up", msg=r.get("error"))
        self.assertEqual(r["interface_address"], "10.99.0.5")
        self.assertTrue(r["transmission_bound"])

    # -- additional cases the spec implies --

    def test_fresh_handshake_but_transmission_not_bound_to_tunnel(self):
        """Tunnel is healthy but transmission is bound to 0.0.0.0 — traffic
        leaks out the bare ISP link. Must report down so the user notices."""
        for p in self._patch_iface(ipv4="10.99.0.5"):
            p.start()
        self._patch_wg_output(15).start()
        self._patch_bind("0.0.0.0").start()
        r = app._do_tunnel_check()
        self.assertEqual(r["status"], "down")
        self.assertIn("transmission bound", r["error"])

    def test_tunnel_ip_changed_does_not_break_check(self):
        """Regression: previously the check binds to a hardcoded TUNNEL_IP.
        The new check derives the IP dynamically — feeding a different IP
        than any old default must still be reported as up if bind matches."""
        new_ip = "10.77.42.99"
        for p in self._patch_iface(ipv4=new_ip):
            p.start()
        self._patch_wg_output(5).start()
        self._patch_bind(new_ip).start()
        r = app._do_tunnel_check()
        self.assertEqual(r["status"], "up", msg=r.get("error"))
        self.assertEqual(r["interface_address"], new_ip)

    def test_wg_binary_missing_is_error_not_down(self):
        patch.stopall()  # drop the default _wg_show_dump fake
        patch("app._wg_show_dump", return_value=None).start()
        for p in self._patch_iface():
            p.start()
        r = app._do_tunnel_check()
        self.assertEqual(r["status"], "error")
        self.assertIn("wg", r["error"])


if __name__ == "__main__":
    unittest.main()
