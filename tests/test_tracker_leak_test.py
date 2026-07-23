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
