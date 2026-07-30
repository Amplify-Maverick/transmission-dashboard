"""Tests for the DVD-ripping backend.

The scan fixture in tests/data/ is real `HandBrakeCLI --scan --json` output
from a retail DVD (chapter lists trimmed for size), so the parser is tested
against the shape HandBrake 1.11 actually emits rather than a hand-written
approximation.
"""

import json
import os
import tempfile
import unittest
from unittest import mock

import app
import handbrake

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def _fixture(name):
    with open(os.path.join(DATA_DIR, name)) as f:
        return f.read()


class TestParseScan(unittest.TestCase):
    """Turning a real scan into a title list."""

    @classmethod
    def setUpClass(cls):
        cls.raw = _fixture("scan_step_brothers.txt")

    def test_titles_parsed(self):
        res = handbrake.parse_scan(self.raw)
        self.assertEqual([t["index"] for t in res["titles"]], [1, 2, 5])
        self.assertEqual(res["disc_name"], "Step_Brothers")

    def test_durations(self):
        titles = {t["index"]: t for t in handbrake.parse_scan(self.raw)["titles"]}
        self.assertEqual(titles[1]["duration_seconds"], 1 * 3600 + 45 * 60 + 26)
        self.assertEqual(titles[1]["duration"], "1:45:26")
        # A sub-hour title drops the hours component.
        self.assertEqual(titles[5]["duration"], "0:48")

    def test_main_feature_falls_back_to_longest(self):
        # DVDs report MainFeature: -1 — the longest title is the feature.
        self.assertEqual(json.loads(
            self.raw.split("JSON Title Set: ", 1)[1])["MainFeature"], -1)
        self.assertEqual(handbrake.parse_scan(self.raw)["main_index"], 1)

    def test_video_metadata(self):
        title = handbrake.parse_scan(self.raw)["titles"][0]
        self.assertEqual((title["width"], title["height"]), (720, 480))
        self.assertEqual(title["fps"], 23.976)
        # This disc is film-sourced and flagged progressive; the field is
        # passed through rather than inferred from the frame rate.
        self.assertFalse(title["interlaced"])
        self.assertEqual(title["angles"], 1)

    def test_audio_tracks(self):
        audio = handbrake.parse_scan(self.raw)["titles"][0]["audio"]
        self.assertEqual(len(audio), 3)
        first = audio[0]
        self.assertEqual(first["index"], 1)
        self.assertEqual(first["language"], "English")
        self.assertEqual(first["codec"], "ac3")
        self.assertEqual(first["channels"], 6)
        self.assertEqual(first["channel_layout"], "5.1(side)")
        self.assertIn("448 kbps", first["description"])

    def test_subtitle_tracks(self):
        subs = handbrake.parse_scan(self.raw)["titles"][0]["subtitles"]
        self.assertTrue(subs)
        self.assertEqual(subs[0]["index"], 1)
        self.assertEqual(subs[0]["format"], "bitmap")
        self.assertEqual(subs[0]["source"], "VOBSUB")

    def test_min_seconds_filters_idents(self):
        res = handbrake.parse_scan(self.raw, min_seconds=300)
        self.assertEqual([t["index"] for t in res["titles"]], [1, 2])
        # The main feature is still resolved from what survived the filter.
        self.assertEqual(res["main_index"], 1)

    def test_no_title_set_raises(self):
        with self.assertRaises(ValueError):
            handbrake.parse_scan("libdvdnav: DVD Title: FOO\nno json here\n")

    def test_truncated_title_set_raises(self):
        with self.assertRaises(ValueError):
            handbrake.parse_scan('JSON Title Set: {"TitleList": [')


class TestPickMainTitle(unittest.TestCase):
    def test_longest_wins(self):
        titles = [{"index": 3, "duration_seconds": 60},
                  {"index": 7, "duration_seconds": 9000},
                  {"index": 9, "duration_seconds": 120}]
        self.assertEqual(handbrake.pick_main_title(titles), 7)

    def test_empty(self):
        self.assertIsNone(handbrake.pick_main_title([]))


