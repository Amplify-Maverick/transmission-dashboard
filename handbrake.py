"""HandBrakeCLI wrapper: optical drives, disc scanning, and rip execution.

Split the same way as transmission.py / mock_transmission.py: the parsing
helpers here are pure functions (unit-testable with captured CLI output),
while everything that touches a real drive or spawns a process lives on the
`HandBrake` class so mock_handbrake.py can override just the I/O for local
UI work.

Two things about HandBrakeCLI shape the design:

  * With `--json` it writes pretty-printed JSON *blocks* to stdout, each
    introduced by a `Name: {` line ("Version:", "Progress:", "JSON Title
    Set:"), interleaved with plain log lines. There is no NDJSON mode, so
    progress has to be read as multi-line blocks.
  * `--preset-list` goes to **stderr**, not stdout.
"""

import glob
import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque

import config

# HandBrake reports durations as an {Hours, Minutes, Seconds, Ticks} dict.
# Ticks are 90kHz units; we only use the H/M/S fields, which are exact.
_TICKS_PER_SEC = 90000

# A retail DVD carries dozens of junk titles (studio idents, menu loops,
# per-scene chapter stubs). Titles shorter than this are hidden from the
# scan by HandBrake itself, which keeps the picker readable.
DEFAULT_MIN_TITLE_SEC = 20

# A scan reads the disc structure and decrypts a few previews; on a scratched
# disc it can wedge, so it always runs under a timeout.
DEFAULT_SCAN_TIMEOUT = 420

# Containers we offer. MKV is the default because DVD audio is AC3 and DVD
# subtitles are VOBSUB bitmaps — MP4 cannot carry VOBSUB at all, so an MP4
# rip silently loses subtitles unless they are burned in.
CONTAINERS = {
    "mkv": {"format": "av_mkv", "ext": ".mkv"},
    "mp4": {"format": "av_mp4", "ext": ".mp4"},
}
DEFAULT_CONTAINER = "mkv"

# DVD video is 480i/576i, so the sensible default preset is one of
# HandBrake's 576p25 presets rather than a 1080p one that would upscale.
DEFAULT_PRESET = "HQ 576p25 Surround"

# Encoding pins both cores of the T440p flat out, and transmission-daemon is
# single-thread-bound on the same box. Ripping runs niced by default so a rip
# never starves the daemon's event loop.
DEFAULT_NICE = 15

_LOG_TAIL_LINES = 40

# `udevadm info --query=property` emits KEY=VALUE lines.
_UDEV_PROP_RE = re.compile(r"^([A-Za-z0-9_]+)=(.*)$")

# A JSON block header: `Progress: {`, `Version: {`, `JSON Title Set: {`.
_JSON_BLOCK_RE = re.compile(r"^([A-Za-z][A-Za-z ]*): \{\s*$")

# Filesystem-hostile characters in an output filename. Windows-hostile ones
# are included too, since rips get copied onto a shared media library.
_UNSAFE_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# ---------- pure helpers ----------

def duration_seconds(d):
    """Seconds from HandBrake's duration dict (or a bare number)."""
    if isinstance(d, (int, float)):
        return int(d)
    if not isinstance(d, dict):
        return 0
    if any(k in d for k in ("Hours", "Minutes", "Seconds")):
        return (
            int(d.get("Hours") or 0) * 3600
            + int(d.get("Minutes") or 0) * 60
            + int(d.get("Seconds") or 0)
        )
    ticks = d.get("Ticks")
    return int(int(ticks) / _TICKS_PER_SEC) if ticks else 0


def fmt_duration(seconds):
    """`1:45:26` for feature lengths, `4:17` for shorts."""
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def sanitize_filename(name, fallback="disc"):
    """Make `name` safe to use as a single path component.

    Rejects path separators outright rather than substituting them, so a
    user-supplied name can never escape the configured output directory.
    """
    name = (name or "").strip()
    # Underscores in a disc label ("Step_Brothers") are a mastering artifact,
    # not the film's title — spaces read better in a media library.
    name = name.replace("_", " ")
    # Substitute rather than delete: deleting glues words together
    # ("The?Sequel" -> "TheSequel") and swallows tabs entirely.
    name = _UNSAFE_NAME_RE.sub(" ", name)
    name = re.sub(r"\s+", " ", name).strip()
    # Leading dots would hide the file; trailing dots/spaces break on SMB.
    name = name.strip(". ")
    if not name:
        return fallback
    return name[:150]


def _fps(frame_rate):
    """HandBrake reports frame rate as a Num/Den pair."""
    if not isinstance(frame_rate, dict):
        return None
    num, den = frame_rate.get("Num"), frame_rate.get("Den")
    if not num or not den:
        return None
    return round(float(num) / float(den), 3)


