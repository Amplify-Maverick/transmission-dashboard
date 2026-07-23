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
# The verdict tests exercise a *configured* indicator, so pin an interface name
# before config is imported. The unset case (indicator disabled) is covered
# explicitly by test_empty_iface_is_disabled_not_down.
os.environ.setdefault("TUNNEL_IFACE", "wg-test")

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
        # Pin the interface at the config-module level. The env-var setdefault
        # above only takes effect when this module is the first in the process
        # to import config — under full-suite discovery, an earlier test module
        # (e.g. test_copy_preflight) wins that race with TUNNEL_IFACE unset and
        # every verdict here would collapse to "disabled".
        patch.object(config, "TUNNEL_IFACE", "wg-test").start()
        self.iface = config.TUNNEL_IFACE
        # Default: wg dump succeeds with a fresh handshake. Tests that need
        # different behaviour patch over this one.
        self.p_dump = patch(
            "app._wg_show_dump",
            return_value=_dump_with_handshake_age(15),
        )
        self.p_dump.start()
        # Default: packets from the tunnel IP egress the tunnel interface
        # (routing correct). Leak tests override this. Keeps the "up" cases
        # deterministic and off the real `ip` binary.
        self.p_route = patch("app._route_egress_dev", return_value=self.iface)
        self.p_route.start()
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
        self.assertEqual(r["reason"], "iface_missing")
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
        self.assertEqual(r["reason"], "ok")
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
        self.assertEqual(r["reason"], "not_bound")
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

    def test_empty_iface_is_disabled_not_down(self):
        """An unset TUNNEL_IFACE means the operator didn't opt into live tunnel
        monitoring. It must report 'disabled' (the UI renders a muted "Tunnel
        off" chip), never a false 'down' — and must not probe wg/psutil with an
        empty interface name."""
        with patch.object(config, "TUNNEL_IFACE", ""), \
             patch("app._wg_show_dump") as m_dump, \
             patch("app.psutil.net_if_stats") as m_stats:
            r = app._do_tunnel_check()
        self.assertEqual(r["status"], "disabled")
        self.assertEqual(r["reason"], "not_configured")
        m_dump.assert_not_called()
        m_stats.assert_not_called()

    # -- leak checks beyond the IPv4 bind --

    def _patch_addrs_multi(self, mapping):
        """Patch psutil so multiple interfaces exist, each with the given list
        of (family, address) tuples. `mapping` is {iface: [(family, addr), ...]}."""
        stats = {name: _FakeStats(isup=True) for name in mapping}
        addrs = {
            name: [_FakeAddr(fam, addr) for (fam, addr) in lst]
            for name, lst in mapping.items()
        }
        return [
            patch("app.psutil.net_if_stats", return_value=stats),
            patch("app.psutil.net_if_addrs", return_value=addrs),
        ]

    def test_route_leak_when_tunnel_ip_egresses_bare_link(self):
        """Bind is correct and the handshake is fresh, but packets from the
        tunnel IP would leave via a bare interface (missing policy route).
        That's a leak the bind check can't see — must report down."""
        for p in self._patch_iface(ipv4="10.99.0.5"):
            p.start()
        self._patch_wg_output(10).start()
        self._patch_bind("10.99.0.5").start()
        # Override setUp's routing patch: egress is eth0, not the tunnel.
        patch("app._route_egress_dev", return_value="eth0").start()
        r = app._do_tunnel_check()
        self.assertEqual(r["status"], "down")
        self.assertEqual(r["reason"], "route_leak")
        self.assertIn("egress", r["error"])

    def test_ipv6_leak_when_host_has_bare_v6_and_transmission_not_bound_v6(self):
        """v4 is perfectly bound and routed, but the host has a global IPv6 on
        a bare interface and transmission's v6 bind is '::' (all). BitTorrent
        can leak the real v6 address — must report down as an IPv6 leak."""
        import socket as s
        for p in self._patch_addrs_multi({
            self.iface: [(s.AF_INET, "10.99.0.5"), (s.AF_INET6, "fc00:1111::5")],
            "eth0": [(s.AF_INET, "192.168.1.10"), (s.AF_INET6, "2001:db8:abcd::1")],
        }):
            p.start()
        self._patch_wg_output(10).start()
        self._patch_bind("10.99.0.5").start()
        patch("app.client.get_session_bind_address_ipv6", return_value="::").start()
        r = app._do_tunnel_check()
        self.assertEqual(r["status"], "down")
        self.assertEqual(r["reason"], "ipv6_leak")
        self.assertTrue(r["host_bare_ipv6"])

    def test_ipv6_safe_when_bound_to_tunnel_v6(self):
        """Same bare-v6 host, but transmission is bound to the tunnel's own
        IPv6 and it routes out the tunnel — no leak, stays up."""
        import socket as s
        tunnel_v6 = "fc00:1111::5"
        for p in self._patch_addrs_multi({
            self.iface: [(s.AF_INET, "10.99.0.5"), (s.AF_INET6, tunnel_v6)],
            "eth0": [(s.AF_INET, "192.168.1.10"), (s.AF_INET6, "2001:db8:abcd::1")],
        }):
            p.start()
        self._patch_wg_output(10).start()
        self._patch_bind("10.99.0.5").start()
        patch("app.client.get_session_bind_address_ipv6", return_value=tunnel_v6).start()
        r = app._do_tunnel_check()
        self.assertEqual(r["status"], "up", msg=r.get("error"))
        self.assertEqual(r["reason"], "ok")

    def test_ipv6_safe_when_host_has_no_global_v6(self):
        """No globally-routable IPv6 anywhere on the host → nothing to leak
        through, so the v6 check must not block a green verdict."""
        import socket as s
        for p in self._patch_addrs_multi({
            self.iface: [(s.AF_INET, "10.99.0.5")],
            "eth0": [(s.AF_INET, "192.168.1.10"), (s.AF_INET6, "fe80::1")],  # link-local only
        }):
            p.start()
        self._patch_wg_output(10).start()
        self._patch_bind("10.99.0.5").start()
        # Even a wrong v6 bind is harmless with no bare global v6 present.
        patch("app.client.get_session_bind_address_ipv6", return_value="::").start()
        r = app._do_tunnel_check()
        self.assertEqual(r["status"], "up", msg=r.get("error"))
        self.assertFalse(r["host_bare_ipv6"])

    def test_wg_binary_missing_is_error_not_down(self):
        patch.stopall()  # drop the default _wg_show_dump fake
        # stopall also dropped setUp's TUNNEL_IFACE pin — restore it.
        patch.object(config, "TUNNEL_IFACE", self.iface).start()
        patch("app._wg_show_dump", return_value=None).start()
        for p in self._patch_iface():
            p.start()
        r = app._do_tunnel_check()
        self.assertEqual(r["status"], "error")
        self.assertIn("wg", r["error"])


if __name__ == "__main__":
    unittest.main()