class TestDurations(unittest.TestCase):
    def test_from_hms(self):
        self.assertEqual(
            handbrake.duration_seconds({"Hours": 2, "Minutes": 3, "Seconds": 4}),
            7384)

    def test_from_ticks_only(self):
        # 90kHz ticks — used when a block omits the H/M/S fields.
        self.assertEqual(handbrake.duration_seconds({"Ticks": 90000 * 42}), 42)

    def test_junk(self):
        self.assertEqual(handbrake.duration_seconds(None), 0)
        self.assertEqual(handbrake.duration_seconds("nope"), 0)

    def test_fmt(self):
        self.assertEqual(handbrake.fmt_duration(0), "0:00")
        self.assertEqual(handbrake.fmt_duration(59), "0:59")
        self.assertEqual(handbrake.fmt_duration(600), "10:00")
        self.assertEqual(handbrake.fmt_duration(3661), "1:01:01")
        self.assertEqual(handbrake.fmt_duration(-5), "0:00")


class TestJSONBlockStream(unittest.TestCase):
    """HandBrake writes pretty-printed blocks interleaved with log lines."""

    STREAM = """\
Version: {
    "Arch": "x86_64",
    "Name": "HandBrake"
}
[04:19:27] Compile-time hardening features are enabled
libdvdnav: DVD Title: Step_Brothers
Progress: {
    "Scanning": {
        "Preview": 0,
        "Progress": 0.0
    },
    "State": "SCANNING"
}
Progress: {
    "State": "WORKING",
    "Working": {
        "ETASeconds": 1234,
        "Pass": 1,
        "PassCount": 2,
        "Paused": 0,
        "Progress": 0.25,
        "Rate": 31.5,
        "RateAvg": 28.9
    }
}
"""

    def test_blocks_and_kinds(self):
        blocks = list(handbrake.iter_json_blocks(self.STREAM.splitlines(True)))
        self.assertEqual([k for k, _ in blocks],
                         ["Version", "Progress", "Progress"])
        self.assertEqual(blocks[0][1]["Name"], "HandBrake")
        self.assertEqual(blocks[2][1]["Working"]["Rate"], 31.5)

    def test_brace_in_a_string_does_not_split_a_block(self):
        # Brace counting would break here; the col-0 `}` rule doesn't.
        stream = 'Progress: {\n    "Name": "Disc {weird}",\n    "State": "WORKING"\n}\n'
        blocks = list(handbrake.iter_json_blocks(stream.splitlines(True)))
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0][1]["Name"], "Disc {weird}")

    def test_truncated_final_block_is_dropped(self):
        # A killed process cuts stdout mid-block; that must not raise.
        stream = 'Progress: {\n    "State": "WORKING"\n'
        self.assertEqual(list(handbrake.iter_json_blocks(stream.splitlines(True))), [])

    def test_log_only_stream(self):
        self.assertEqual(
            list(handbrake.iter_json_blocks(["[04:19:27] starting\n", "done\n"])), [])


class TestParseProgress(unittest.TestCase):
    def test_working(self):
        p = handbrake.parse_progress({
            "State": "WORKING",
            "Working": {"Progress": 0.2537, "Rate": 31.55, "RateAvg": 28.91,
                        "ETASeconds": 900, "Pass": 1, "PassCount": 2,
                        "Paused": 0},
        })
        self.assertEqual(p["state"], "WORKING")
        self.assertEqual(p["percent"], 25.4)
        self.assertEqual(p["fps"], 31.6)
        self.assertEqual(p["avg_fps"], 28.9)
        self.assertEqual(p["eta_seconds"], 900)
        self.assertEqual((p["pass"], p["pass_count"]), (1, 2))
        self.assertFalse(p["paused"])

    def test_percent_is_clamped(self):
        # HandBrake occasionally reports slightly over 1.0 at the tail.
        p = handbrake.parse_progress({"State": "WORKING",
                                      "Working": {"Progress": 1.004}})
        self.assertEqual(p["percent"], 100.0)

    def test_scanning_has_no_rate(self):
        p = handbrake.parse_progress({"State": "SCANNING",
                                      "Scanning": {"Progress": 0.5}})
        self.assertEqual(p["percent"], 50.0)
        self.assertNotIn("fps", p)

    def test_workdone_error_code(self):
        p = handbrake.parse_progress({"State": "WORKDONE",
                                      "WorkDone": {"Error": 3}})
        self.assertEqual(p["error_code"], 3)

    def test_zero_rate_becomes_none(self):
        # A 0 fps reading is "not measured yet", not a real rate.
        p = handbrake.parse_progress({"State": "WORKING",
                                      "Working": {"Progress": 0.0, "Rate": 0}})
        self.assertIsNone(p["fps"])


