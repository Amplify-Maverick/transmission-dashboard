"""Tests for listing finished rips and copying them to the media server.

The copy worker is exercised end to end against stub `ssh` and `rsync`
executables on PATH, so the real progress parsing, verification round-trip and
terminal-state handling all run — only the network is faked.
"""

import os
import shutil
import stat
import tempfile
import unittest
from unittest import mock

import app

# Emits two --info=progress2 lines (\r-separated, as rsync really does) then
# exits with FAKE_RSYNC_RC, echoing FAKE_RSYNC_STDERR to stderr.
_RSYNC_STUB = """#!/bin/sh
printf '      1,000,000  33%%   10.00MB/s    0:00:02\\r'
printf '      3,000,000 100%%   12.00MB/s    0:00:00\\r'
if [ -n "$FAKE_RSYNC_STDERR" ]; then echo "$FAKE_RSYNC_STDERR" >&2; fi
exit ${FAKE_RSYNC_RC:-0}
"""

# Handles the two remote commands the worker issues: mkdir -p and du -scb.
_SSH_STUB = """#!/bin/sh
args="$*"
case "$args" in
  *"mkdir -p"*)
    exit ${FAKE_MKDIR_RC:-0}
    ;;
  *"du -scb"*)
    if [ "${FAKE_DU_RC:-0}" != "0" ]; then
      echo "du: cannot access: No such file or directory" >&2
      exit ${FAKE_DU_RC}
    fi
    printf '%s\\ttotal\\n' "${FAKE_DU_BYTES:-3000000}"
    exit 0
    ;;
esac
exit 0
"""


def _install_stub(bindir, name, body):
    path = os.path.join(bindir, name)
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class _RipCopyBase(unittest.TestCase):
    """Temp rip dir, temp state file, stub ssh/rsync on PATH."""

    CFG = {
        "host": "mediahost",
        "user": "mediauser",
        "port": 22,
        "folders": [{"name": "Movies", "path": "/mnt/pool/movies"}],
    }
    FOLDER = CFG["folders"][0]

    def _enter(self, patcher):
        """Start a patcher and undo it after the test.

        Stands in for TestCase.enterContext, which needs Python 3.11 — the
        project supports 3.9+.
        """
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.rips = os.path.join(self.tmp, "rips")
        os.makedirs(self.rips)

        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        _install_stub(bindir, "rsync", _RSYNC_STUB)
        _install_stub(bindir, "ssh", _SSH_STUB)
        self._enter(mock.patch.dict(
            os.environ, {"PATH": bindir + os.pathsep + os.environ["PATH"]}))

        self._enter(mock.patch.object(
            app, "RIP_COPY_STATE_FILE", os.path.join(self.tmp, "rip_copy_state.json")))
        app._rip_copy_state_cache = {}
        app._rip_copy_state_last_flush = 0.0
        self.addCleanup(setattr, app, "_rip_copy_state_cache", None)

        self.settings = dict(app._rip_settings(), output_dir=self.rips)
        self._enter(mock.patch.object(
            app, "_rip_settings", return_value=self.settings))
        self.events = self._enter(mock.patch.object(app.db, "log_event"))

    def make_file(self, name="Step Brothers.mkv", size=3000000):
        path = os.path.join(self.rips, name)
        with open(path, "wb") as f:
            f.write(b"\0" * size)
        return path

    def env(self, **kv):
        self._enter(mock.patch.dict(os.environ, {k: str(v) for k, v in kv.items()}))


class TestRipFileListing(_RipCopyBase):
    def test_lists_video_files_only(self):
        self.make_file("A.mkv")
        self.make_file("B.mp4")
        self.make_file("C.m4v")
        self.make_file("notes.txt")
        os.makedirs(os.path.join(self.rips, "adir.mkv"))
        names = {f["name"] for f in app._rip_files()}
        self.assertEqual(names, {"A.mkv", "B.mp4", "C.m4v"})

    def test_newest_first(self):
        old = self.make_file("Old.mkv")
        new = self.make_file("New.mkv")
        os.utime(old, (1000, 1000))
        os.utime(new, (2000, 2000))
        self.assertEqual([f["name"] for f in app._rip_files()],
                         ["New.mkv", "Old.mkv"])

    def test_size_and_copy_state_included(self):
        path = self.make_file("A.mkv", size=1234)
        app.update_rip_copy_entry(path, status="done", percent=100.0)
        f = app._rip_files()[0]
        self.assertEqual(f["size"], 1234)
        self.assertEqual(f["copy"]["status"], "done")
        self.assertFalse(f["copy"]["active"])

    def test_history_metadata_joined_by_path(self):
        path = self.make_file("A.mkv")
        rows = [{"output_path": path, "disc_label": "STEP BROTHERS",
                 "preset": "HQ 576p25 Surround", "title_index": 1,
                 "duration_seconds": 6326}]
        with mock.patch.object(app.db, "list_rips", return_value=rows):
            f = app._rip_files()[0]
        self.assertEqual(f["disc_label"], "STEP BROTHERS")
        self.assertEqual(f["preset"], "HQ 576p25 Surround")
        self.assertEqual(f["duration_seconds"], 6326)

    def test_missing_output_dir_is_not_an_error(self):
        self.settings["output_dir"] = os.path.join(self.tmp, "nope")
        self.assertEqual(app._rip_files(), [])