def _audio_track(a, i):
    return {
        "index": a.get("TrackNumber") or i,
        "language": a.get("Language") or "Unknown",
        "language_code": a.get("LanguageCode"),
        "codec": a.get("CodecName"),
        "channels": a.get("ChannelCount"),
        "channel_layout": a.get("ChannelLayout"),
        "bitrate": a.get("BitRate"),
        "description": a.get("Description") or a.get("Language") or "Audio",
        "commentary": bool((a.get("Attributes") or {}).get("Commentary")),
        "default": bool((a.get("Attributes") or {}).get("Default")),
    }


def _subtitle_track(s, i):
    attrs = s.get("Attributes") or {}
    return {
        "index": s.get("TrackNumber") or i,
        # DVD subtitle "Language" often carries the presentation variant too
        # ("English (Wide Screen) [VOBSUB]"), which is worth showing as-is.
        "language": s.get("Language") or "Unknown",
        "language_code": s.get("LanguageCode"),
        "format": s.get("Format"),
        "source": s.get("SourceName"),
        "forced": bool(attrs.get("Forced")),
        "closed_caption": bool(attrs.get("ClosedCaption")),
        "commentary": bool(attrs.get("Commentary")),
    }


def parse_scan(stdout, min_seconds=0):
    """Parse `HandBrakeCLI --scan --json` stdout into a title list.

    Returns {"titles": [...], "main_index": int|None, "disc_name": str|None}.
    Raises ValueError when no title set is present (no disc, unreadable disc,
    or a scan that died before emitting one).
    """
    marker = "JSON Title Set: "
    idx = stdout.find(marker)
    if idx < 0:
        raise ValueError("no title set in scan output")
    try:
        obj, _ = json.JSONDecoder().raw_decode(stdout[idx + len(marker):])
    except ValueError as e:
        raise ValueError(f"unreadable title set: {e}") from e

    titles = []
    for t in obj.get("TitleList") or []:
        secs = duration_seconds(t.get("Duration"))
        if min_seconds and secs < min_seconds:
            continue
        geo = t.get("Geometry") or {}
        titles.append({
            "index": t.get("Index"),
            "name": t.get("Name"),
            "duration_seconds": secs,
            "duration": fmt_duration(secs),
            "chapters": len(t.get("ChapterList") or []),
            "width": geo.get("Width"),
            "height": geo.get("Height"),
            "fps": _fps(t.get("FrameRate")),
            "interlaced": bool(t.get("InterlaceDetected")),
            "angles": t.get("AngleCount") or 1,
            "audio": [_audio_track(a, i + 1)
                      for i, a in enumerate(t.get("AudioList") or [])],
            "subtitles": [_subtitle_track(s, i + 1)
                          for i, s in enumerate(t.get("SubtitleList") or [])],
        })

    # HandBrake's own MainFeature detection is only populated for Blu-ray
    # playlists; on DVDs it comes back -1, so fall back to the longest title.
    main = obj.get("MainFeature")
    main_index = main if isinstance(main, int) and main > 0 else pick_main_title(titles)

    disc_name = None
    for t in titles:
        if t.get("name"):
            disc_name = t["name"]
            break

    return {"titles": titles, "main_index": main_index, "disc_name": disc_name}


def pick_main_title(titles):
    """Index of the longest title, or None. The feature is essentially always
    the longest thing on a DVD; extras and idents are far shorter."""
    if not titles:
        return None
    return max(titles, key=lambda t: t.get("duration_seconds") or 0).get("index")


def iter_json_blocks(lines):
    """Yield (kind, obj) for each pretty-printed JSON block in `lines`.

    HandBrake indents block bodies, so the top-level closing brace is the
    only `}` at column 0 — that, not brace counting, delimits a block. Brace
    counting would misfire on a disc label containing a brace.
    """
    kind = None
    buf = []
    for raw in lines:
        line = raw.rstrip("\r\n")
        if kind is None:
            m = _JSON_BLOCK_RE.match(line)
            if m:
                kind = m.group(1)
                buf = ["{"]
            continue
        buf.append(line)
        if line == "}":
            try:
                yield kind, json.loads("\n".join(buf))
            except ValueError:
                pass
            kind = None
            buf = []


def parse_progress(obj):
    """Normalize a HandBrake `Progress:` block into flat UI-ready fields.

    States seen during a rip: SCANNING (pre-pass), WORKING (encoding),
    MUXING, WORKDONE. Only WORKING carries rate/ETA.
    """
    state = (obj.get("State") or "").upper()
    out = {"state": state}
    body = obj.get("Working") or obj.get("Scanning") or obj.get("Muxing") or {}
    if state == "WORKDONE":
        out["error_code"] = int((obj.get("WorkDone") or {}).get("Error") or 0)
        return out
    pct = body.get("Progress")
    if isinstance(pct, (int, float)):
        out["percent"] = round(max(0.0, min(1.0, float(pct))) * 100, 1)
    if state == "WORKING":
        out["fps"] = round(float(body.get("Rate") or 0), 1) or None
        out["avg_fps"] = round(float(body.get("RateAvg") or 0), 1) or None
        eta = body.get("ETASeconds")
        out["eta_seconds"] = int(eta) if isinstance(eta, (int, float)) else None
        out["pass"] = body.get("Pass")
        out["pass_count"] = body.get("PassCount")
        out["paused"] = bool(body.get("Paused"))
    return out