class TestParsePresetList(unittest.TestCase):
    # Real --preset-list shape: libhb logs first (stderr is shared), groups at
    # column 0, presets indented 4, wrapped descriptions indented 8.
    RAW = """\
[04:20:02] Compile-time hardening features are enabled
[04:20:02] hb_init: starting libhb thread
General/
    Very Fast 1080p30
        Small H.264 video (up to 1080p30) and AAC stereo audio, in
        an MP4 container.
    HQ 576p25 Surround
        High quality H.264 video (up to 576p25), AAC stereo audio,
        and Dolby Digital (AC-3) surround audio, in an MP4
        container.
Matroska/
    H.265 MKV 480p30
        H.265 video (up to 480p30) and AAC stereo audio, in an MKV
        container.
"""

    def test_groups_and_names(self):
        groups = handbrake.parse_preset_list(self.RAW)
        self.assertEqual([g["group"] for g in groups], ["General", "Matroska"])
        self.assertEqual([p["name"] for p in groups[0]["presets"]],
                         ["Very Fast 1080p30", "HQ 576p25 Surround"])

    def test_wrapped_descriptions_are_joined(self):
        groups = handbrake.parse_preset_list(self.RAW)
        desc = groups[0]["presets"][1]["description"]
        self.assertEqual(
            desc,
            "High quality H.264 video (up to 576p25), AAC stereo audio, and "
            "Dolby Digital (AC-3) surround audio, in an MP4 container.")

    def test_empty_input(self):
        self.assertEqual(handbrake.parse_preset_list(""), [])

    def test_log_only_input(self):
        self.assertEqual(handbrake.parse_preset_list("[04:20:02] qsv: no\n"), [])


class TestDriveDetection(unittest.TestCase):
    # Verbatim from `udevadm info --query=property` on the production host.
    LOADED = """\
ID_MODEL=DVD-RW_DS8A8SH
ID_MODEL_ENC=DVD-RW\\x20DS8A8SH\\x20\\x20
ID_CDROM_MEDIA_DVD=1
ID_CDROM_MEDIA_STATE=complete
ID_CDROM_MEDIA_TRACK_COUNT=1
ID_BUS=usb
ID_FS_LABEL=Step_Brothers
ID_FS_LABEL_ENC=Step_Brothers
ID_FS_TYPE=udf
"""
    EMPTY = """\
ID_MODEL=MATSHITADVD-RAM_UJ8G2
ID_MODEL_ENC=MATSHITADVD-RAM\\x20UJ8G2\\x20\\x20
ID_BUS=ata
"""

    def test_parse_props(self):
        props = handbrake.parse_udev_props(self.LOADED)
        self.assertEqual(props["ID_FS_TYPE"], "udf")
        self.assertEqual(props["ID_BUS"], "usb")

    def test_loaded_drive(self):
        d = handbrake.drive_from_props(
            "/dev/sr1", handbrake.parse_udev_props(self.LOADED))
        self.assertTrue(d["has_disc"])
        self.assertEqual(d["disc_label"], "Step_Brothers")
        self.assertEqual(d["media_type"], "dvd")
        self.assertEqual(d["media_state"], "complete")
        self.assertEqual(d["bus"], "usb")
        self.assertEqual(d["model"], "DVD-RW DS8A8SH")

    def test_empty_drive(self):
        d = handbrake.drive_from_props(
            "/dev/sr0", handbrake.parse_udev_props(self.EMPTY))
        self.assertFalse(d["has_disc"])
        self.assertIsNone(d["disc_label"])
        self.assertIsNone(d["media_type"])
        self.assertEqual(d["model"], "MATSHITADVD-RAM UJ8G2")

    def test_no_udev_data_still_lists_the_drive(self):
        d = handbrake.drive_from_props("/dev/sr2", {})
        self.assertEqual(d["device"], "/dev/sr2")
        self.assertEqual(d["model"], "sr2")
        self.assertFalse(d["has_disc"])


