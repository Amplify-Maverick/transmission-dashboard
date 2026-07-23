"""Tests for the opt-in end-to-end tracker leak test.

The web endpoints are thin; the safety-relevant pieces are the pure helpers:
extracting IPs from tracker announce messages, and the verdict logic that
must never call a leak a pass (or silently pass what it couldn't judge).
"""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("USE_MOCK", "true")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

import app  # noqa: E402


class ExtractPublicIpsTests(unittest.TestCase):
    def test_torguard_style_message(self):
        msg = "Success! Your torrent client IP is: 93.184.216.34"
        self.assertEqual(app._extract_public_ips(msg), ["93.184.216.34"])

    def test_ipv6_in_message(self):
        msg = "announce ok; seen from 2606:4700:abcd::17."
        self.assertEqual(app._extract_public_ips(msg), ["2606:4700:abcd::17"])

    def test_private_and_loopback_ips_ignored(self):
        msg = "peer 192.168.1.10 via 127.0.0.1 and fe80::1 and 10.64.0.2"
        self.assertEqual(app._extract_public_ips(msg), [])

    def test_documentation_ranges_are_not_global(self):
        """TEST-NET / 2001:db8 doc addresses are not internet-routable — the
        extractor must drop them like any other non-global address."""
        msg = "peers 203.0.113.9 and 198.51.100.7 and 2001:db8::1"
        self.assertEqual(app._extract_public_ips(msg), [])

    def test_timestamps_and_versions_not_mistaken_for_ips(self):
        msg = "at 12:34:56 client 4.0.5 announced (build 2026.07.22.01)"
        self.assertEqual(app._extract_public_ips(msg), [])

    def test_trailing_punctuation_stripped(self):
        msg = "your IP: 93.184.216.9."
        self.assertEqual(app._extract_public_ips(msg), ["93.184.216.9"])

    def test_empty_and_none(self):
        self.assertEqual(app._extract_public_ips(""), [])
        self.assertEqual(app._extract_public_ips(None), [])


class VerdictTests(unittest.TestCase):
    def test_pass_when_seen_matches_expected_exit(self):
        v, problems = app._tracker_test_verdict(
            ["198.51.100.7"], "198.51.100.7", None, [])
        self.assertEqual(v, "pass")
        self.assertEqual(problems, [])

    def test_leak_when_seen_differs_from_expected_exit(self):
        v, problems = app._tracker_test_verdict(
            ["203.0.113.50"], "198.51.100.7", None, [])
        self.assertEqual(v, "leak")
        self.assertIn("203.0.113.50", problems[0])

    def test_leak_when_tracker_saw_hosts_bare_ipv6(self):
        """Seeing the host's own bare global v6 is a definitive leak even when
        the expected-exit fetch failed entirely."""
        v, problems = app._tracker_test_verdict(
            ["2001:db8:abcd::1"], None, None, ["2001:db8:abcd::1"])
        self.assertEqual(v, "leak")
        self.assertIn("bare IPv6", problems[0])

    def test_inconclusive_when_nothing_seen(self):
        v, problems = app._tracker_test_verdict([], "198.51.100.7", None, [])
        self.assertEqual(v, "inconclusive")

    def test_inconclusive_when_no_expected_exit_to_compare(self):
        """Tracker saw an IP but the exit fetch failed — must NOT pass."""
        v, problems = app._tracker_test_verdict(["198.51.100.7"], None, None, [])
        self.assertEqual(v, "inconclusive")
        self.assertIn("198.51.100.7", problems[0])

    def test_pass_requires_both_families_to_match(self):
        v, _ = app._tracker_test_verdict(
            ["198.51.100.7", "2001:db8:1::5"],
            "198.51.100.7", "2001:db8:1::5", [])
        self.assertEqual(v, "pass")
        v, problems = app._tracker_test_verdict(
            ["198.51.100.7", "2001:db8:bad::5"],
            "198.51.100.7", "2001:db8:1::5", [])
        self.assertEqual(v, "leak")

    def test_v6_comparison_case_insensitive(self):
        v, _ = app._tracker_test_verdict(
            ["2001:DB8:1::5"].copy(), None, "2001:db8:1::5", [])
        # _extract_public_ips canonicalizes in real flow; the verdict itself
        # must still not false-leak on case.
        self.assertEqual(v, "pass")


class ScheduleDueTests(unittest.TestCase):
    def _settings(self, **over):
        base = {"enabled": True, "magnet": "magnet:?xt=urn:btih:x&tr=http://t/a",
                "interval_hours": 24}
        base.update(over)
        return base

    def test_not_due_when_disabled_or_no_magnet_or_no_interval(self):
        self.assertFalse(app._tracker_test_due(self._settings(enabled=False), None))
        self.assertFalse(app._tracker_test_due(self._settings(magnet=""), None))
        self.assertFalse(app._tracker_test_due(self._settings(interval_hours=0), None))

    def test_due_when_never_ran(self):
        self.assertTrue(app._tracker_test_due(self._settings(), None))

    def test_due_when_garbage_last_run(self):
        self.assertTrue(app._tracker_test_due(self._settings(), "not-a-date"))

    def test_due_respects_interval(self):
        from datetime import datetime, timedelta, timezone
        now = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
        recent = (now - timedelta(hours=23)).isoformat()
        old = (now - timedelta(hours=25)).isoformat()
        self.assertFalse(app._tracker_test_due(self._settings(), recent, now=now))
        self.assertTrue(app._tracker_test_due(self._settings(), old, now=now))