def parse_preset_list(text):
    """Parse `HandBrakeCLI --preset-list` (stderr) into groups.

    Format: group names at column 0 ending in `/`, preset names indented 4,
    wrapped descriptions indented 8. libhb's own startup log lines are
    interleaved and get skipped.
    """
    groups = []
    current = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("["):
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        if indent == 0:
            if stripped.endswith("/"):
                current = {"group": stripped[:-1], "presets": []}
                groups.append(current)
            continue
        if indent == 4 and current is not None:
            current["presets"].append({"name": stripped, "description": ""})
        elif indent >= 8 and current is not None and current["presets"]:
            last = current["presets"][-1]
            last["description"] = (last["description"] + " " + stripped).strip()
    return [g for g in groups if g["presets"]]


def parse_udev_props(text):
    props = {}
    for line in text.splitlines():
        m = _UDEV_PROP_RE.match(line.strip())
        if m:
            props[m.group(1)] = m.group(2)
    return props


def drive_from_props(device, props):
    """Build a drive record from udev properties.

    ID_CDROM_MEDIA_* only appear when a disc is loaded, which is how disc
    presence is detected — no need to open the device.
    """
    media_types = [k[len("ID_CDROM_MEDIA_"):].lower()
                   for k in props
                   if k.startswith("ID_CDROM_MEDIA_") and props[k] == "1"
                   and k not in ("ID_CDROM_MEDIA_STATE",)]
    has_disc = bool(props.get("ID_CDROM_MEDIA")
                    or props.get("ID_CDROM_MEDIA_STATE")
                    or props.get("ID_FS_TYPE")
                    or media_types)
    label = props.get("ID_FS_LABEL_ENC") or props.get("ID_FS_LABEL") or ""
    if label:
        # udev escapes non-printables as \x20 sequences.
        label = label.replace("\\x20", " ").strip()
    kind = "dvd" if "dvd" in media_types else ("bd" if "bd" in media_types else None)
    if kind is None and media_types:
        kind = "cd" if any(m.startswith("cd") for m in media_types) else media_types[0]
    return {
        "device": device,
        "model": (props.get("ID_MODEL_ENC") or props.get("ID_MODEL") or "")
                 .replace("\\x20", " ").strip() or os.path.basename(device),
        "bus": props.get("ID_BUS"),
        "has_disc": has_disc,
        "disc_label": label or None,
        "media_type": kind,
        # A freshly-inserted disc reports "blank"/"loading" until the drive
        # finishes reading the TOC; scanning then fails, so the UI says wait.
        "media_state": props.get("ID_CDROM_MEDIA_STATE"),
        "readable": os.access(device, os.R_OK),
    }


def scan_command(device, cli=None, min_seconds=DEFAULT_MIN_TITLE_SEC):
    return [
        cli or cli_path(),
        "--input", device,
        "--scan",
        "--title", "0",          # 0 = every title
        "--min-duration", str(int(min_seconds)),
        "--json",
    ]


def output_extension(container):
    return CONTAINERS.get(container, CONTAINERS[DEFAULT_CONTAINER])["ext"]


def rip_command(device, title, output, preset=DEFAULT_PRESET,
                container=DEFAULT_CONTAINER, audio=None, subtitles=None,
                burn_subtitle=False, chapters=True, cli=None, nice=DEFAULT_NICE):
    """Build the encode command.

    Track numbers are HandBrake's 1-based per-title indices, exactly as
    reported by the scan.
    """
    fmt = CONTAINERS.get(container, CONTAINERS[DEFAULT_CONTAINER])["format"]
    cmd = []
    if nice and int(nice) > 0:
        cmd += ["nice", "-n", str(int(nice))]
    cmd += [
        cli or cli_path(),
        "--input", device,
        "--title", str(int(title)),
        "--preset", preset,
        "--format", fmt,
        "--output", output,
        "--json",
    ]
    if audio:
        cmd += ["--audio", ",".join(str(int(a)) for a in audio)]
    if subtitles:
        cmd += ["--subtitle", ",".join(str(int(s)) for s in subtitles)]
        if burn_subtitle:
            # Burn the first *selected* track (1-based within the selection).
            cmd += ["--subtitle-burned=1"]
    if chapters:
        cmd += ["--markers"]
    return cmd


def cli_path():
    return config.HANDBRAKE_CLI