class TestResolveRipFile(_RipCopyBase):
    def test_accepts_a_real_rip(self):
        self.make_file("A.mkv")
        self.assertEqual(app._resolve_rip_file("A.mkv"),
                         os.path.realpath(os.path.join(self.rips, "A.mkv")))

    def test_rejects_traversal(self):
        self.make_file("A.mkv")
        outside = os.path.join(self.tmp, "secret.mkv")
        open(outside, "w").close()
        self.assertIsNone(app._resolve_rip_file("../secret.mkv"))
        self.assertIsNone(app._resolve_rip_file("/etc/passwd"))
        self.assertIsNone(app._resolve_rip_file(outside))

    def test_rejects_non_video(self):
        self.make_file("notes.txt")
        self.assertIsNone(app._resolve_rip_file("notes.txt"))

    def test_rejects_missing(self):
        self.assertIsNone(app._resolve_rip_file("ghost.mkv"))

    def test_rejects_empty(self):
        self.assertIsNone(app._resolve_rip_file(""))
        self.assertIsNone(app._resolve_rip_file(None))


class TestCopyDestination(_RipCopyBase):
    CFG_MULTI = {
        "host": "h", "user": "u", "port": 22, "space_margin_percent": 8,
        "folders": [{"name": "Movies", "path": "/mnt/a/movies"},
                    {"name": "Movies", "path": "/mnt/b/movies"},
                    {"name": "TV", "path": "/mnt/a/tv"}],
    }

    def test_unknown_folder(self):
        folder, err = app._rip_copy_destination(self.CFG_MULTI, "Nope", 100)
        self.assertIsNone(folder)
        self.assertIn("unknown destination folder", err)

    def test_single_candidate_skips_df(self):
        with mock.patch.object(app, "_remote_df",
                               side_effect=AssertionError("should not df")):
            folder, err = app._rip_copy_destination(self.CFG_MULTI, "TV", 0)
        self.assertEqual(folder["path"], "/mnt/a/tv")
        self.assertIsNone(err)

    def test_falls_back_to_the_disk_with_room(self):
        def df(u, h, p, path):
            if path == "/mnt/a/movies":
                return (1000, 990, 10, "/mnt/a")
            return (10 ** 12, 0, 8 * 10 ** 11, "/mnt/b")
        with mock.patch.object(app, "_remote_df", side_effect=df):
            folder, err = app._rip_copy_destination(self.CFG_MULTI, "Movies", 3 * 10 ** 6)
        self.assertEqual(folder["path"], "/mnt/b/movies")
        self.assertIsNone(err)

    def test_rejects_when_nothing_fits(self):
        with mock.patch.object(app, "_remote_df",
                               return_value=(1000, 990, 10, "/mnt/x")):
            folder, err = app._rip_copy_destination(self.CFG_MULTI, "Movies", 3 * 10 ** 6)
        self.assertIsNone(folder)
        self.assertIn("not enough free space", err)

    def test_unreachable_disks_fall_through_to_the_primary(self):
        # Better to let rsync report the real connection error than to block
        # the copy on a df that couldn't run.
        with mock.patch.object(app, "_remote_df",
                               side_effect=RuntimeError("ssh: timed out")):
            folder, err = app._rip_copy_destination(self.CFG_MULTI, "Movies", 3 * 10 ** 6)
        self.assertEqual(folder["path"], "/mnt/a/movies")
        self.assertIsNone(err)

    def test_margin_is_applied_per_drive(self):
        # 5% override on /mnt/b lets a copy through that the global 8% blocks.
        cfg = dict(self.CFG_MULTI, drive_margins={"/mnt/b": 5})
        total, free = 1000, 70
        with mock.patch.object(app, "_remote_df",
                               return_value=(total, total - free, free, "/mnt/b")):
            folder, err = app._rip_copy_destination(cfg, "Movies", 15)
        self.assertIsNotNone(folder)
        self.assertIsNone(err)
        with mock.patch.object(app, "_remote_df",
                               return_value=(total, total - free, free, "/mnt/other")):
            folder, err = app._rip_copy_destination(cfg, "Movies", 15)
        self.assertIsNone(folder)