class StopTimingTests(unittest.TestCase):
    """The stop guard must fire on a *successful* announce (a content magnet =
    download risk), never on a merely attempted/failed one — the echo tracker
    replies with a FAILURE carrying the IP, and stopping on that early signal
    was killing the reply before it landed (the false 'no IP to check')."""

    def _state(self, **over):
        base = {"running": True, "torrent_id": 42,
                "started_at": "2026-07-22T00:00:00+00:00",
                "started_mono": app.time.monotonic(),
                "expected": None, "result": None}
        base.update(over)
        return base

    def test_failed_echo_announce_is_not_stopped(self):
        """Announce failed (echo tracker) and the IP hasn't been parsed yet:
        the torrent must keep running so the reply can be recorded — no stop."""
        with patch.object(app, "_tracker_test", self._state()), \
             patch("app.client.get_tracker_stats", return_value={
                 "id": 42,
                 "trackerStats": [{"hasAnnounced": True,
                                   "lastAnnounceSucceeded": False,
                                   "lastAnnounceResult": "connecting…"}]}), \
             patch("app.client.stop") as m_stop:
            payload = app._tracker_test_evaluate()
        m_stop.assert_not_called()
        self.assertTrue(payload["running"])

    def test_successful_announce_without_ip_is_stopped_and_concluded(self):
        """A real tracker accepted the announce (download risk) but echoed no
        IP: stop immediately and conclude inconclusive rather than waiting."""
        with patch.object(app, "_tracker_test", self._state()), \
             patch("app.client.get_tracker_stats", return_value={
                 "id": 42,
                 "trackerStats": [{"hasAnnounced": True,
                                   "lastAnnounceSucceeded": True,
                                   "lastAnnounceResult": "Success"}]}), \
             patch("app.client.stop") as m_stop, \
             patch("app.client.remove"):
            payload = app._tracker_test_evaluate()
        m_stop.assert_called_once_with(42)
        self.assertFalse(payload["running"])
        self.assertEqual(payload["result"]["verdict"], "inconclusive")

    def test_failed_echo_announce_with_ip_resolves_without_stop(self):
        """The echo tracker's failure reply carries the IP: it's parsed and the
        run resolves via the verdict path (finish removes the torrent) — the
        early stop must not have interrupted it."""
        with patch.object(app, "_tracker_test", self._state()), \
             patch("app.client.get_tracker_stats", return_value={
                 "id": 42,
                 "trackerStats": [{"hasAnnounced": True,
                                   "lastAnnounceSucceeded": False,
                                   "lastAnnounceResult":
                                       "Your IP address is 93.184.216.34"}]}), \
             patch("app._public_ip_from_source", return_value="93.184.216.34"), \
             patch("app._bare_global_ipv6_addrs", return_value=[]), \
             patch("app.config.TUNNEL_IFACE", "wg-test"), \
             patch("app.client.stop") as m_stop, \
             patch("app.client.remove"):
            payload = app._tracker_test_evaluate()
        m_stop.assert_not_called()
        self.assertFalse(payload["running"])
        self.assertEqual(payload["result"]["verdict"], "pass")

    def test_failed_expected_fetch_is_retried_not_cached(self):
        """The tracker echoed our IP, but the first expected-exit fetch fails
        (returns None). The run must NOT cache that failure and idle until the
        timeout — the next poll must re-fetch and, on success, resolve to a
        verdict. This is the regression for the 'stuck on Testing…' hang."""
        state = self._state()
        tracker = {"id": 42,
                   "trackerStats": [{"hasAnnounced": True,
                                     "lastAnnounceSucceeded": False,
                                     "lastAnnounceResult":
                                         "Your IP address is 93.184.216.34"}]}
        with patch.object(app, "_tracker_test", state), \
             patch("app.client.get_tracker_stats", return_value=tracker), \
             patch("app._bare_global_ipv6_addrs", return_value=[]), \
             patch("app.config.TUNNEL_IFACE", "wg-test"), \
             patch("app.client.stop"), \
             patch("app.client.remove"), \
             patch("app._public_ip_from_source",
                   side_effect=[None, "93.184.216.34"]) as m_fetch:
            first = app._tracker_test_evaluate()
            # Fetch failed: still running, not concluded, and the failed None
            # was not left as a permanent verdict.
            self.assertTrue(first["running"])
            second = app._tracker_test_evaluate()
        # The second poll must have re-attempted the fetch (2 calls total) and
        # the fresh success resolves the run — proving the failure wasn't cached.
        self.assertEqual(m_fetch.call_count, 2)
        self.assertFalse(second["running"])
        self.assertEqual(second["result"]["verdict"], "pass")


class SettingsParsingTests(unittest.TestCase):
    def _with_config(self, stored):
        return patch("app._read_app_config", return_value={"tracker_test": stored})

    def test_defaults_when_unconfigured(self):
        with patch("app._read_app_config", return_value={}):
            s = app._tracker_test_settings()
        self.assertFalse(s["enabled"])
        self.assertEqual(s["magnet"], "")
        self.assertEqual(s["echo_url"], app._TRACKER_TEST_DEFAULT_ECHO_URL)

    def test_stored_values_win(self):
        with self._with_config({"enabled": True,
                                "magnet": "magnet:?xt=urn:btih:abc&tr=http://x/announce",
                                "echo_url": "https://api.ipify.org"}):
            s = app._tracker_test_settings()
        self.assertTrue(s["enabled"])
        self.assertIn("magnet:?", s["magnet"])
        self.assertEqual(s["echo_url"], "https://api.ipify.org")

    def test_garbage_types_fall_back_to_defaults(self):
        with self._with_config({"enabled": "yes", "magnet": 42, "echo_url": ""}):
            s = app._tracker_test_settings()
        self.assertFalse(s["enabled"])
        self.assertEqual(s["magnet"], "")
        self.assertEqual(s["echo_url"], app._TRACKER_TEST_DEFAULT_ECHO_URL)


if __name__ == "__main__":
    unittest.main()
