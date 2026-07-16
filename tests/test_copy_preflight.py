"""Tests for /api/torrent/<tid>/copy/preflight — the copy modal's
drive indicator.

The endpoint must mirror _run_copy's disk selection: df each same-name
candidate in order and pick the first that fits with the 8% margin.
These tests mock the media config, the torrent location, and the remote
df so the selection logic is exercised without SSH or transmission.
"""

import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("USE_MOCK", "true")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

import app  # noqa: E402

TB = 1_000_000_000_000
CFG = {
    "host": "media.example", "user": "mediauser", "port": 22,
    "folders": [
        {"name": "Movies", "path": "/mnt/internal/movies"},
        {"name": "Movies", "path": "/mnt/external/movies"},
        {"name": "Shows", "path": "/mnt/internal/shows"},
    ],
}


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()
        with self.client.session_transaction() as s:
            s["logged_in"] = True
        # A real 5-byte file keeps _estimate_source_size on its cheap
        # single-file path (no du subprocess).
        self.src = tempfile.NamedTemporaryFile(delete=False)
        self.src.write(b"video")
        self.src.close()
        self.addCleanup(os.unlink, self.src.name)

    def _get(self, folder="Movies", df_by_path=None, source=None,
             cfg=CFG):
        """Call the endpoint with mocked config, source, and remote df.

        df_by_path maps path -> (total, used, available) or an Exception.
        """
        def fake_df(user, host, port, path):
            v = (df_by_path or {}).get(path)
            if isinstance(v, Exception):
                raise v
            if v is None:
                raise AssertionError(f"unexpected df for {path}")
            return v

        with patch.object(app, "_read_media_config", return_value=cfg), \
             patch.object(app.client, "get_torrent_location",
                          return_value=source or self.src.name), \
             patch.object(app, "_remote_df", side_effect=fake_df):
            res = self.client.get(
                f"/api/torrent/1/copy/preflight?folder={folder}")
        return res

    def test_primary_fits_is_chosen(self):
        res = self._get(df_by_path={
            "/mnt/internal/movies": (TB, 0, TB),
            "/mnt/external/movies": (TB, 0, TB),
        })
        data = res.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["chosen_path"], "/mnt/internal/movies")
        self.assertEqual([d["fits"] for d in data["disks"]], [True, True])

    def test_full_primary_falls_back_to_external(self):
        res = self._get(df_by_path={
            "/mnt/internal/movies": (TB, TB, 0),
            "/mnt/external/movies": (TB, 0, TB),
        })
        data = res.get_json()
        self.assertEqual(data["chosen_path"], "/mnt/external/movies")
        self.assertEqual([d["fits"] for d in data["disks"]], [False, True])

    def test_margin_alone_rejects_primary(self):
        # Primary has room for the bytes but would dip under the default
        # reserve; the copy must go to the external drive.
        margin = int(TB * app.DEFAULT_SPACE_MARGIN_PERCENT / 100)
        res = self._get(df_by_path={
            "/mnt/internal/movies": (TB, TB - margin + 1, margin - 1),
            "/mnt/external/movies": (TB, 0, TB),
        })
        data = res.get_json()
        self.assertEqual(data["chosen_path"], "/mnt/external/movies")

    def test_configured_margin_overrides_default(self):
        # 20% free but a configured 30% margin: primary must be rejected,
        # and the endpoint must report the configured fraction to the UI.
        cfg = dict(CFG, space_margin_percent=30)
        free = int(TB * 0.20)
        res = self._get(cfg=cfg, df_by_path={
            "/mnt/internal/movies": (TB, TB - free, free),
            "/mnt/external/movies": (TB, 0, TB),
        })
        data = res.get_json()
        self.assertEqual(data["margin_fraction"], 0.30)
        self.assertEqual(data["chosen_path"], "/mnt/external/movies")

    def test_zero_margin_accepts_nearly_full_disk(self):
        # Margin configured off: primary fits as long as the bytes do.
        cfg = dict(CFG, space_margin_percent=0)
        res = self._get(cfg=cfg, df_by_path={
            "/mnt/internal/movies": (TB, TB - 5, 5),
            "/mnt/external/movies": (TB, 0, TB),
        })
        data = res.get_json()
        self.assertEqual(data["margin_fraction"], 0.0)
        self.assertEqual(data["chosen_path"], "/mnt/internal/movies")

    def test_no_disk_fits(self):
        res = self._get(df_by_path={
            "/mnt/internal/movies": (TB, TB, 0),
            "/mnt/external/movies": (TB, TB, 0),
        })
        data = res.get_json()
        self.assertIsNone(data["chosen_path"])

    def test_all_unreachable_targets_primary(self):
        err = RuntimeError("ssh timed out")
        res = self._get(df_by_path={
            "/mnt/internal/movies": err,
            "/mnt/external/movies": err,
        })
        data = res.get_json()
        # Worker falls through to the primary so rsync surfaces the error.
        self.assertEqual(data["chosen_path"], "/mnt/internal/movies")
        self.assertTrue(all(d["error"] for d in data["disks"]))

    def test_single_candidate_shortfall_has_no_chosen(self):
        res = self._get(folder="Shows", df_by_path={
            "/mnt/internal/shows": (TB, TB, 0),
        })
        data = res.get_json()
        self.assertIsNone(data["chosen_path"])
        self.assertFalse(data["disks"][0]["fits"])

    def test_unknown_size_targets_primary_without_df_gate(self):
        # Source missing → need 0 → worker skips selection entirely.
        res = self._get(source="/nonexistent/path", df_by_path={
            "/mnt/internal/movies": (TB, TB, 0),
            "/mnt/external/movies": (TB, 0, TB),
        })
        data = res.get_json()
        self.assertEqual(data["need"], 0)
        self.assertEqual(data["chosen_path"], "/mnt/internal/movies")

    def test_unknown_folder_400s(self):
        res = self._get(folder="Nope", df_by_path={})
        self.assertEqual(res.status_code, 400)


if __name__ == "__main__":
    unittest.main()
