import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import app

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _make_video(path, seconds):
    """Encode a real, tiny video so ffprobe has something to read."""
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc=size=64x64:rate=5:duration={seconds}",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         path],
        check=True, capture_output=True,
    )


def _files_row(tmp, rel, length, completed, thash="abc"):
    return [{
        "id": 1,
        "hashString": thash,
        "downloadDir": tmp,
        "name": os.path.dirname(rel) or rel,
        "files": [{"name": rel, "length": length, "bytesCompleted": completed}],
    }]


class TestProbeTarget(unittest.TestCase):
    """Picking which file on disk to probe."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _target(self, rows, incomplete_dir=None, expect_hash="abc"):
        with mock.patch.object(app.client, "get_torrent_files", return_value=rows,
                               create=True):
            return app._bitrate_probe_target(1, expect_hash, incomplete_dir)

    def test_picks_largest_video_and_reports_completion(self):
        os.makedirs(os.path.join(self.tmp, "Show"))
        for name, size in (("small.mkv", 10), ("big.mkv", 99)):
            with open(os.path.join(self.tmp, "Show", name), "wb") as f:
                f.write(b"\0" * size)
        rows = [{
            "id": 1, "hashString": "abc", "downloadDir": self.tmp, "name": "Show",
            "files": [
                {"name": "Show/small.mkv", "length": 10, "bytesCompleted": 10},
                {"name": "Show/big.mkv", "length": 99, "bytesCompleted": 99},
            ],
        }]
        path, length, complete = self._target(rows)
        self.assertEqual(path, os.path.join(self.tmp, "Show", "big.mkv"))
        self.assertEqual(length, 99)
        self.assertTrue(complete)

    def test_finds_part_suffixed_file_and_marks_it_incomplete(self):
        open(os.path.join(self.tmp, "m.mkv.part"), "wb").close()
        rows = _files_row(self.tmp, "m.mkv", 1000, 400)
        path, length, complete = self._target(rows)
        self.assertTrue(path.endswith("m.mkv.part"))
        self.assertEqual(length, 1000)
        self.assertFalse(complete)

    def test_falls_back_to_incomplete_dir(self):
        inc = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, inc, True)
        open(os.path.join(inc, "m.mkv.part"), "wb").close()
        rows = _files_row(self.tmp, "m.mkv", 1000, 400)
        path, _, _ = self._target(rows, incomplete_dir=inc)
        self.assertEqual(path, os.path.join(inc, "m.mkv.part"))

    def test_skips_samples_and_extras(self):
        os.makedirs(os.path.join(self.tmp, "Extras"))
        open(os.path.join(self.tmp, "sample.mkv"), "wb").close()
        open(os.path.join(self.tmp, "Extras", "bloopers.mkv"), "wb").close()
        rows = [{
            "id": 1, "hashString": "abc", "downloadDir": self.tmp, "name": "M",
            "files": [
                {"name": "sample.mkv", "length": 500, "bytesCompleted": 500},
                {"name": "Extras/bloopers.mkv", "length": 900,
                 "bytesCompleted": 900},
            ],
        }]
        self.assertIsNone(self._target(rows))

    def test_ignores_non_video_files(self):
        open(os.path.join(self.tmp, "readme.nfo"), "wb").close()
        rows = _files_row(self.tmp, "readme.nfo", 500, 500)
        self.assertIsNone(self._target(rows))

    def test_rejects_id_now_held_by_a_different_torrent(self):
        # Ids are reassigned on daemon restart; probing anyway would report
        # some other torrent's bitrate under this card.
        open(os.path.join(self.tmp, "m.mkv"), "wb").close()
        rows = _files_row(self.tmp, "m.mkv", 100, 100, thash="different")
        self.assertIsNone(self._target(rows))

    def test_missing_file_on_disk_yields_nothing(self):
        rows = _files_row(self.tmp, "gone.mkv", 100, 100)
        self.assertIsNone(self._target(rows))


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe not installed")
class TestComputeBitrate(unittest.TestCase):
    """End-to-end over a real encoded file."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _compute(self, rows):
        with mock.patch.object(app.client, "get_torrent_files", return_value=rows,
                               create=True):
            return app._compute_bitrate(1, "abc", None)

    def test_bitrate_uses_declared_size_not_bytes_on_disk(self):
        path = os.path.join(self.tmp, "movie.mkv")
        _make_video(path, 40)
        declared = 40 * 1_000_000  # pretend the finished file is 40 MB
        got = self._compute(_files_row(self.tmp, "movie.mkv", declared, declared))
        self.assertIsNotNone(got)
        bps, nbytes, duration = got
        self.assertEqual(nbytes, declared)
        self.assertAlmostEqual(duration, 40, delta=1.5)
        self.assertAlmostEqual(bps, declared * 8.0 / duration, delta=1)

    def test_incomplete_mkv_is_probed_from_its_header(self):
        # Matroska carries duration up front, so a partial download still
        # reports the full runtime — that's the whole point of showing this
        # while a torrent is still going.
        path = os.path.join(self.tmp, "movie.mkv.part")
        _make_video(os.path.join(self.tmp, "src.mkv"), 40)
        shutil.copyfile(os.path.join(self.tmp, "src.mkv"), path)
        os.remove(os.path.join(self.tmp, "src.mkv"))
        got = self._compute(_files_row(self.tmp, "movie.mkv", 40_000_000, 1_000))
        self.assertIsNotNone(got)
        self.assertAlmostEqual(got[2], 40, delta=1.5)

    def test_incomplete_avi_is_skipped(self):
        # ffprobe estimates AVI duration from the bytes it can see, so a
        # partial file would report a short runtime and a bogus bitrate.
        path = os.path.join(self.tmp, "movie.avi")
        _make_video(path, 40)
        self.assertIsNone(
            self._compute(_files_row(self.tmp, "movie.avi", 40_000_000, 1_000))
        )

    def test_complete_avi_is_probed(self):
        path = os.path.join(self.tmp, "movie.avi")
        _make_video(path, 40)
        got = self._compute(
            _files_row(self.tmp, "movie.avi", 40_000_000, 40_000_000)
        )
        self.assertIsNotNone(got)
        self.assertAlmostEqual(got[2], 40, delta=1.5)

    def test_clip_shorter_than_the_sanity_floor_is_rejected(self):
        path = os.path.join(self.tmp, "movie.mkv")
        _make_video(path, 5)
        self.assertIsNone(
            self._compute(_files_row(self.tmp, "movie.mkv", 40_000_000, 40_000_000))
        )

    def test_unreadable_file_yields_nothing(self):
        with open(os.path.join(self.tmp, "movie.mkv"), "wb") as f:
            f.write(b"\0" * 4096)
        self.assertIsNone(
            self._compute(_files_row(self.tmp, "movie.mkv", 40_000_000, 40_000_000))
        )