class TestSanitizeFilename(unittest.TestCase):
    def test_underscores_become_spaces(self):
        # DVD labels are mastered as STEP_BROTHERS; spaces read better.
        self.assertEqual(handbrake.sanitize_filename("Step_Brothers"),
                         "Step Brothers")

    def test_path_separators_are_stripped(self):
        # Nothing may survive that could escape the output directory.
        for name in ("../../etc/passwd", "a/b", "a\\b"):
            out = handbrake.sanitize_filename(name)
            self.assertNotIn("/", out)
            self.assertNotIn("\\", out)
            self.assertNotIn("..", out.replace(". ", ""))

    def test_windows_hostile_chars(self):
        self.assertEqual(handbrake.sanitize_filename('Movie: "The?Sequel"*'),
                         "Movie The Sequel")

    def test_leading_dots_and_whitespace(self):
        self.assertEqual(handbrake.sanitize_filename("  ...hidden.  "), "hidden")
        self.assertEqual(handbrake.sanitize_filename("a\t\tb"), "a b")

    def test_empty_uses_fallback(self):
        self.assertEqual(handbrake.sanitize_filename(""), "disc")
        self.assertEqual(handbrake.sanitize_filename("///", fallback="x"), "x")
        self.assertEqual(handbrake.sanitize_filename(None), "disc")

    def test_length_cap(self):
        self.assertEqual(len(handbrake.sanitize_filename("a" * 400)), 150)


class TestCommands(unittest.TestCase):
    def test_scan_command(self):
        cmd = handbrake.scan_command("/dev/sr1", cli="HB", min_seconds=30)
        self.assertEqual(cmd[0], "HB")
        self.assertIn("--scan", cmd)
        self.assertIn("--json", cmd)
        # title 0 = scan every title
        self.assertEqual(cmd[cmd.index("--title") + 1], "0")
        self.assertEqual(cmd[cmd.index("--min-duration") + 1], "30")

    def test_rip_command_basics(self):
        cmd = handbrake.rip_command(
            "/dev/sr1", 3, "/out/Film.mkv", preset="HQ 576p25 Surround",
            container="mkv", cli="HB", nice=0)
        self.assertEqual(cmd[0], "HB")  # nice=0 means no wrapper
        self.assertEqual(cmd[cmd.index("--title") + 1], "3")
        self.assertEqual(cmd[cmd.index("--preset") + 1], "HQ 576p25 Surround")
        self.assertEqual(cmd[cmd.index("--format") + 1], "av_mkv")
        self.assertEqual(cmd[cmd.index("--output") + 1], "/out/Film.mkv")
        self.assertIn("--markers", cmd)

    def test_nice_wrapper(self):
        cmd = handbrake.rip_command("/dev/sr1", 1, "/out/a.mkv", cli="HB", nice=15)
        self.assertEqual(cmd[:3], ["nice", "-n", "15"])
        self.assertEqual(cmd[3], "HB")

    def test_track_selection_args(self):
        cmd = handbrake.rip_command(
            "/dev/sr1", 1, "/out/a.mkv", cli="HB", nice=0,
            audio=[1, 3], subtitles=[2], burn_subtitle=True)
        self.assertEqual(cmd[cmd.index("--audio") + 1], "1,3")
        self.assertEqual(cmd[cmd.index("--subtitle") + 1], "2")
        self.assertIn("--subtitle-burned=1", cmd)

    def test_no_burn_without_subtitles(self):
        cmd = handbrake.rip_command("/dev/sr1", 1, "/out/a.mkv", cli="HB",
                                    nice=0, burn_subtitle=True)
        self.assertNotIn("--subtitle-burned=1", cmd)

    def test_chapters_off(self):
        cmd = handbrake.rip_command("/dev/sr1", 1, "/out/a.mkv", cli="HB",
                                    nice=0, chapters=False)
        self.assertNotIn("--markers", cmd)

    def test_mp4_container(self):
        cmd = handbrake.rip_command("/dev/sr1", 1, "/out/a.mp4", cli="HB",
                                    nice=0, container="mp4")
        self.assertEqual(cmd[cmd.index("--format") + 1], "av_mp4")
        self.assertEqual(handbrake.output_extension("mp4"), ".mp4")

    def test_unknown_container_falls_back_to_mkv(self):
        cmd = handbrake.rip_command("/dev/sr1", 1, "/out/a.mkv", cli="HB",
                                    nice=0, container="avi")
        self.assertEqual(cmd[cmd.index("--format") + 1], "av_mkv")
        self.assertEqual(handbrake.output_extension("avi"), ".mkv")