class TestRunRipCopy(_RipCopyBase):
    """The worker, against stub ssh/rsync."""

    def _run(self, path):
        app._run_rip_copy(path, self.FOLDER, self.CFG)
        return app.load_rip_copy_state()[path]

    def test_successful_copy(self):
        path = self.make_file()
        entry = self._run(path)
        self.assertEqual(entry["status"], "done")
        self.assertEqual(entry["percent"], 100.0)
        self.assertEqual(entry["dest_host"], "mediahost")
        self.assertEqual(entry["dest_path"], "/mnt/pool/movies/Step Brothers.mkv")
        self.assertEqual(entry["folder"], "Movies")
        self.assertEqual(entry["total_bytes"], 3000000)
        self.assertTrue(entry["started_at"])
        self.assertTrue(entry["finished_at"])
        self.assertIsNone(entry["error_message"])

    def test_progress_is_parsed_from_rsync(self):
        path = self.make_file()
        seen = []
        real = app.update_rip_copy_entry

        def spy(p, *a, **kw):
            if "percent" in kw:
                seen.append(kw["percent"])
            return real(p, *a, **kw)

        with mock.patch.object(app, "update_rip_copy_entry", side_effect=spy):
            app._run_rip_copy(path, self.FOLDER, self.CFG)
        # 33% then 100% from the stub's two progress lines.
        self.assertIn(33.0, seen)
        self.assertIn(100.0, seen)

    def test_verification_bytes_recorded(self):
        self.env(FAKE_DU_BYTES=3000000)
        path = self.make_file()
        self.assertEqual(self._run(path)["verified_bytes"], 3000000)

    def test_rsync_failure_reports_stderr(self):
        self.env(FAKE_RSYNC_RC=23, FAKE_RSYNC_STDERR="rsync: mkstemp failed: No space left")
        path = self.make_file()
        entry = self._run(path)
        self.assertEqual(entry["status"], "error")
        self.assertIn("No space left", entry["error_message"])

    def test_rsync_failure_without_stderr_uses_exit_code(self):
        self.env(FAKE_RSYNC_RC=12)
        path = self.make_file()
        self.assertIn("code 12", self._run(path)["error_message"])

    def test_remote_mkdir_failure(self):
        self.env(FAKE_MKDIR_RC=1)
        path = self.make_file()
        entry = self._run(path)
        self.assertEqual(entry["status"], "error")
        self.assertIn("remote mkdir failed", entry["error_message"])

    def test_missing_file_after_copy_fails_the_copy(self):
        # rsync said 0 but the file isn't on the remote — e.g. the library
        # path is an unmounted mountpoint.
        self.env(FAKE_DU_RC=1)
        path = self.make_file()
        entry = self._run(path)
        self.assertEqual(entry["status"], "error")
        self.assertIn("could not be verified", entry["error_message"])

    def test_cancellation(self):
        path = self.make_file()
        cancel = app.threading.Event()
        cancel.set()
        with app._active_rip_copies_lock:
            app._active_rip_copies[path] = {"cancel": cancel, "proc": None}
        entry = self._run(path)
        self.assertEqual(entry["status"], "cancelled")
        self.assertIsNone(entry["error_message"])
        # The local file is kept — cancelling a copy must never delete data.
        self.assertTrue(os.path.exists(path))

    def test_slot_released_afterwards(self):
        path = self.make_file()
        self._run(path)
        with app._active_rip_copies_lock:
            self.assertNotIn(path, app._active_rip_copies)

    def test_event_logged_on_success(self):
        path = self.make_file()
        self._run(path)
        types = [c.args[0] for c in self.events.call_args_list]
        self.assertIn("rip.copied", types)
        call = next(c for c in self.events.call_args_list if c.args[0] == "rip.copied")
        self.assertEqual(call.args[1], "info")
        self.assertIn("mediahost", call.args[2])

    def test_event_severity_on_failure(self):
        self.env(FAKE_RSYNC_RC=23)
        path = self.make_file()
        self._run(path)
        call = next(c for c in self.events.call_args_list if c.args[0] == "rip.copied")
        self.assertEqual(call.args[1], "error")

    def test_library_refresh_fires_only_on_success(self):
        path = self.make_file()
        with mock.patch.object(app, "_trigger_library_refresh") as refresh:
            app._run_rip_copy(path, self.FOLDER, self.CFG)
        refresh.assert_called_once()

        self.env(FAKE_RSYNC_RC=23)
        with mock.patch.object(app, "_trigger_library_refresh") as refresh:
            app._run_rip_copy(path, self.FOLDER, self.CFG)
        refresh.assert_not_called()


