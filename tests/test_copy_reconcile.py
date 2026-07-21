"""Tests for startup reconciliation of orphaned 'copying' entries.

A copy runs in a thread inside the web worker, and its rsync is a child of
that process — both die when the service restarts. Terminal states are only
written by that thread, so a restart mid-copy leaves 'copying' on disk
forever and the UI hides the Copy button behind a transfer that no longer
exists. reconcile_interrupted_copies() rewrites those entries at startup.

These tests drive the state cache directly; no SSH or rsync is involved.
"""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("USE_MOCK", "true")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

import app  # noqa: E402


class ReconcileInterruptedCopiesTests(unittest.TestCase):
    def setUp(self):
        # Swap the module-level cache for a fixture and neutralise the disk
        # flush, so tests never touch the real copy_state.json.
        self._flush = patch.object(app, "_flush_copy_state_unlocked")
        self._flush.start()
        self.addCleanup(self._flush.stop)
        self._orig_cache = app._copy_state_cache
        self.addCleanup(lambda: setattr(app, "_copy_state_cache", self._orig_cache))

    def _seed(self, state):
        app._copy_state_cache = state

    def test_copying_entry_becomes_interrupted(self):
        self._seed({"44": {"id": 44, "status": "copying", "progress_pct": 90,
                           "rate": "41.33MB/s", "eta_seconds": 37}})

        reconciled = app.reconcile_interrupted_copies()

        self.assertEqual(reconciled, ["44"])
        entry = app._copy_state_cache["44"]
        self.assertEqual(entry["status"], "interrupted")
        self.assertIsNotNone(entry["finished_at"])
        # Stale throughput readings must not survive — they describe a
        # process that no longer exists.
        self.assertIsNone(entry["rate"])
        self.assertIsNone(entry["eta_seconds"])

    def test_message_mentions_progress_and_resume(self):
        self._seed({"44": {"id": 44, "status": "copying", "progress_pct": 90}})

        app.reconcile_interrupted_copies()

        msg = app._copy_state_cache["44"]["error_message"]
        self.assertIn("90%", msg)
        self.assertIn("resumes", msg)

    def test_progress_omitted_when_never_recorded(self):
        self._seed({"7": {"id": 7, "status": "copying"}})

        app.reconcile_interrupted_copies()

        entry = app._copy_state_cache["7"]
        self.assertEqual(entry["status"], "interrupted")
        self.assertNotIn("reached", entry["error_message"])

    def test_terminal_states_are_left_alone(self):
        self._seed({
            "1": {"id": 1, "status": "done", "progress_pct": 100},
            "2": {"id": 2, "status": "error", "error_message": "disk full"},
            "3": {"id": 3, "status": "cancelled"},
            "4": {"id": 4, "status": "idle"},
        })

        reconciled = app.reconcile_interrupted_copies()

        self.assertEqual(reconciled, [])
        self.assertEqual(app._copy_state_cache["1"]["status"], "done")
        self.assertEqual(app._copy_state_cache["2"]["error_message"], "disk full")
        self.assertEqual(app._copy_state_cache["3"]["status"], "cancelled")
        self.assertEqual(app._copy_state_cache["4"]["status"], "idle")

    def test_only_copying_entries_touched_in_mixed_state(self):
        self._seed({
            "1": {"id": 1, "status": "done"},
            "44": {"id": 44, "status": "copying", "progress_pct": 90},
            "45": {"id": 45, "status": "copying", "progress_pct": 12},
        })

        reconciled = app.reconcile_interrupted_copies()

        self.assertEqual(sorted(reconciled), ["44", "45"])
        self.assertEqual(app._copy_state_cache["1"]["status"], "done")
        self.assertEqual(app._copy_state_cache["44"]["status"], "interrupted")
        self.assertEqual(app._copy_state_cache["45"]["status"], "interrupted")

    def test_is_idempotent(self):
        """Multiple workers import the module concurrently; a second pass
        must not rewrite finished_at or re-report the entry."""
        self._seed({"44": {"id": 44, "status": "copying", "progress_pct": 90}})
        app.reconcile_interrupted_copies()
        first_finished = app._copy_state_cache["44"]["finished_at"]

        reconciled = app.reconcile_interrupted_copies()

        self.assertEqual(reconciled, [])
        self.assertEqual(app._copy_state_cache["44"]["finished_at"], first_finished)

    def test_empty_state_is_a_noop(self):
        self._seed({})
        self.assertEqual(app.reconcile_interrupted_copies(), [])


class InterruptedSeverityTests(unittest.TestCase):
    def test_interrupted_is_a_warning_not_an_error(self):
        # Nothing was corrupted and retrying resumes, so an interrupted copy
        # must not be logged at the same severity as a failed one.
        self.assertEqual(app._copy_severity("interrupted"), "warn")
        self.assertEqual(app._copy_severity("error"), "error")
        self.assertEqual(app._copy_severity("done"), "info")
        self.assertEqual(app._copy_severity("cancelled"), "info")


class CopyTransportTimeoutTests(unittest.TestCase):
    """The long-lived rsync transport must detect a peer that goes away
    after the connection is established, not just one that never answers."""

    def test_transport_sets_connect_and_keepalive_timeouts(self):
        opts = app._copy_ssh_transport(2222)

        self.assertIn("-o ConnectTimeout=", opts)
        self.assertIn("-o ServerAliveInterval=", opts)
        self.assertIn("-o ServerAliveCountMax=", opts)
        self.assertIn("-p 2222", opts)
        self.assertIn("-o BatchMode=yes", opts)

    def test_keepalive_gives_up_within_a_few_minutes(self):
        # Interval * CountMax is how long rsync stays wedged on a dead peer.
        # Long enough to ride out a blip, short enough to surface as an error.
        budget = app._COPY_SSH_ALIVE_INTERVAL * app._COPY_SSH_ALIVE_COUNT_MAX
        self.assertGreaterEqual(budget, 60)
        self.assertLessEqual(budget, 300)

    def test_rsync_io_timeout_is_longer_than_ssh_keepalive(self):
        # ssh keepalive is the primary detector; rsync's --timeout is the
        # backstop for a live transport with no data flowing. If it fired
        # first it would abort copies that are merely checksumming.
        self.assertGreater(
            app._COPY_RSYNC_IO_TIMEOUT,
            app._COPY_SSH_ALIVE_INTERVAL * app._COPY_SSH_ALIVE_COUNT_MAX,
        )


if __name__ == "__main__":
    unittest.main()