class TestOutputPath(unittest.TestCase):
    """_rip_output_path keeps every rip inside the configured directory."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_basic(self):
        path = app._rip_output_path(self.tmp, "Step_Brothers", ".mkv")
        self.assertEqual(path, os.path.join(self.tmp, "Step Brothers.mkv"))

    def test_collisions_get_a_suffix(self):
        first = app._rip_output_path(self.tmp, "Film", ".mkv")
        open(first, "w").close()
        second = app._rip_output_path(self.tmp, "Film", ".mkv")
        self.assertEqual(os.path.basename(second), "Film (2).mkv")
        open(second, "w").close()
        third = app._rip_output_path(self.tmp, "Film", ".mkv")
        self.assertEqual(os.path.basename(third), "Film (3).mkv")

    def test_traversal_cannot_escape(self):
        path = app._rip_output_path(self.tmp, "../../etc/shadow", ".mkv")
        self.assertEqual(os.path.dirname(path), os.path.realpath(self.tmp))

    def test_absolute_name_cannot_escape(self):
        path = app._rip_output_path(self.tmp, "/etc/shadow", ".mkv")
        self.assertEqual(os.path.dirname(path), os.path.realpath(self.tmp))


class TestSelectTracks(unittest.TestCase):
    AVAILABLE = [{"index": 1}, {"index": 2}, {"index": 4}]

    def test_valid(self):
        self.assertEqual(app._select_tracks([1, 4], self.AVAILABLE, "audio"),
                         [1, 4])

    def test_none_means_empty(self):
        self.assertEqual(app._select_tracks(None, self.AVAILABLE, "audio"), [])

    def test_duplicates_collapse(self):
        self.assertEqual(app._select_tracks([2, 2], self.AVAILABLE, "audio"), [2])

    def test_unknown_track_rejected(self):
        with self.assertRaises(ValueError):
            app._select_tracks([3], self.AVAILABLE, "audio")

    def test_non_numeric_rejected(self):
        with self.assertRaises(ValueError):
            app._select_tracks(["x"], self.AVAILABLE, "audio")

    def test_non_list_rejected(self):
        with self.assertRaises(ValueError):
            app._select_tracks("1,2", self.AVAILABLE, "audio")


class TestRipSettings(unittest.TestCase):
    """Stored settings are clamped so a hand-edited config can't break a rip."""

    def _settings(self, stored):
        with mock.patch.object(app, "_read_app_config", return_value={"rip": stored}):
            return app._rip_settings()

    def test_defaults(self):
        s = self._settings({})
        self.assertEqual(s["container"], handbrake.DEFAULT_CONTAINER)
        self.assertEqual(s["preset"], handbrake.DEFAULT_PRESET)
        self.assertEqual(s["max_concurrent"], 1)

    def test_overrides_applied(self):
        s = self._settings({"output_dir": "/srv/rips", "preset": "Fast 480p30",
                            "container": "mp4", "eject_when_done": True})
        self.assertEqual(s["output_dir"], "/srv/rips")
        self.assertEqual(s["preset"], "Fast 480p30")
        self.assertEqual(s["container"], "mp4")
        self.assertTrue(s["eject_when_done"])

    def test_out_of_range_values_clamped(self):
        s = self._settings({"nice": 99, "max_concurrent": 50,
                            "min_title_seconds": -5})
        self.assertEqual(s["nice"], 19)
        self.assertEqual(s["max_concurrent"], 4)
        self.assertEqual(s["min_title_seconds"], 0)

    def test_garbage_values_fall_back(self):
        s = self._settings({"nice": "loud", "max_concurrent": None,
                            "min_title_seconds": "x", "container": "avi"})
        self.assertEqual(s["nice"], app.config.RIP_NICE)
        self.assertEqual(s["max_concurrent"], 1)
        self.assertEqual(s["min_title_seconds"], handbrake.DEFAULT_MIN_TITLE_SEC)
        self.assertEqual(s["container"], handbrake.DEFAULT_CONTAINER)