class TestRipInProgressFiles(_RipCopyBase):
    """HandBrake writes straight to the final path, so a rip in progress looks
    like a file in the output directory that grows from zero."""

    def _ripping(self, path, **fields):
        """Register `path` as the live output of a running rip on /dev/sr1."""
        app._rip_state_cache = {}
        self.addCleanup(setattr, app, "_rip_state_cache", None)
        self._enter(mock.patch.object(
            app, "RIP_STATE_FILE", os.path.join(self.tmp, "rip_state.json")))
        app.update_rip_entry("/dev/sr1", status="ripping", output_path=path,
                             **fields)
        cancel = app.threading.Event()
        with app._active_rips_lock:
            app._active_rips["/dev/sr1"] = {"cancel": cancel}
        self.addCleanup(lambda: app._active_rips.pop("/dev/sr1", None))

    def test_in_progress_output_is_flagged(self):
        path = self.make_file("Half.mkv", size=1024)
        self._ripping(path, percent=62.5, phase="working", **{"pass": 2})
        f = next(x for x in app._rip_files() if x["path"] == path)
        self.assertIsNotNone(f["ripping"])
        self.assertEqual(f["ripping"]["percent"], 62.5)
        self.assertEqual(f["ripping"]["pass"], 2)

    def test_finished_files_are_not_flagged(self):
        done = self.make_file("Done.mkv")
        live = self.make_file("Half.mkv")
        self._ripping(live)
        by_name = {f["name"]: f for f in app._rip_files()}
        self.assertIsNone(by_name["Done.mkv"]["ripping"])
        self.assertIsNotNone(by_name["Half.mkv"]["ripping"])

    def test_nothing_flagged_when_no_rip_runs(self):
        self.make_file("Done.mkv")
        self.assertEqual(app._active_rip_output_paths(), {})
        self.assertIsNone(app._rip_files()[0]["ripping"])

    def test_copy_is_refused_while_ripping(self):
        # Copying mid-encode ships a truncated movie, and a two-pass encode
        # rewrites the file wholesale on pass 2.
        path = self.make_file("Half.mkv")
        self._ripping(path)
        client = app.app.test_client()
        with client.session_transaction() as s:
            s["logged_in"] = True
        res = client.post("/api/rip/files/copy",
                          json={"path": "Half.mkv", "folder": "Movies"})
        self.assertEqual(res.status_code, 409)
        self.assertIn("still being ripped", res.get_json()["error"])

    def test_delete_is_refused_while_ripping(self):
        path = self.make_file("Half.mkv")
        self._ripping(path)
        client = app.app.test_client()
        with client.session_transaction() as s:
            s["logged_in"] = True
        res = client.post("/api/rip/files/delete", json={"path": "Half.mkv"})
        self.assertEqual(res.status_code, 409)
        self.assertTrue(os.path.exists(path))


class TestCopyStateIdentity(_RipCopyBase):
    """Copy state is keyed by path, and paths get reused."""

    def test_matching_state_is_kept(self):
        path = self.make_file("A.mkv", size=100)
        st = os.stat(path)
        app.update_rip_copy_entry(path, status="done", source_size=st.st_size,
                                  source_mtime=st.st_mtime)
        self.assertEqual(app._rip_files()[0]["copy"]["status"], "done")

    def test_state_for_a_replaced_file_is_dropped(self):
        # Rip "A.mkv", copy it, delete it, rip the same disc again: the new
        # file must not inherit "on media server" from the old one.
        path = self.make_file("A.mkv", size=100)
        st = os.stat(path)
        app.update_rip_copy_entry(path, status="done", source_size=st.st_size,
                                  source_mtime=st.st_mtime)
        os.remove(path)
        self.make_file("A.mkv", size=999)
        self.assertNotIn("status", app._rip_files()[0]["copy"])

    def test_growing_file_invalidates_state(self):
        path = self.make_file("A.mkv", size=100)
        st = os.stat(path)
        app.update_rip_copy_entry(path, status="done", source_size=st.st_size,
                                  source_mtime=st.st_mtime)
        with open(path, "ab") as f:
            f.write(b"\0" * 50)
        self.assertNotIn("status", app._rip_files()[0]["copy"])

    def test_legacy_state_without_identity_is_trusted(self):
        path = self.make_file("A.mkv")
        app.update_rip_copy_entry(path, status="done")
        self.assertEqual(app._rip_files()[0]["copy"]["status"], "done")

    def test_matcher_tolerates_sub_second_mtime_drift(self):
        path = self.make_file("A.mkv", size=100)
        st = os.stat(path)
        self.assertTrue(app._copy_state_matches_file(
            {"source_size": 100, "source_mtime": st.st_mtime + 0.4}, st))
        self.assertFalse(app._copy_state_matches_file(
            {"source_size": 100, "source_mtime": st.st_mtime + 30}, st))

    def test_matcher_ignores_unparseable_values(self):
        path = self.make_file("A.mkv", size=100)
        st = os.stat(path)
        self.assertTrue(app._copy_state_matches_file(
            {"source_size": "x", "source_mtime": "y"}, st))


