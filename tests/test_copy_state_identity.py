"""Tests for copy-state identity — binding entries to a torrent's infohash.

Transmission's numeric id is session-scoped: it is reassigned when the
daemon restarts and reused after a removal. copy_state.json is keyed by
that id, so without an identity check a "done" entry left by a deleted
torrent gets inherited by whatever torrent later lands on the same id,
and the card reads as already copied mid-download.

db.DB_PATH points at a temp file and the copy-state cache is reset per
test, so neither the real dashboard.db nor copy_state.json is touched.
"""

import os
import tempfile
import threading
import unittest
from unittest.mock import patch

os.environ.setdefault("USE_MOCK", "true")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

import app  # noqa: E402
import db  # noqa: E402

HASH_A = "a" * 40
HASH_B = "b" * 40


def _torrent(tid, name, hash_, percent_done=1.0):
    return {"id": tid, "name": name, "hashString": hash_,
            "percentDone": percent_done, "labels": []}


class CopyStateIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls._tmp.close()
        cls._orig_db_path = db.DB_PATH
        db.DB_PATH = cls._tmp.name
        # Whatever connection this thread already holds points at the real
        # dashboard.db — close it rather than orphan it behind the new
        # holder (an unclosed handle warns when it is finally collected).
        conn = getattr(db._tls, "conn", None)
        if conn is not None:
            conn.close()
        db._tls = threading.local()
        db.init()
        # Never write the real copy_state.json.
        cls._state_file = tempfile.NamedTemporaryFile(
            suffix=".json", delete=False)
        cls._state_file.close()
        cls._orig_state_file = app.COPY_STATE_FILE
        app.COPY_STATE_FILE = cls._state_file.name

    @classmethod
    def tearDownClass(cls):
        db.DB_PATH = cls._orig_db_path
        # Close before dropping the TLS holder, or the temp DB's connection
        # is only reclaimed at GC time (noisy ResourceWarning).
        conn = getattr(db._tls, "conn", None)
        if conn is not None:
            conn.close()
        db._tls = threading.local()
        app.COPY_STATE_FILE = cls._orig_state_file
        os.unlink(cls._tmp.name)
        os.unlink(cls._state_file.name)

    def setUp(self):
        self.client = app.app.test_client()
        with self.client.session_transaction() as s:
            s["logged_in"] = True
        app._copy_state_cache = {}
        app._copy_state_last_flush = 0.0
        app._legacy_copied_names = None
        with db._tx() as c:
            c.execute("DELETE FROM copy_history")

    def _states(self):
        return app.load_copy_state()

    # ---- hash-bearing entries ----

    def test_entry_survives_when_hash_matches(self):
        app.update_copy_entry(7, status="done", hash=HASH_A,
                              dest_path="/mnt/shows/Show")
        app._gc_state_for_live_torrents([_torrent(7, "Show.S01", HASH_A)])
        self.assertEqual(self._states()["7"]["status"], "done")

    def test_entry_dropped_when_id_was_reassigned(self):
        # id 7 copied Show.S01; after a daemon restart id 7 is a different,
        # still-downloading torrent. The old "done" must not stick to it.
        app.update_copy_entry(7, status="done", hash=HASH_A,
                              dest_path="/mnt/shows/Show")
        app._gc_state_for_live_torrents(
            [_torrent(7, "Other.S12", HASH_B, percent_done=0.4)])
        self.assertNotIn("7", self._states())

    def test_entry_kept_when_daemon_reports_no_hash(self):
        # A missing hashString is not evidence of reassignment.
        app.update_copy_entry(7, status="done", hash=HASH_A)
        app._gc_state_for_live_torrents([
            {"id": 7, "name": "Show.S01", "percentDone": 1.0},
        ])
        self.assertIn("7", self._states())

    def test_entry_dropped_when_torrent_is_gone(self):
        app.update_copy_entry(7, status="done", hash=HASH_A)
        app._gc_state_for_live_torrents([_torrent(9, "Other", HASH_B)])
        self.assertNotIn("7", self._states())

    def test_active_copy_is_never_reclaimed(self):
        app.update_copy_entry(7, status="copying", hash=HASH_A)
        with app._active_copies_lock:
            app._active_copies[7] = {"cancel": threading.Event(), "proc": None}
        try:
            app._gc_state_for_live_torrents([])
        finally:
            with app._active_copies_lock:
                app._active_copies.pop(7, None)
        self.assertIn("7", self._states())

    # ---- legacy entries written before the hash was recorded ----

    def test_legacy_done_kept_when_history_corroborates_it(self):
        db.record_copy(7, "Show.S01", status="done", finished_at="2026-01-01")
        app.update_copy_entry(7, status="done", dest_path="/mnt/shows/Show")
        app._gc_state_for_live_torrents([_torrent(7, "Show.S01", HASH_A)])
        states = self._states()
        self.assertEqual(states["7"]["status"], "done")
        # ...and adopted the hash so it isn't re-validated next sweep.
        self.assertEqual(states["7"]["hash"], HASH_A)

    def test_legacy_done_dropped_when_history_names_another_torrent(self):
        db.record_copy(7, "Show.S01", status="done", finished_at="2026-01-01")
        app.update_copy_entry(7, status="done", dest_path="/mnt/shows/Show")
        app._gc_state_for_live_torrents(
            [_torrent(7, "Other.S12", HASH_B, percent_done=0.4)])
        self.assertNotIn("7", self._states())

    def test_legacy_non_done_entry_is_left_alone(self):
        # An error row can't produce a false "sent" chip, and dropping it
        # would erase a message the user hasn't read yet.
        app.update_copy_entry(7, status="error", error_message="no space")
        app._gc_state_for_live_torrents([_torrent(7, "Other.S12", HASH_B)])
        self.assertEqual(self._states()["7"]["status"], "error")

    # ---- the copied-check must not act on a stale or absent record ----

    def test_check_refuses_to_name_search_an_incomplete_torrent(self):
        cfg = {"host": "media.example", "user": "mediauser", "port": 22,
               "folders": [{"name": "Shows", "path": "/mnt/shows"}]}
        detail = _torrent(7, "Two.and.a.Half.Men.S12", HASH_B,
                          percent_done=0.4)
        with patch.object(app, "_read_media_config", return_value=cfg), \
             patch.object(app.client, "get_torrent_detail",
                          return_value=detail), \
             patch.object(app, "_remote_check_presence") as probe:
            res = self.client.get("/api/torrent/7/copy/check")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["state"], "unknown")
        probe.assert_not_called()

    def test_check_ignores_dest_paths_recorded_for_another_torrent(self):
        app.update_copy_entry(7, status="done", hash=HASH_A,
                              dest_targets=["/mnt/shows/Show/Season 01"],
                              dest_path="/mnt/shows/Show")
        cfg = {"host": "media.example", "user": "mediauser", "port": 22,
               "folders": [{"name": "Shows", "path": "/mnt/shows"}]}
        detail = _torrent(7, "Other.S12", HASH_B)
        with patch.object(app, "_read_media_config", return_value=cfg), \
             patch.object(app.client, "get_torrent_detail",
                          return_value=detail), \
             patch.object(app, "_remote_check_presence",
                          return_value=(False, "")) as probe:
            res = self.client.get("/api/torrent/7/copy/check")
        self.assertEqual(res.status_code, 200)
        # The other torrent's exact paths must not be probed.
        exact_paths = probe.call_args.args[3]
        self.assertEqual(exact_paths, [])
        self.assertEqual(probe.call_args.args[5], ["Other.S12"])


if __name__ == "__main__":
    unittest.main()