class TestBitrateEndpoint(unittest.TestCase):
    """Cache/queue behaviour of /api/torrents/bitrates."""

    def setUp(self):
        app.app.config["TESTING"] = True
        self.client = app.app.test_client()
        with self.client.session_transaction() as s:
            s["logged_in"] = True
        with app._bitrate_lock:
            app._bitrate_cache.clear()
            app._bitrate_pending.clear()
        while not app._bitrate_jobs.empty():
            app._bitrate_jobs.get_nowait()

    def _post(self, torrents):
        return self.client.post("/api/torrents/bitrates", json={"torrents": torrents})

    def test_unknown_torrent_returns_nothing_and_queues_a_probe(self):
        with mock.patch.object(app, "_ensure_bitrate_worker") as worker:
            r = self._post([{"id": 7, "hash": "abc"}])
        self.assertEqual(r.get_json()["bitrates"], {})
        worker.assert_called_once()
        self.assertEqual(app._bitrate_jobs.get_nowait(), (7, "abc"))

    def test_cached_hit_is_returned_keyed_by_current_id(self):
        with app._bitrate_lock:
            app._bitrate_cache["abc"] = {
                "bps": 8_000_000.0, "bytes": 40_000_000,
                "duration": 40.0, "ts": app.time.time(),
            }
        # Same torrent, new id after a daemon restart: the hash keys the
        # cache, so the answer follows the torrent rather than the id.
        r = self._post([{"id": 99, "hash": "abc"}])
        self.assertEqual(r.get_json()["bitrates"]["99"]["bps"], 8_000_000.0)

    def test_recent_failure_is_not_requeued(self):
        with app._bitrate_lock:
            app._bitrate_cache["abc"] = {
                "bps": None, "bytes": None, "duration": None,
                "ts": app.time.time(),
            }
        self._post([{"id": 7, "hash": "abc"}])
        self.assertTrue(app._bitrate_jobs.empty())

    def test_stale_failure_is_retried(self):
        with app._bitrate_lock:
            app._bitrate_cache["abc"] = {
                "bps": None, "bytes": None, "duration": None,
                "ts": app.time.time() - app._BITRATE_RETRY_S - 1,
            }
        with mock.patch.object(app, "_ensure_bitrate_worker"):
            self._post([{"id": 7, "hash": "abc"}])
        self.assertEqual(app._bitrate_jobs.get_nowait(), (7, "abc"))

    def test_in_flight_probe_is_not_queued_twice(self):
        with mock.patch.object(app, "_ensure_bitrate_worker"):
            self._post([{"id": 7, "hash": "abc"}])
            self._post([{"id": 7, "hash": "abc"}])
        self.assertEqual(app._bitrate_jobs.qsize(), 1)

    def test_entries_without_a_hash_are_dropped(self):
        with mock.patch.object(app, "_ensure_bitrate_worker"):
            r = self._post([{"id": 7}, {"id": 8, "hash": ""}, "junk"])
        self.assertEqual(r.get_json()["bitrates"], {})
        self.assertTrue(app._bitrate_jobs.empty())

    def test_non_list_payload_is_rejected(self):
        r = self.client.post("/api/torrents/bitrates", json={"torrents": "abc"})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
