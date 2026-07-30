"""Fake HandBrake backend for local UI work (USE_MOCK=true).

The dev box has neither HandBrakeCLI nor an optical drive, so without this
the Rips tab would be permanently greyed out and unbuildable locally. The
fixture mirrors a real scan of a retail DVD: two long titles (theatrical and
unrated cuts, which is what makes main-title picking interesting), a short
extra, AC3 tracks with a commentary, and VOBSUB subtitles.
"""

import os
import time

from handbrake import (
    DEFAULT_MIN_TITLE_SEC,
    DEFAULT_SCAN_TIMEOUT,
    HandBrake,
    HandBrakeError,
    fmt_duration,
)

_MOCK_DRIVES = [
    {
        "device": "/dev/sr0",
        "model": "MATSHITADVD-RAM UJ8G2",
        "bus": "ata",
        "has_disc": False,
        "disc_label": None,
        "media_type": None,
        "media_state": None,
        "readable": True,
    },
    {
        "device": "/dev/sr1",
        "model": "DVD-RW DS8A8SH",
        "bus": "usb",
        "has_disc": True,
        "disc_label": "Step Brothers",
        "media_type": "dvd",
        "media_state": "complete",
        "readable": True,
    },
]


def _audio(index, language, channels, layout, bitrate, commentary=False):
    codec = "ac3"
    desc = f"{language} ({codec.upper()}, {layout}, {bitrate // 1000} kbps)"
    if commentary:
        desc += " (commentary)"
    return {
        "index": index,
        "language": language,
        "language_code": language[:3].lower(),
        "codec": codec,
        "channels": channels,
        "channel_layout": layout,
        "bitrate": bitrate,
        "description": desc,
        "commentary": commentary,
        "default": index == 1,
    }


def _sub(index, language, forced=False):
    return {
        "index": index,
        "language": language,
        "language_code": language[:3].lower(),
        "format": "bitmap",
        "source": "VOBSUB",
        "forced": forced,
        "closed_caption": False,
        "commentary": False,
    }


def _title(index, seconds, chapters):
    return {
        "index": index,
        "name": "Step Brothers",
        "duration_seconds": seconds,
        "duration": fmt_duration(seconds),
        "chapters": chapters,
        "width": 720,
        "height": 480,
        "fps": 23.976,
        # Film-sourced DVDs are flagged progressive, like the disc this
        # fixture was modelled on.
        "interlaced": False,
        "angles": 1,
        "audio": [
            _audio(1, "English", 6, "5.1(side)", 448000),
            _audio(2, "English", 2, "stereo", 192000, commentary=True),
            _audio(3, "Spanish", 2, "stereo", 192000),
        ],
        "subtitles": [
            _sub(1, "English (Wide Screen) [VOBSUB]"),
            _sub(2, "Spanish [VOBSUB]"),
            _sub(3, "English (Forced) [VOBSUB]", forced=True),
        ],
    }


_MOCK_TITLES = [
    _title(1, 6326, 28),   # 1:45:26 — unrated cut, the longest
    _title(2, 5862, 28),   # 1:37:42 — theatrical cut
    _title(5, 48, 5),      # studio ident
    _title(10, 1324, 2),   # extras reel
]

_MOCK_PRESETS = [
    {"group": "General", "presets": [
        {"name": "Fast 576p25", "description": "H.264 video (up to 576p25) and AAC stereo audio, in an MP4 container."},
        {"name": "Fast 1080p30", "description": "H.264 video (up to 1080p30) and AAC stereo audio, in an MP4 container."},
        {"name": "HQ 576p25 Surround", "description": "High quality H.264 video (up to 576p25), AAC stereo audio, and Dolby Digital (AC-3) surround audio, in an MP4 container."},
        {"name": "HQ 1080p30 Surround", "description": "High quality H.264 video (up to 1080p30), AAC stereo audio, and Dolby Digital (AC-3) surround audio, in an MP4 container."},
        {"name": "Super HQ 576p25 Surround", "description": "Super high quality H.264 video (up to 576p25), AAC stereo audio, and Dolby Digital (AC-3) surround audio, in an MP4 container."},
    ]},
    {"group": "Matroska", "presets": [
        {"name": "H.264 MKV 576p25", "description": "H.264 video (up to 576p25), AAC stereo audio, and AC-3 surround audio, in an MKV container."},
        {"name": "H.265 MKV 576p25", "description": "H.265 video (up to 576p25), AAC stereo audio, and AC-3 surround audio, in an MKV container."},
    ]},
]

# Wall-clock length of a simulated rip. Long enough to watch the progress
# card update and exercise cancelling, short enough not to be tedious.
_MOCK_RIP_SECONDS = float(os.getenv("MOCK_RIP_SECONDS", "45"))


class MockHandBrake(HandBrake):
    def available(self):
        return True

    def version(self):
        return "HandBrake 1.11.0 (mock)"

    def drives(self):
        return [dict(d) for d in _MOCK_DRIVES]

    def eject(self, device):
        for d in _MOCK_DRIVES:
            if d["device"] == device:
                d.update(has_disc=False, disc_label=None,
                         media_type=None, media_state=None)
                return
        raise HandBrakeError("drive not found")

    def presets(self):
        return [dict(g, presets=[dict(p) for p in g["presets"]])
                for g in _MOCK_PRESETS]

    def scan(self, device, min_seconds=DEFAULT_MIN_TITLE_SEC,
             timeout=DEFAULT_SCAN_TIMEOUT):
        drive = next((d for d in _MOCK_DRIVES if d["device"] == device), None)
        if drive is None:
            raise HandBrakeError("drive not found")
        if not drive["has_disc"]:
            raise HandBrakeError("no disc in the drive")
        time.sleep(2)  # a real DVD scan is far from instant
        titles = [t for t in _MOCK_TITLES if t["duration_seconds"] >= min_seconds]
        return {
            "titles": [dict(t) for t in titles],
            "main_index": titles[0]["index"] if titles else None,
            "disc_name": drive["disc_label"],
        }

    def run_rip(self, cmd, on_progress=None, cancelled=None):
        """Simulate an encode: a short scan pass, then WORKING, then MUXING."""
        output = cmd[cmd.index("--output") + 1] if "--output" in cmd else None

        if on_progress:
            on_progress({"state": "SCANNING", "percent": 0.0})
        started = time.monotonic()
        while True:
            if cancelled is not None and cancelled():
                return 1, ["mock: cancelled"]
            elapsed = time.monotonic() - started
            frac = min(1.0, elapsed / _MOCK_RIP_SECONDS)
            if on_progress:
                on_progress({
                    "state": "WORKING",
                    "percent": round(frac * 100, 1),
                    "fps": 42.5,
                    "avg_fps": 40.1,
                    "eta_seconds": int(_MOCK_RIP_SECONDS - elapsed),
                    "pass": 1,
                    "pass_count": 1,
                    "paused": False,
                })
            if frac >= 1.0:
                break
            time.sleep(1)

        if on_progress:
            on_progress({"state": "MUXING", "percent": 100.0})
        if output:
            try:
                os.makedirs(os.path.dirname(output), exist_ok=True)
                with open(output, "wb") as f:
                    f.write(b"\0" * 1024 * 512)  # stand-in, not real video
            except OSError as e:
                return 1, [f"mock: could not write output: {e}"]
        if on_progress:
            on_progress({"state": "WORKDONE", "error_code": 0})
        return 0, ["mock: encode finished"]