class TestDeleteRipFile(_RipCopyBase):
    def setUp(self):
        super().setUp()
        self.client = app.app.test_client()
        with self.client.session_transaction() as s:
            s["logged_in"] = True

    def test_deletes_the_file_and_reports_freed_space(self):
        path = self.make_file("A.mkv", size=4096)
        res = self.client.post("/api/rip/files/delete", json={"path": "A.mkv"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["freed"], 4096)
        self.assertFalse(os.path.exists(path))

    def test_clears_the_copy_result(self):
        path = self.make_file("A.mkv")
        app.update_rip_copy_entry(path, status="done")
        self.client.post("/api/rip/files/delete", json={"path": "A.mkv"})
        self.assertNotIn(path, app.load_rip_copy_state())

    def test_logs_an_event(self):
        self.make_file("A.mkv")
        self.client.post("/api/rip/files/delete", json={"path": "A.mkv"})
        self.assertIn("rip.deleted",
                      [c.args[0] for c in self.events.call_args_list])

    def test_refuses_while_copying(self):
        path = self.make_file("A.mkv")
        with app._active_rip_copies_lock:
            app._active_rip_copies[path] = {"cancel": app.threading.Event(),
                                           "proc": None}
        self.addCleanup(lambda: app._active_rip_copies.pop(path, None))
        res = self.client.post("/api/rip/files/delete", json={"path": "A.mkv"})
        self.assertEqual(res.status_code, 409)
        self.assertIn("being copied", res.get_json()["error"])
        self.assertTrue(os.path.exists(path))

    def test_cannot_delete_outside_the_output_directory(self):
        outside = os.path.join(self.tmp, "keepme.mkv")
        open(outside, "w").close()
        for candidate in ("../keepme.mkv", outside, "/etc/passwd"):
            res = self.client.post("/api/rip/files/delete",
                                   json={"path": candidate})
            self.assertEqual(res.status_code, 404)
        self.assertTrue(os.path.exists(outside))

    def test_missing_file(self):
        res = self.client.post("/api/rip/files/delete", json={"path": "ghost.mkv"})
        self.assertEqual(res.status_code, 404)

    def test_requires_login(self):
        self.make_file("A.mkv")
        anon = app.app.test_client()
        res = anon.post("/api/rip/files/delete", json={"path": "A.mkv"})
        self.assertEqual(res.status_code, 401)


class TestRipCopyReconcile(_RipCopyBase):
    def test_copying_becomes_interrupted(self):
        path = self.make_file()
        app.update_rip_copy_entry(path, status="copying", percent=40.0)
        self.assertEqual(app._reconcile_interrupted_rip_copies(), [path])
        entry = app.load_rip_copy_state()[path]
        self.assertEqual(entry["status"], "interrupted")
        self.assertTrue(entry["error_message"])
        # rsync --partial means a retry resumes, so the partial data stays.
        self.assertTrue(os.path.exists(path))

    def test_terminal_states_untouched(self):
        path = self.make_file()
        app.update_rip_copy_entry(path, status="done")
        self.assertEqual(app._reconcile_interrupted_rip_copies(), [])
        self.assertEqual(app.load_rip_copy_state()[path]["status"], "done")

    def test_replace_drops_the_previous_attempt(self):
        path = self.make_file()
        app.update_rip_copy_entry(path, status="error", error_message="boom")
        app.update_rip_copy_entry(path, _replace=True, status="copying")
        entry = app.load_rip_copy_state()[path]
        self.assertEqual(entry["status"], "copying")
        self.assertNotIn("error_message", entry)


if __name__ == "__main__":
    unittest.main()
