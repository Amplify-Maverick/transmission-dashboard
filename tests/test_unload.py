"""Tests for the unload/load flow — parking stopped torrents outside
Transmission (magnet kept in the dashboard DB) and re-adding them later.

The transmission client is patched per-call, and db.DB_PATH is pointed
at a temp file so the real dashboard.db is never touched.
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

MAGNET = "magnet:?xt=urn:btih:aabbccdd&dn=Test"

DETAIL_STOPPED = {
    "id": 1,
    "status": 0,
    "hashString": "aabbccdd",
    "magnetLink": MAGNET,
    "name": "Test Torrent",
    "totalSize": 1000,
    "percentDone": 0.25,
    "downloadDir": "/downloads",
    "labels": ["tv"],
}


class UnloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Redirect the module-level DB to a temp file. _conn caches one
        # connection per thread, so reset the TLS holder too.
        cls._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls._tmp.close()
        cls._orig_db_path = db.DB_PATH
        db.DB_PATH = cls._tmp.name
        db._tls = threading.local()
        db.init()

    @classmethod
    def tearDownClass(cls):
        db.DB_PATH = cls._orig_db_path
        db._tls = threading.local()
        os.unlink(cls._tmp.name)

    def setUp(self):
        self.client = app.app.test_client()
        with self.client.session_transaction() as s:
            s["logged_in"] = True
        # Fresh table per test.
        with db._tx() as c:
            c.execute("DELETE FROM unloaded_torrents")

    def _unload(self, detail=DETAIL_STOPPED):
        with patch.object(app.client, "get_torrent_detail",
                          return_value=dict(detail)), \
             patch.object(app.client, "remove") as remove:
            res = self.client.post("/api/torrent/1/unload")
        return res, remove

    def test_unload_stores_row_and_removes_from_daemon(self):
        res, remove = self._unload()
        self.assertEqual(res.status_code, 200)
        remove.assert_called_once_with(1, delete_local_data=False)
        rows = db.list_unloaded_torrents()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["hash"], "aabbccdd")
        self.assertEqual(row["magnet_link"], MAGNET)
        self.assertEqual(row["download_dir"], "/downloads")
        self.assertEqual(row["labels"], ["tv"])
        self.assertEqual(row["total_size"], 1000)
        self.assertAlmostEqual(row["percent_done"], 0.25)

    def test_unload_rejects_running_torrent(self):
        detail = dict(DETAIL_STOPPED, status=4)
        res, remove = self._unload(detail)
        self.assertEqual(res.status_code, 409)
        remove.assert_not_called()
        self.assertEqual(db.list_unloaded_torrents(), [])

    def test_unload_rolls_back_row_when_remove_fails(self):
        with patch.object(app.client, "get_torrent_detail",
                          return_value=dict(DETAIL_STOPPED)), \
             patch.object(app.client, "remove",
                          side_effect=RuntimeError("daemon down")):
            res = self.client.post("/api/torrent/1/unload")
        self.assertEqual(res.status_code, 500)
        self.assertEqual(db.list_unloaded_torrents(), [])

    def test_list_endpoint(self):
        self._unload()
        res = self.client.get("/api/unloaded")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["unloaded"]), 1)
        self.assertEqual(data["unloaded"][0]["name"], "Test Torrent")

    def test_start_readds_with_dir_and_labels_then_deletes_row(self):
        self._unload()
        uid = db.list_unloaded_torrents()[0]["id"]
        added = {"result": "success",
                 "arguments": {"torrent-added": {"id": 42}}}
        with patch.object(app.client, "add_magnet",
                          return_value=added) as add, \
             patch.object(app.client, "set_labels") as set_labels:
            res = self.client.post(f"/api/unloaded/{uid}/start")
        self.assertEqual(res.status_code, 200)
        add.assert_called_once_with(MAGNET, paused=False,
                                    download_dir="/downloads")
        set_labels.assert_called_once_with(42, ["tv"])
        self.assertEqual(db.list_unloaded_torrents(), [])

    def test_start_missing_row_404(self):
        res = self.client.post("/api/unloaded/9999/start")
        self.assertEqual(res.status_code, 404)

    def test_forget_deletes_row(self):
        self._unload()
        uid = db.list_unloaded_torrents()[0]["id"]
        res = self.client.post(f"/api/unloaded/{uid}/forget")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(db.list_unloaded_torrents(), [])

    def test_unload_twice_upserts_single_row(self):
        self._unload()
        self._unload()
        self.assertEqual(len(db.list_unloaded_torrents()), 1)


if __name__ == "__main__":
    unittest.main()
