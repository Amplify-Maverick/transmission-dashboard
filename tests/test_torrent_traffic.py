"""Tests for per-torrent traffic history — turning Transmission's monotonic
uploadedEver/downloadedEver counters into per-bucket deltas, and the queries
the System page graphs read back.

db.DB_PATH is pointed at a temp file so the real dashboard.db is untouched.
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

BUCKET = db.TRAFFIC_BUCKET_SECS


def t(hash, name, up, down):
    return {"hashString": hash, "name": name,
            "uploadedEver": up, "downloadedEver": down}


class TorrentTrafficTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
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
        with db._tx() as c:
            c.execute("DELETE FROM torrent_traffic")
        app._traffic_prev.clear()

    # ---- delta extraction ----

    def test_first_tick_only_establishes_baseline(self):
        """Counters are cumulative, so the first sighting has nothing to
        attribute — recording it would credit a torrent's whole lifetime
        upload to the moment the dashboard started."""
        app._record_torrent_traffic([t("aa", "A", 5000, 100)], 1000)
        self.assertEqual(db.get_torrent_traffic_totals(0), [])

    def test_second_tick_records_the_delta(self):
        app._record_torrent_traffic([t("aa", "A", 5000, 100)], 1000)
        app._record_torrent_traffic([t("aa", "A", 5300, 180)], 1060)
        rows = db.get_torrent_traffic_totals(0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["hash"], "aa")
        self.assertEqual(rows[0]["up_bytes"], 300)
        self.assertEqual(rows[0]["down_bytes"], 80)

    def test_idle_torrent_writes_no_row(self):
        app._record_torrent_traffic([t("aa", "A", 5000, 100)], 1000)
        app._record_torrent_traffic([t("aa", "A", 5000, 100)], 1060)
        self.assertEqual(db.get_torrent_traffic_totals(0), [])

    def test_counter_reset_is_skipped_not_negative(self):
        """A re-added or re-verified torrent restarts uploadedEver at 0."""
        app._record_torrent_traffic([t("aa", "A", 5000, 100)], 1000)
        app._record_torrent_traffic([t("aa", "A", 0, 0)], 1060)
        self.assertEqual(db.get_torrent_traffic_totals(0), [])
        # And it re-baselines, so counting resumes from the new low value.
        app._record_torrent_traffic([t("aa", "A", 250, 0)], 1120)
        rows = db.get_torrent_traffic_totals(0)
        self.assertEqual(rows[0]["up_bytes"], 250)

    def test_ticks_in_one_bucket_accumulate(self):
        app._record_torrent_traffic([t("aa", "A", 0, 0)], BUCKET)
        app._record_torrent_traffic([t("aa", "A", 100, 0)], BUCKET + 30)
        app._record_torrent_traffic([t("aa", "A", 450, 0)], BUCKET + 60)
        with db._conn() as c:
            rows = c.execute("SELECT * FROM torrent_traffic").fetchall()
        self.assertEqual(len(rows), 1, "same bucket must not create a 2nd row")
        self.assertEqual(rows[0]["up_bytes"], 450)

    def test_removed_torrent_drops_out_of_the_baseline(self):
        app._record_torrent_traffic([t("aa", "A", 100, 0), t("bb", "B", 50, 0)], 1000)
        app._record_torrent_traffic([t("aa", "A", 200, 0)], 1060)
        self.assertNotIn("bb", app._traffic_prev)

    def test_name_is_retained_after_rename(self):
        app._record_torrent_traffic([t("aa", "Old", 0, 0)], 1000)
        app._record_torrent_traffic([t("aa", "New", 100, 0)], 1060)
        self.assertEqual(db.get_torrent_traffic_totals(0)[0]["name"], "New")

    # ---- queries ----

    def test_totals_rank_by_upload_and_respect_the_window(self):
        now = int(__import__("time").time())
        old = now - 10 * 86400
        db.add_torrent_traffic(old, [("aa", "A", 9_000_000, 0)])
        db.add_torrent_traffic(now, [("aa", "A", 10, 0), ("bb", "B", 500, 0)])
        recent = db.get_torrent_traffic_totals(now - 3600)
        self.assertEqual([r["hash"] for r in recent], ["bb", "aa"])
        allrows = db.get_torrent_traffic_totals(now - 30 * 86400)
        self.assertEqual(allrows[0]["hash"], "aa")

    def test_series_is_zero_filled(self):
        """A bucket with no row means "uploaded nothing", not "no data" —
        dropping it would draw a straight line across the quiet period."""
        now = int(__import__("time").time())
        since = now - 6 * 3600
        db.add_torrent_traffic(now - 300, [("aa", "A", 400, 0)])
        series = db.get_torrent_traffic_series(since, ["aa"], buckets=12)
        pts = series["aa"]
        self.assertGreater(len(pts), 1)
        self.assertTrue(all(p["v"] is not None for p in pts))
        self.assertEqual(sum(p["v"] for p in pts), 400)
        self.assertEqual([p["t"] for p in pts], sorted(p["t"] for p in pts))

    def test_long_ranges_widen_the_plotted_step(self):
        """The chart labels itself with the step, so a 30d view must report
        the aggregated step rather than the 15-minute storage bucket."""
        now = int(__import__("time").time())
        _, short = db.traffic_series_grid(now - 3600, 120)
        _, long = db.traffic_series_grid(now - 30 * 86400, 120)
        self.assertEqual(short, db.TRAFFIC_BUCKET_SECS)
        self.assertGreater(long, db.TRAFFIC_BUCKET_SECS)
        self.assertEqual(long % db.TRAFFIC_BUCKET_SECS, 0)

    def test_endpoint_reports_the_plotted_step(self):
        now = int(__import__("time").time())
        db.add_torrent_traffic(now - 60, [("aa", "A", 900, 0)])
        d1 = self.client.get("/api/metrics/torrents?range=1h").get_json()
        d30 = self.client.get("/api/metrics/torrents?range=30d").get_json()
        self.assertEqual(d1["step_secs"], db.TRAFFIC_BUCKET_SECS)
        self.assertGreater(d30["step_secs"], db.TRAFFIC_BUCKET_SECS)

    def test_series_rejects_an_injected_field(self):
        with self.assertRaises(ValueError):
            db.get_torrent_traffic_series(0, ["aa"], field="up_bytes; DROP TABLE")

    def test_series_of_nothing_is_empty(self):
        self.assertEqual(db.get_torrent_traffic_series(0, []), {})

    def test_prune_drops_old_buckets_only(self):
        now = int(__import__("time").time())
        db.add_torrent_traffic(now - 40 * 86400, [("aa", "A", 100, 0)])
        db.add_torrent_traffic(now, [("bb", "B", 100, 0)])
        db.prune_torrent_traffic(days=30)
        self.assertEqual([r["hash"] for r in db.get_torrent_traffic_totals(0)], ["bb"])

    # ---- endpoint ----

    def test_endpoint_returns_totals_and_series(self):
        now = int(__import__("time").time())
        db.add_torrent_traffic(now - 60, [("aa", "A", 900, 5), ("bb", "B", 100, 0)])
        res = self.client.get("/api/metrics/torrents?range=24h")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["metric"], "up")
        self.assertEqual([r["hash"] for r in data["totals"]], ["aa", "bb"])
        self.assertEqual(data["totals"][0]["display_name"], "A")
        self.assertEqual([s["hash"] for s in data["series"]], ["aa", "bb"])
        self.assertTrue(data["series"][0]["points"])

    def test_endpoint_uses_the_custom_name(self):
        now = int(__import__("time").time())
        db.add_torrent_traffic(now - 60, [("aa", "Raw name", 900, 0)])
        with patch.object(db, "get_custom_names_map", return_value={"aa": "Nice"}):
            data = self.client.get("/api/metrics/torrents?range=24h").get_json()
        self.assertEqual(data["totals"][0]["display_name"], "Nice")

    def test_endpoint_down_metric_reranks(self):
        now = int(__import__("time").time())
        db.add_torrent_traffic(now - 60, [("aa", "A", 900, 1), ("bb", "B", 1, 900)])
        data = self.client.get("/api/metrics/torrents?range=24h&metric=down").get_json()
        self.assertEqual(data["totals"][0]["hash"], "bb")

    def test_down_ranking_sees_past_the_top_uploaders(self):
        """Ranking must happen in SQL. Sorting an already-LIMITed list of top
        uploaders hides a heavy downloader that seeds nothing."""
        now = int(__import__("time").time())
        rows = [(f"up{i}", f"Up{i}", 10_000 - i, 0) for i in range(8)]
        rows.append(("leech", "Leecher", 1, 999_999))
        db.add_torrent_traffic(now - 60, rows)
        data = self.client.get(
            "/api/metrics/torrents?range=24h&metric=down&limit=8").get_json()
        self.assertEqual(data["totals"][0]["hash"], "leech")
        self.assertEqual(data["series"][0]["hash"], "leech")

    def test_totals_rejects_an_injected_field(self):
        with self.assertRaises(ValueError):
            db.get_torrent_traffic_totals(0, field="up_bytes; DROP TABLE")

    def test_totals_exclude_zero_rows_for_the_ranked_field(self):
        now = int(__import__("time").time())
        db.add_torrent_traffic(now - 60, [("aa", "A", 0, 500)])
        up = db.get_torrent_traffic_totals(now - 3600, field="up_bytes")
        down = db.get_torrent_traffic_totals(now - 3600, field="down_bytes")
        self.assertEqual(up, [])
        self.assertEqual([r["hash"] for r in down], ["aa"])

    # ---- empty states ----

    def test_has_history_distinguishes_the_two_empties(self):
        """A window with no traffic must not look like a sampler that has
        never run — the UI shows a different message for each."""
        now = int(__import__("time").time())
        fresh = self.client.get("/api/metrics/torrents?range=24h").get_json()
        self.assertFalse(fresh["has_history"])
        self.assertEqual(fresh["totals"], [])

        # Traffic exists, but older than the window being asked about.
        db.add_torrent_traffic(now - 5 * 86400, [("aa", "A", 500, 0)])
        quiet = self.client.get("/api/metrics/torrents?range=1h").get_json()
        self.assertTrue(quiet["has_history"])
        self.assertEqual(quiet["totals"], [])

    def test_endpoint_reports_sampler_enabled(self):
        data = self.client.get("/api/metrics/torrents?range=24h").get_json()
        self.assertTrue(data["sampler_enabled"])
        with patch.object(app.config, "METRICS_SAMPLE_INTERVAL", 0):
            off = self.client.get("/api/metrics/torrents?range=24h").get_json()
        self.assertFalse(off["sampler_enabled"])

    # ---- sampler error reporting ----

    def test_sampler_failure_is_logged_not_swallowed(self):
        """A sampler failing every tick used to be invisible — empty charts
        with no explanation anywhere."""
        before = len(db.list_events(limit=50))
        with patch.object(app, "_collect_metric_sample",
                          side_effect=RuntimeError("rpc down")), \
             patch.object(app.time, "sleep", side_effect=[None, StopIteration]):
            with self.assertRaises(StopIteration):
                app._metrics_sampler_worker()
        events = db.list_events(limit=50)
        self.assertEqual(len(events), before + 1)
        self.assertEqual(events[0]["type"], "metrics.sampler")
        self.assertEqual(events[0]["severity"], "error")
        self.assertIn("rpc down", events[0]["details"]["error"])

    def test_recovery_is_logged_so_the_error_isnt_left_hanging(self):
        """One failing tick then a good one: the operator sees the fault
        cleared rather than an error event with no resolution."""
        with patch.object(app, "_collect_metric_sample",
                          side_effect=[RuntimeError("rpc down"), ({}, [])]), \
             patch.object(app, "_record_torrent_traffic"), \
             patch.object(db, "insert_metric_sample"), \
             patch.object(app.time, "sleep",
                          side_effect=[None, None, StopIteration]):
            with self.assertRaises(StopIteration):
                app._metrics_sampler_worker()
        types = [(e["type"], e["severity"]) for e in db.list_events(limit=10)]
        self.assertIn(("metrics.sampler", "error"), types)
        self.assertIn(("metrics.sampler", "info"), types)

    def test_repeated_identical_failures_log_once(self):
        """Same fault every 30s must not flood the events log."""
        before = len(db.list_events(limit=50))
        with patch.object(app, "_collect_metric_sample",
                          side_effect=RuntimeError("rpc down")), \
             patch.object(app.time, "sleep",
                          side_effect=[None, None, None, StopIteration]):
            with self.assertRaises(StopIteration):
                app._metrics_sampler_worker()
        self.assertEqual(len(db.list_events(limit=50)), before + 1)

    def test_endpoint_caps_plotted_series(self):
        now = int(__import__("time").time())
        db.add_torrent_traffic(now - 60, [
            (f"h{i}", f"T{i}", 1000 - i, 0) for i in range(9)])
        data = self.client.get("/api/metrics/torrents?range=24h&limit=9").get_json()
        self.assertEqual(len(data["totals"]), 9)
        self.assertEqual(len(data["series"]), app._TORRENT_SERIES_MAX)

    # ---- seeding list + per-row series ----

    def _seeding(self, hash, name, status=6, up=0, peers=0):
        return {"hashString": hash, "name": name, "status": status,
                "uploadedEver": up, "downloadedEver": 0,
                "peersConnected": peers, "uploadRatio": 1.5, "id": 1}

    def test_seeding_list_includes_idle_seeders(self):
        """The list is live daemon state, not traffic history — a torrent
        seeding to nobody must still appear, with a zero total, instead of
        vanishing because it has no rows."""
        now = int(__import__("time").time())
        db.add_torrent_traffic(now - 60, [("aa", "A", 900, 0)])
        live = [self._seeding("aa", "A", peers=3),
                self._seeding("idle", "Idle One")]
        with patch.object(app.client, "get_stats_torrents", return_value=live):
            data = self.client.get(
                "/api/metrics/torrents/seeding?range=24h").get_json()
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["active"], 1)
        rows = {r["hash"]: r for r in data["torrents"]}
        self.assertEqual(rows["aa"]["bytes"], 900)
        self.assertEqual(rows["aa"]["peers"], 3)
        self.assertEqual(rows["idle"]["bytes"], 0)

    def test_seeding_list_excludes_non_seeding_states(self):
        live = [self._seeding("seed", "Seeding", status=6),
                self._seeding("queued", "Queued to seed", status=5),
                self._seeding("dl", "Downloading", status=4),
                self._seeding("stopped", "Stopped", status=0)]
        with patch.object(app.client, "get_stats_torrents", return_value=live):
            data = self.client.get(
                "/api/metrics/torrents/seeding?range=24h").get_json()
        self.assertEqual({r["hash"] for r in data["torrents"]}, {"seed", "queued"})

    def test_seeding_list_orders_movers_first_then_alphabetical(self):
        now = int(__import__("time").time())
        db.add_torrent_traffic(now - 60, [("busy", "Busy", 500, 0)])
        live = [self._seeding("zzz", "Zebra"), self._seeding("aaa", "Apple"),
                self._seeding("busy", "Busy")]
        with patch.object(app.client, "get_stats_torrents", return_value=live):
            data = self.client.get(
                "/api/metrics/torrents/seeding?range=24h").get_json()
        self.assertEqual([r["name"] for r in data["torrents"]],
                         ["Busy", "Apple", "Zebra"])

    def test_seeding_list_uses_the_custom_name(self):
        live = [self._seeding("aa", "Raw name")]
        with patch.object(app.client, "get_stats_torrents", return_value=live), \
             patch.object(db, "get_custom_names_map", return_value={"aa": "Nice"}):
            data = self.client.get(
                "/api/metrics/torrents/seeding?range=24h").get_json()
        self.assertEqual(data["torrents"][0]["name"], "Nice")

    def test_seeding_list_survives_a_dead_daemon(self):
        with patch.object(app.client, "get_stats_torrents",
                          side_effect=RuntimeError("rpc down")):
            res = self.client.get("/api/metrics/torrents/seeding?range=24h")
        self.assertNotEqual(res.status_code, 200)

    def test_row_series_returns_one_torrents_points(self):
        now = int(__import__("time").time())
        db.add_torrent_traffic(now - 60, [("aa", "A", 900, 5), ("bb", "B", 100, 0)])
        data = self.client.get(
            "/api/metrics/torrents/aa/series?range=24h").get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["hash"], "aa")
        self.assertEqual(data["total"], 900)
        self.assertEqual(sum(p["v"] for p in data["points"]), 900)

    def test_row_series_is_zero_filled_for_an_idle_torrent(self):
        """An expanded idle seeder gets a flat zero line, not an error."""
        data = self.client.get(
            "/api/metrics/torrents/abcdef/series?range=24h").get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["total"], 0)
        self.assertTrue(data["points"])
        self.assertTrue(all(p["v"] == 0 for p in data["points"]))

    def test_row_series_honours_the_metric(self):
        now = int(__import__("time").time())
        db.add_torrent_traffic(now - 60, [("aa", "A", 900, 7)])
        up = self.client.get(
            "/api/metrics/torrents/aa/series?range=24h&metric=up").get_json()
        down = self.client.get(
            "/api/metrics/torrents/aa/series?range=24h&metric=down").get_json()
        self.assertEqual(up["total"], 900)
        self.assertEqual(down["total"], 7)

    def test_row_series_rejects_a_bad_hash(self):
        for bad in ("../etc", "zz!!", "", "x" * 65):
            res = self.client.get(f"/api/metrics/torrents/{bad}/series?range=24h")
            self.assertIn(res.status_code, (400, 404), bad)

    def test_row_series_rejects_bad_range_and_metric(self):
        self.assertEqual(self.client.get(
            "/api/metrics/torrents/aa/series?range=nope").status_code, 400)
        self.assertEqual(self.client.get(
            "/api/metrics/torrents/aa/series?metric=sideways").status_code, 400)

    def test_seeding_endpoints_require_login(self):
        with self.client.session_transaction() as s:
            s.clear()
        for url in ("/api/metrics/torrents/seeding?range=24h",
                    "/api/metrics/torrents/aa/series?range=24h"):
            self.assertIn(self.client.get(url).status_code, (302, 401), url)

    def test_endpoint_rejects_bad_args(self):
        self.assertEqual(
            self.client.get("/api/metrics/torrents?range=nope").status_code, 400)
        self.assertEqual(
            self.client.get("/api/metrics/torrents?metric=sideways").status_code, 400)

    def test_endpoint_requires_login(self):
        with self.client.session_transaction() as s:
            s.clear()
        res = self.client.get("/api/metrics/torrents?range=24h")
        self.assertIn(res.status_code, (302, 401))


if __name__ == "__main__":
    unittest.main()