class TestRipStateReconcile(unittest.TestCase):
    """A HandBrake child dies with the dashboard, so a 'ripping' entry found
    at startup is stale and its partial output is unusable."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        state_file = os.path.join(self.tmp, "rip_state.json")
        patcher = mock.patch.object(app, "RIP_STATE_FILE", state_file)
        patcher.start()
        self.addCleanup(patcher.stop)
        # The module-level cache is shared; reset it around each test.
        app._rip_state_cache = {}
        app._rip_state_last_flush = 0.0
        self.addCleanup(setattr, app, "_rip_state_cache", None)
        self.log = mock.patch.object(app.db, "log_event")
        self.log.start()
        self.addCleanup(self.log.stop)

    def test_ripping_entry_becomes_interrupted(self):
        partial = os.path.join(self.tmp, "Half.mkv")
        with open(partial, "wb") as f:
            f.write(b"x" * 32)
        app.update_rip_entry("/dev/sr1", status="ripping", percent=42.0,
                             output_path=partial, output_name="Half.mkv")

        self.assertEqual(app._reconcile_interrupted_rips(), ["/dev/sr1"])
        entry = app.load_rip_state()["/dev/sr1"]
        self.assertEqual(entry["status"], "interrupted")
        self.assertTrue(entry["error_message"])
        self.assertTrue(entry["finished_at"])
        # Nothing can resume a partial encode — it must not be left behind.
        self.assertFalse(os.path.exists(partial))

    def test_terminal_and_idle_entries_untouched(self):
        app.update_rip_entry("/dev/sr0", status="done", percent=100.0)
        app.update_rip_entry("/dev/sr1", status="idle")
        self.assertEqual(app._reconcile_interrupted_rips(), [])
        state = app.load_rip_state()
        self.assertEqual(state["/dev/sr0"]["status"], "done")
        self.assertEqual(state["/dev/sr1"]["status"], "idle")

    def test_scanning_entry_also_reconciled(self):
        app.update_rip_entry("/dev/sr1", status="scanning")
        self.assertEqual(app._reconcile_interrupted_rips(), ["/dev/sr1"])

    def test_replace_clears_previous_run(self):
        app.update_rip_entry("/dev/sr1", status="error",
                             error_message="boom", percent=13.0)
        app.update_rip_entry("/dev/sr1", _replace=True, status="ripping")
        entry = app.load_rip_state()["/dev/sr1"]
        self.assertEqual(entry["status"], "ripping")
        # A new rip must not inherit the last failure's message or progress.
        self.assertNotIn("error_message", entry)
        self.assertNotIn("percent", entry)

    def test_state_survives_a_reload(self):
        app.update_rip_entry("/dev/sr1", status="done", output_name="A.mkv")
        app._rip_state_cache = None  # force a read from disk
        self.assertEqual(app.load_rip_state()["/dev/sr1"]["output_name"], "A.mkv")


class TestRipErrorMessage(unittest.TestCase):
    def test_picks_the_last_meaningful_log_line(self):
        tail = ["[12:00:00] starting", "Encoding: task 1",
                "libhb: work result = 4", "ERROR: No space left on device"]
        self.assertEqual(app._rip_error_message(1, tail),
                         "ERROR: No space left on device")

    def test_falls_back_to_exit_code(self):
        self.assertEqual(app._rip_error_message(3, ["all fine here"]),
                         "HandBrake exited with code 3")
        self.assertEqual(app._rip_error_message(3, []),
                         "HandBrake exited with code 3")


class TestScanFailureReason(unittest.TestCase):
    """Scan failures get an actionable message rather than a raw traceback."""

    def _reason(self, stderr):
        res = mock.Mock(stderr=stderr, stdout="")
        return handbrake._scan_failure_reason(res, ValueError("no title set"))

    def test_missing_device(self):
        self.assertIn("not found",
                      self._reason("open /dev/sr9: No such file or directory"))

    def test_permission(self):
        self.assertIn("permission",
                      self._reason("dvd: Permission denied opening /dev/sr0"))

    def test_blank_disc(self):
        self.assertIn("blank", self._reason("scan: No title found."))

    def test_unknown(self):
        self.assertIn("scan failed", self._reason("something odd happened"))


if __name__ == "__main__":
    unittest.main()