# ---------- the I/O layer ----------

class HandBrakeError(Exception):
    pass


class HandBrake:
    """Everything that touches a real drive or spawns HandBrakeCLI."""

    def available(self):
        return bool(shutil.which(cli_path()))

    def version(self):
        if not self.available():
            return None
        try:
            out = subprocess.run(
                [cli_path(), "--version"],
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        for line in (out.stdout or "").splitlines():
            if line.lower().startswith("handbrake"):
                return line.strip()
        return None

    def drives(self):
        """Enumerate optical drives, newest-disc info included."""
        out = []
        for device in sorted(glob.glob("/dev/sr[0-9]*")):
            props = {}
            try:
                res = subprocess.run(
                    ["udevadm", "info", "--query=property", "--name", device],
                    capture_output=True, text=True, timeout=10,
                )
                if res.returncode == 0:
                    props = parse_udev_props(res.stdout)
            except (OSError, subprocess.SubprocessError):
                # No udevadm (or it failed) — still list the drive, just with
                # unknown disc state rather than dropping it from the UI.
                pass
            out.append(drive_from_props(device, props))
        return out

    def eject(self, device):
        try:
            res = subprocess.run(
                ["eject", device], capture_output=True, text=True, timeout=30,
            )
        except FileNotFoundError as e:
            raise HandBrakeError("eject is not installed") from e
        except (OSError, subprocess.SubprocessError) as e:
            raise HandBrakeError(str(e)) from e
        if res.returncode != 0:
            raise HandBrakeError((res.stderr or "eject failed").strip())

    def presets(self):
        """Grouped preset list. Empty list when HandBrake is missing."""
        if not self.available():
            return []
        try:
            res = subprocess.run(
                [cli_path(), "--preset-list"],
                capture_output=True, text=True, timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        # --preset-list writes to stderr; stdout is checked too in case a
        # future release moves it.
        return parse_preset_list(res.stderr or "") or parse_preset_list(res.stdout or "")

    def scan(self, device, min_seconds=DEFAULT_MIN_TITLE_SEC,
             timeout=DEFAULT_SCAN_TIMEOUT):
        """Scan a disc and return parse_scan()'s dict. Raises HandBrakeError."""
        if not self.available():
            raise HandBrakeError("HandBrakeCLI is not installed")
        cmd = scan_command(device, min_seconds=min_seconds)
        try:
            res = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise HandBrakeError(
                f"scan timed out after {timeout}s — disc may be damaged"
            ) from e
        except OSError as e:
            raise HandBrakeError(str(e)) from e
        try:
            return parse_scan(res.stdout or "", min_seconds=min_seconds)
        except ValueError as e:
            raise HandBrakeError(_scan_failure_reason(res, e)) from e

    def run_rip(self, cmd, on_progress=None, cancelled=None):
        """Run an encode, feeding normalized progress to `on_progress`.

        Returns (returncode, log_tail). `cancelled` is polled between
        progress blocks; when it goes true the process is terminated and the
        return code will be non-zero.
        """
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1,
            )
        except OSError as e:
            raise HandBrakeError(str(e)) from e

        tail = deque(maxlen=_LOG_TAIL_LINES)
        # stderr is HandBrake's log stream and can outrun any pipe buffer, so
        # it has to be drained concurrently or the encode deadlocks on write.
        def drain_stderr():
            for line in proc.stderr:
                tail.append(line.rstrip())

        t = threading.Thread(target=drain_stderr, daemon=True)
        t.start()

        killed = False
        try:
            for kind, obj in iter_json_blocks(proc.stdout):
                if cancelled is not None and cancelled():
                    killed = True
                    _terminate(proc)
                    break
                if kind == "Progress" and on_progress is not None:
                    on_progress(parse_progress(obj))
        finally:
            if not killed and cancelled is not None and cancelled():
                _terminate(proc)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            t.join(timeout=5)
            for s in (proc.stdout, proc.stderr):
                try:
                    s.close()
                except OSError:
                    pass
        return proc.returncode, list(tail)


def _scan_failure_reason(res, err):
    """Turn a failed scan into something a user can act on."""
    blob = ((res.stderr or "") + (res.stdout or "")).lower()
    if "no such file or directory" in blob:
        return "drive not found"
    if "permission denied" in blob:
        return "permission denied reading the drive"
    if "no title found" in blob or "unrecognized file type" in blob:
        return "no readable titles — disc may be blank, dirty, or unsupported"
    if "css" in blob and "error" in blob:
        return "could not decrypt the disc (libdvdcss missing or unknown CSS key)"
    return f"scan failed: {err}"


def _terminate(proc):
    """SIGTERM, then SIGKILL if HandBrake doesn't fold quickly."""
    try:
        proc.terminate()
    except OSError:
        return
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.2)
    try:
        proc.kill()
    except OSError:
        pass
