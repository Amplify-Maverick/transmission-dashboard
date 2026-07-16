"""Tests for the tunnel auto-recovery watchdog (app._tunnel_recovery_tick).

The watchdog must:
  - only act on down verdicts a bounce can fix (stale/missing handshake);
  - wait TUNNEL_RECOVERY_AFTER_SEC of continuous outage before acting;
  - honour the cooldown between attempts;
  - stop after TUNNEL_RECOVERY_MAX_ATTEMPTS and reset once the tunnel is up.

The tick is driven directly with an injected `now` and a patched
_cached_tunnel_check, so no threads, subprocesses, or sleeps are involved.
"""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("USE_MOCK", "true")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ.setdefault("TUNNEL_IFACE", "wg-test")

import app  # noqa: E402
import config  # noqa: E402


def _check(status, reason):
    return {"status": status, "reason": reason}


class TunnelRecoveryTests(unittest.TestCase):
    def setUp(self):
        # Fresh watchdog state and deterministic knobs for every test.
        app._tunnel_recovery.update(
            down_since=None, attempts=0, last_attempt_mono=None,
            last_attempt_at=None, last_result=None, gave_up=False,
        )
        self.p_after = patch.object(config, "TUNNEL_RECOVERY_AFTER_SEC", 300.0)
        self.p_cool = patch.object(config, "TUNNEL_RECOVERY_COOLDOWN_SEC", 600.0)
        self.p_max = patch.object(config, "TUNNEL_RECOVERY_MAX_ATTEMPTS", 3)
        self.p_cmd = patch.object(config, "TUNNEL_RECOVERY_CMD", "true")
        for p in (self.p_after, self.p_cool, self.p_max, self.p_cmd):
            p.start()
        self.runs = []
        patch("app._run_tunnel_recovery_cmd",
              side_effect=lambda: (self.runs.append(1), "ok")[1]).start()
        patch("app.db.log_event").start()
        patch("app.time.sleep").start()
        # The post-attempt force refresh would re-probe the real host.
        self.check_calls = []
        patch("app._cached_tunnel_check",
              side_effect=self._fake_check).start()
        self.check_result = _check("down", "stale_handshake")

    def tearDown(self):
        patch.stopall()

    def _fake_check(self, force=False):
        self.check_calls.append(force)
        return dict(self.check_result)

    def test_no_attempt_before_after_window(self):
        app._tunnel_recovery_tick(now=1000.0)
        app._tunnel_recovery_tick(now=1000.0 + 299.0)
        self.assertEqual(len(self.runs), 0)

    def test_attempt_fires_after_sustained_outage(self):
        app._tunnel_recovery_tick(now=1000.0)   # outage starts
        app._tunnel_recovery_tick(now=1301.0)   # past AFTER_SEC
        self.assertEqual(len(self.runs), 1)
        self.assertEqual(app._tunnel_recovery["attempts"], 1)
        self.assertEqual(app._tunnel_recovery["last_result"], "ok")
        # The attempt forces a fresh check so the UI reflects the bounce.
        self.assertIn(True, self.check_calls)

    def test_cooldown_gates_second_attempt(self):
        app._tunnel_recovery_tick(now=1000.0)
        app._tunnel_recovery_tick(now=1301.0)   # attempt 1
        app._tunnel_recovery_tick(now=1301.0 + 599.0)  # inside cooldown
        self.assertEqual(len(self.runs), 1)
        app._tunnel_recovery_tick(now=1301.0 + 601.0)  # cooldown elapsed
        self.assertEqual(len(self.runs), 2)

    def test_gives_up_after_max_attempts(self):
        t = 1000.0
        app._tunnel_recovery_tick(now=t)
        for i in range(1, 4):
            t += 700.0
            app._tunnel_recovery_tick(now=t)
        self.assertEqual(len(self.runs), 3)
        t += 700.0
        app._tunnel_recovery_tick(now=t)  # budget exhausted
        self.assertEqual(len(self.runs), 3)
        self.assertTrue(app._tunnel_recovery["gave_up"])

    def test_up_resets_state_and_budget(self):
        app._tunnel_recovery_tick(now=1000.0)
        app._tunnel_recovery_tick(now=1301.0)
        self.assertEqual(app._tunnel_recovery["attempts"], 1)
        self.check_result = _check("up", "ok")
        app._tunnel_recovery_tick(now=1400.0)
        self.assertEqual(app._tunnel_recovery["attempts"], 0)
        self.assertIsNone(app._tunnel_recovery["down_since"])
        self.assertFalse(app._tunnel_recovery["gave_up"])

    def test_unrecoverable_reasons_never_trigger(self):
        for reason in ("iface_missing", "iface_down", "no_ipv4", "not_bound"):
            app._tunnel_recovery.update(down_since=None, attempts=0)
            self.check_result = _check("down", reason)
            app._tunnel_recovery_tick(now=1000.0)
            app._tunnel_recovery_tick(now=2000.0)
            self.assertEqual(len(self.runs), 0, f"fired on {reason}")

    def test_reason_flip_does_not_refill_budget(self):
        # stale_handshake outage burns one attempt...
        app._tunnel_recovery_tick(now=1000.0)
        app._tunnel_recovery_tick(now=1301.0)
        self.assertEqual(app._tunnel_recovery["attempts"], 1)
        # ...reason flips to something unrecoverable and back; the attempt
        # counter must survive (only "up" restores the budget).
        self.check_result = _check("down", "not_bound")
        app._tunnel_recovery_tick(now=1400.0)
        self.assertEqual(app._tunnel_recovery["attempts"], 1)
        self.check_result = _check("down", "stale_handshake")
        app._tunnel_recovery_tick(now=1500.0)  # new down_since starts here
        app._tunnel_recovery_tick(now=1500.0 + 301.0 + 600.0)
        self.assertEqual(len(self.runs), 2)
        self.assertEqual(app._tunnel_recovery["attempts"], 2)

    def test_error_status_stands_down(self):
        self.check_result = _check("error", "wg_missing")
        app._tunnel_recovery_tick(now=1000.0)
        app._tunnel_recovery_tick(now=9000.0)
        self.assertEqual(len(self.runs), 0)

    def test_state_snapshot_shape(self):
        s = app._tunnel_recovery_state()
        self.assertEqual(
            set(s),
            {"enabled", "attempts", "max_attempts",
             "last_attempt_at", "last_result", "gave_up"},
        )


if __name__ == "__main__":
    unittest.main()
