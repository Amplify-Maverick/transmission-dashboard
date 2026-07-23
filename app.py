import base64
import csv
import hmac
import io
import json
import os
import queue
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import wraps

import psutil
import requests
from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

import config
import db

if config.USE_MOCK:
    from mock_transmission import MockTransmissionClient as TransmissionClient
else:
    from transmission import TransmissionClient

app = Flask(__name__)
app.secret_key = config.FLASK_SECRET_KEY
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=24)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Strict"

DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "")
DASHBOARD_PASS = os.environ.get("DASHBOARD_PASS", "")

client = TransmissionClient()

db.init()


def _startup_config_check():
    """Print a clear warning listing any missing required configuration.

    Nothing here aborts startup — the app still boots so the operator can
    reach the login page and read the message — but surfacing the gaps up
    front beats a confusing 401/500 the first time a feature is used.
    """
    missing = []
    if not DASHBOARD_USER or not DASHBOARD_PASS:
        missing.append(
            "DASHBOARD_USER / DASHBOARD_PASS — login is DISABLED until both are "
            "set, so the dashboard is currently inaccessible"
        )
    if not config.USE_MOCK and (not config.TR_USER or not config.TR_PASS):
        missing.append(
            "TR_USER / TR_PASS — Transmission RPC credentials (set USE_MOCK=true "
            "for development without a real daemon)"
        )
    if not config.TUNNEL_IFACE:
        missing.append(
            "TUNNEL_IFACE — WireGuard interface name for the tunnel indicator "
            "(leave unset to hide the indicator)"
        )
    if missing:
        border = "=" * 72
        print(border, flush=True)
        print("transmission-dashboard: configuration warnings", flush=True)
        print("Fill these in .env (copy .env.example) — see README.md:", flush=True)
        for m in missing:
            print(f"  - {m}", flush=True)
        print(border, flush=True)


_startup_config_check()


def _torrent_name(tid):
    try:
        detail = client.get_torrent_detail(tid)
        if detail:
            return detail.get("name") or f"torrent {tid}"
    except Exception:
        pass
    return f"torrent {tid}"


def _torrent_hash(tid):
    """The torrent's infohash, or None if it can't be read.

    Transmission's numeric id is session-scoped — it is reassigned when the
    daemon restarts and reused after a removal — so it is not a stable
    identity. The infohash is; copy-state entries carry it so a stale entry
    can't be inherited by whatever torrent later lands on the same id.
    """
    try:
        detail = client.get_torrent_detail(tid)
        if detail:
            return detail.get("hashString") or None
    except Exception:
        pass
    return None


def _copy_severity(status):
    if status == "done":
        return "info"
    if status == "cancelled":
        return "info"
    if status == "interrupted":
        # Nothing was corrupted — the worker just died mid-transfer. Retrying
        # resumes, so this is a warning rather than a failure.
        return "warn"
    return "error"

def _atomic_write_json(path, data):
    """Write JSON to `path` via tmp + os.replace so concurrent readers never
    see a half-written file. Truncate-and-rewrite (the previous approach) made
    a torn read look like a corrupt file and silently dropped all state."""
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

# ---------- copy-to-media-server state ----------

MEDIA_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "media_config.json")
COPY_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "copy_state.json")
COPIED_LABEL = "Copied"
_copy_state_lock = threading.Lock()
_copy_state_cache = None
_copy_state_last_flush = 0.0
# Progress ticks flush at most this often. The API serves reads from the
# in-memory cache and terminal states force-persist, so the periodic flush
# only matters for crash recovery — once per second was needless disk I/O
# for the whole duration of every copy.
_COPY_STATE_FLUSH_MIN_INTERVAL = 15.0
_active_copies = {}
_active_copies_lock = threading.Lock()

# Liveness for the long-lived rsync transports. Short-lived helpers (df,
# mkdir, verify) only need ConnectTimeout because they finish in seconds; a
# copy can run for hours, so it also needs to notice a connection that has
# gone away *after* it was established. Without these, a silently dead peer
# (VPN relay reboot, NAT idle-eviction, media server sleeping a disk) leaves
# rsync blocked in a read that never returns and no timeout ever fires.
#
# ServerAlive* gives up after ~3min of an unresponsive peer; rsync's own
# --timeout is the backstop for a transport that stays up while data stops
# flowing. It is deliberately much longer, since rsync can legitimately go
# quiet while checksumming a large file.
_COPY_SSH_CONNECT_TIMEOUT = 30
_COPY_SSH_ALIVE_INTERVAL = 30
_COPY_SSH_ALIVE_COUNT_MAX = 6
_COPY_RSYNC_IO_TIMEOUT = 600


def _copy_ssh_transport(port):
    """`rsync -e` transport with connect + keepalive timeouts applied."""
    return (
        "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
        f" -o ConnectTimeout={_COPY_SSH_CONNECT_TIMEOUT}"
        f" -o ServerAliveInterval={_COPY_SSH_ALIVE_INTERVAL}"
        f" -o ServerAliveCountMax={_COPY_SSH_ALIVE_COUNT_MAX}"
        f" -p {port}"
    )

# rsync --info=progress2 emits lines like:
#   "       16,712,704  35%   15.94MB/s    0:00:01 (xfr#1, to-chk=1/3)"
# Bytes carry thousands separators; rate and eta are formatted by rsync.
COPY_PROGRESS_RE = re.compile(
    r"^\s*([\d,]+)\s+(\d+)%\s+(\S+/s)\s+(\d+):(\d+):(\d+)"
)

def _err(msg, code=500):
    return jsonify({"ok": False, "error": str(msg)}), code


# A re-auth URL minted by tailscaled — printed on stderr by `tailscale ssh`
# and exposed via `tailscale status --json` when BackendState == NeedsLogin.
_TAILSCALE_LOGIN_URL_RE = re.compile(r"https://login\.tailscale\.com/[A-Za-z0-9/_\-]+")


def _extract_tailscale_auth_url(text):
    if not text:
        return None
    m = _TAILSCALE_LOGIN_URL_RE.search(text)
    return m.group(0) if m else None


def _tailscale_auth_url_from_daemon():
    """Ask tailscaled whether it's waiting for a browser login.

    Returns the auth URL when BackendState is NeedsLogin and AuthURL is
    populated, otherwise None. Silently returns None if the tailscale CLI
    isn't installed or the call fails — caller will fall back to whatever
    error it already has.
    """
    try:
        r = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    state = (data.get("BackendState") or "").strip()
    url = (data.get("AuthURL") or "").strip()
    if state == "NeedsLogin" and url:
        return url
    return None


def _tailscale_auth_hint(error_text=None):
    """Return a tailscale re-auth URL when an SSH/rsync failure looks like
    a logged-out tailnet, otherwise None.

    First scans the error text — `tailscale ssh` embeds the URL directly —
    then asks the daemon. The daemon path covers plain `ssh` over a tailnet
    IP, which fails with a generic timeout when the node is logged out.
    """
    return _extract_tailscale_auth_url(error_text) or _tailscale_auth_url_from_daemon()


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---------- media config + copy worker ----------

def _read_media_config():
    if not os.path.exists(MEDIA_CONFIG_FILE):
        return {}
    try:
        with open(MEDIA_CONFIG_FILE) as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_media_config(cfg):
    with open(MEDIA_CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


def _load_copy_state_from_disk():
    if not os.path.exists(COPY_STATE_FILE):
        return {}
    try:
        with open(COPY_STATE_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _ensure_copy_state_cache_unlocked():
    global _copy_state_cache
    if _copy_state_cache is None:
        _copy_state_cache = _load_copy_state_from_disk()
    return _copy_state_cache


def _flush_copy_state_unlocked(force=False):
    global _copy_state_last_flush
    now = time.monotonic()
    if not force and (now - _copy_state_last_flush) < _COPY_STATE_FLUSH_MIN_INTERVAL:
        return
    _atomic_write_json(COPY_STATE_FILE, _copy_state_cache)
    _copy_state_last_flush = now


def load_copy_state():
    with _copy_state_lock:
        cache = _ensure_copy_state_cache_unlocked()
        return {k: dict(v) for k, v in cache.items()}


def update_copy_entry(tid, _persist=True, **fields):
    """Update an entry in the copy-state cache.

    _persist=False skips the disk flush — used by the rsync progress loop so
    we don't rewrite copy_state.json once per progress tick. Terminal states
    (done/error/cancelled) always persist.
    """
    with _copy_state_lock:
        state = _ensure_copy_state_cache_unlocked()
        key = str(tid)
        entry = state.get(key) or {"id": tid, "status": "idle"}
        entry.update(fields)
        entry["id"] = tid
        state[key] = entry
        _flush_copy_state_unlocked(force=_persist)
        return dict(entry)


def reconcile_interrupted_copies():
    """Mark orphaned 'copying' entries as interrupted at startup.

    A copy runs in a thread inside the web worker and its rsync is a child of
    that process, so both die with the process. Terminal states are only ever
    written by that thread — if the service is restarted mid-copy (deploy via
    update.sh, unattended-upgrades, reboot), the last progress tick stays on
    disk as 'copying' forever and the UI hides the Copy button behind a
    transfer that no longer exists.

    Any 'copying' entry found at import time is therefore orphaned by
    definition: no copy can outlive the process that started it. Rewriting it
    to 'interrupted' unwedges the UI and tells the truth. Nothing is deleted
    remotely — rsync is incremental, so retrying resumes from where it stopped.

    Returns the list of torrent ids that were reconciled.
    """
    reconciled = []
    with _copy_state_lock:
        state = _ensure_copy_state_cache_unlocked()
        for key, entry in state.items():
            if (entry or {}).get("status") != "copying":
                continue
            pct = entry.get("progress_pct")
            entry["status"] = "interrupted"
            entry["finished_at"] = _now_iso()
            entry["rate"] = None
            entry["eta_seconds"] = None
            entry["error_message"] = (
                "Copy was interrupted before it finished — the dashboard "
                "service stopped while it was running"
                + (f" (reached {pct}%)" if isinstance(pct, int) else "")
                + ". Already-transferred files are intact; starting the copy "
                "again resumes from where it left off."
            )
            reconciled.append(key)
        if reconciled:
            _flush_copy_state_unlocked(force=True)
    return reconciled


def _log_interrupted_copies(keys):
    """Write one event per reconciled copy. Runs off the import path because
    _torrent_name does a Transmission RPC (up to ~13s if the daemon is still
    starting), and boot must not block on the event log."""
    for key in keys:
        try:
            tid = int(key)
        except (TypeError, ValueError):
            continue
        try:
            db.log_event(
                "copy.interrupted",
                "warn",
                "Copy was interrupted by a dashboard restart; retry to resume",
                torrent_id=tid,
                torrent_name=_torrent_name(tid),
            )
        except Exception:
            # Logging is best-effort; the state rewrite is what unwedges the UI.
            pass


def _run_startup_copy_reconcile():
    """Startup hook for reconcile_interrupted_copies(). Never aborts boot —
    a wedged state file must not stop the app that lets you fix it.

    The state rewrite is synchronous so the UI is never served a stale
    'copying'; only the event logging is deferred to a thread.
    """
    try:
        reconciled = reconcile_interrupted_copies()
    except Exception as e:
        print(f"[startup] copy-state reconcile failed: {e}", file=sys.stderr)
        return
    if reconciled:
        threading.Thread(
            target=_log_interrupted_copies, args=(reconciled,), daemon=True,
        ).start()


_run_startup_copy_reconcile()


def _sanitize_subfolder(s):
    s = (s or "").strip().strip("/")
    if not s:
        return ""
    parts = s.split("/")
    for p in parts:
        # Reject empty, "." / ".." (path traversal) and any segment that
        # starts with '-' (could be misread as an rsync flag).
        if p in ("", ".", ".."):
            raise ValueError("subfolder must not contain '.' or '..' segments")
        if p.startswith("-"):
            raise ValueError("subfolder segments cannot start with '-'")
    return "/".join(parts)


def _sanitize_rename(s):
    s = (s or "").strip()
    if not s:
        return ""
    if "/" in s:
        raise ValueError("rename must be a single name, not a path")
    if s in (".", ".."):
        raise ValueError("rename cannot be '.' or '..'")
    if s.startswith("-"):
        raise ValueError("rename cannot start with '-'")
    return s


def _sanitize_show_name(s):
    s = (s or "").strip()
    if not s:
        raise ValueError("show name is required")
    s = s.replace('"', "").replace("'", "").replace("`", "").strip()
    if not s:
        raise ValueError("show name cannot be only quote characters")
    if "/" in s or "\\" in s:
        raise ValueError("show name cannot contain slashes")
    if s in (".", ".."):
        raise ValueError("show name cannot be '.' or '..'")
    if s.startswith("-"):
        raise ValueError("show name cannot start with '-'")
    return s


def _sanitize_year(y):
    if y is None or y == "":
        raise ValueError("year is required")
    try:
        n = int(y)
    except (TypeError, ValueError):
        raise ValueError("year must be a number")
    if n < 1800 or n > 2200:
        raise ValueError("year is out of range")
    return n


def _sanitize_season(n):
    if n is None or n == "":
        raise ValueError("season number is required")
    try:
        s = int(n)
    except (TypeError, ValueError):
        raise ValueError("season must be a number")
    if s < 0 or s > 99:
        raise ValueError("season must be 0-99")
    return f"{s:02d}"


VIDEO_EXTS = (".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".flv", ".ts", ".webm")
SKIP_DIRS = {"sample", "samples", "extras", "featurettes", "proof", "screens"}


def _estimate_source_size(media_type, source, video_files=None):
    """Best-effort total bytes for the source side of a copy."""
    if media_type == "show":
        total = 0
        for p in video_files or []:
            try:
                total += os.path.getsize(p)
            except OSError:
                pass
        return total
    try:
        r = subprocess.run(
            ["du", "-sb", source],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode == 0:
            return int(r.stdout.split()[0])
    except Exception:
        pass
    return 0


def _remote_df(user, host, port, path):
    """SSH to host and return (total, used, available, mountpoint) for the
    filesystem holding `path` — sizes in bytes, mountpoint as df reports it.

    Raises RuntimeError if the SSH call fails or df output can't be parsed.
    """
    quoted = shlex.quote(path)
    try:
        r = subprocess.run(
            ["ssh",
             "-o", "BatchMode=yes",
             "-o", "ConnectTimeout=10",
             "-o", "StrictHostKeyChecking=accept-new",
             "-p", str(port), f"{user}@{host}",
             f"df -PB1 {quoted}"],
            capture_output=True, text=True, timeout=20,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("ssh timed out checking remote disk space")
    except FileNotFoundError:
        raise RuntimeError("ssh client is not installed on this host")
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip()[:200] or f"ssh exit {r.returncode}"
        raise RuntimeError(f"df failed: {msg}")
    lines = [l for l in r.stdout.strip().splitlines() if l.strip()]
    if len(lines) < 2:
        raise RuntimeError("df output was empty")
    # POSIX df -P: Filesystem 1B-blocks Used Available Capacity Mounted-on
    parts = lines[-1].split()
    if len(parts) < 6:
        raise RuntimeError("df output was malformed")
    try:
        # Mountpoints may contain spaces — everything past Capacity is one.
        return int(parts[1]), int(parts[2]), int(parts[3]), " ".join(parts[5:])
    except ValueError:
        raise RuntimeError("df columns were not integers")


# A copy must leave at least this percent of the destination disk free —
# both as headroom for size-estimation error and so the SSDs never sit at
# 100% full. Configurable in Settings (media config `space_margin_percent`,
# 0-50); this is the default when unset.
DEFAULT_SPACE_MARGIN_PERCENT = 8


def _space_margin_fraction(cfg, mountpoint=None):
    """The configured free-space margin as a fraction of disk size.

    A per-drive entry in the config's `drive_margins` dict (keyed by the
    drive's mountpoint on the media server — the per-drive fields in
    Settings) wins over the config-wide value. Missing/malformed/
    out-of-range values fall through to the next level and ultimately the
    default, so a hand-edited config file can't silently disable the margin.
    """
    drive_margins = (cfg or {}).get("drive_margins")
    if not isinstance(drive_margins, dict):
        drive_margins = {}
    candidates = []
    if mountpoint:
        candidates.append(drive_margins.get(mountpoint))
    candidates.append((cfg or {}).get("space_margin_percent"))
    for raw in candidates:
        if raw is None:
            continue
        try:
            pct = int(raw)
        except (TypeError, ValueError):
            continue
        if 0 <= pct <= 50:
            return pct / 100.0
    return DEFAULT_SPACE_MARGIN_PERCENT / 100.0


def _space_shortfall(need, total, available, margin_fraction):
    """Bytes missing for `need` to fit while leaving the safety margin free.

    Returns 0 when the copy fits, i.e. available - need >= margin_fraction
    of the disk.
    """
    required = need + int(total * margin_fraction)
    return max(0, required - available)


def _remote_df_multi(user, host, port, paths):
    """SSH once and run df on multiple paths. Returns a list of dicts:
    [{device, mountpoint, total, used, available}, ...].

    Paths that share a filesystem may collapse into a single row (df's own
    behaviour) — callers that want to dedupe should key on `device`. Raises
    RuntimeError on ssh/parse failure.
    """
    if not paths:
        return []
    quoted = " ".join(shlex.quote(p) for p in paths)
    try:
        r = subprocess.run(
            ["ssh",
             "-o", "BatchMode=yes",
             "-o", "ConnectTimeout=10",
             "-o", "StrictHostKeyChecking=accept-new",
             "-p", str(port), f"{user}@{host}",
             f"df -PB1 {quoted}"],
            capture_output=True, text=True, timeout=20,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("ssh timed out checking remote disk space")
    except FileNotFoundError:
        raise RuntimeError("ssh client is not installed on this host")
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip()[:200] or f"ssh exit {r.returncode}"
        raise RuntimeError(f"df failed: {msg}")
    lines = [l for l in r.stdout.strip().splitlines() if l.strip()]
    out = []
    for line in lines:
        parts = line.split()
        # Skip the header row and any malformed lines.
        if len(parts) < 5 or parts[0] == "Filesystem":
            continue
        try:
            out.append({
                "device": parts[0],
                "total": int(parts[1]),
                "used": int(parts[2]),
                "available": int(parts[3]),
                # Everything past Capacity is the mountpoint (may have spaces).
                "mountpoint": " ".join(parts[5:]),
            })
        except ValueError:
            continue
    if not out:
        raise RuntimeError("df output was empty")
    return out


def _remote_verify_paths(user, host, port, paths, timeout=30):
    """Confirm each remote path exists after a copy; return summed apparent
    bytes (as `du -b` reports them).

    rsync exiting 0 only proves the bytes left this host successfully — it
    can't tell us the destination was the drive the media server actually
    reads from (e.g. an unmounted mountpoint). A single `du` round-trip over
    the expected destination paths catches a "success" that left nothing
    behind before we mark the copy done or delete the local data.

    Returns None when verification couldn't run (e.g. `du` isn't installed on
    the remote) so the caller can skip rather than fail a good copy. Raises
    RuntimeError when the remote is reachable but a path is missing, or ssh
    itself fails — cases where we'd rather not trust the copy.
    """
    if not paths:
        return 0
    quoted = " ".join(shlex.quote(p) for p in paths)
    # `du -scb`: -s summarize each arg, -c print a grand-total line, -b use
    # apparent size in bytes. du exits non-zero (and names the path on stderr)
    # for any operand that doesn't exist — that's our existence check.
    try:
        r = subprocess.run(
            ["ssh",
             "-o", "BatchMode=yes",
             "-o", "ConnectTimeout=10",
             "-o", "StrictHostKeyChecking=accept-new",
             "-p", str(port), f"{user}@{host}",
             f"du -scb -- {quoted}"],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("ssh timed out verifying remote copy")
    except FileNotFoundError:
        raise RuntimeError("ssh client is not installed on this host")
    stderr_txt = (r.stderr or "").strip()
    # du missing on the remote is an environment issue, not a bad copy —
    # skip verification rather than fail every copy to this host.
    if r.returncode == 127 or "command not found" in stderr_txt.lower():
        return None
    if r.returncode != 0:
        raise RuntimeError(stderr_txt[:200] or f"du exit {r.returncode}")
    total = 0
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[-1].strip() == "total":
            try:
                total = int(parts[0])
            except ValueError:
                pass
    return total


def _glob_escape(s):
    """Escape glob metacharacters so a literal name is matched verbatim by
    find's -iname pattern (which is a shell glob, not a regex)."""
    return re.sub(r"([\\*?\[])", r"\\\1", s)


def _remote_check_presence(user, host, port, exact_paths, roots, names,
                           depth=2, timeout=30):
    """Is a torrent's copied content present on the media server?

    Two strategies in one SSH round-trip:
      1. exact_paths — the real per-item destination paths recorded at copy
         time (new copies). Present if all of them exist.
      2. roots + names — the configured library folders and the item's
         candidate on-disk names. Present if any folder holds an entry
         (within `depth` levels) whose name matches, case-insensitively. This
         is what lets the check survive a server switch: it searches the
         folders from Settings by name, not whatever absolute path was saved
         when the copy first ran.

    Only portable find primaries (-iname/-maxdepth) are used — no GNU-only
    -printf — so it works against Linux, BSD/macOS, and busybox/NAS servers.

    Returns (present, debug):
      present = True / False / None (None = couldn't run: nothing to look for,
                host unreachable, or the remote produced no verdict).
      debug   = short string for logging (stderr snippet or note).
    The caller treats None as "unknown" so a transient failure or a stale
    record never reads as a confirmed loss.
    """
    exact_paths = [p for p in (exact_paths or []) if p]
    roots = [r for r in (roots or []) if r]
    names = [n for n in (names or []) if n]
    if not exact_paths and not (roots and names):
        return None, "nothing to check"

    lines = []
    if exact_paths:
        quoted = " ".join(shlex.quote(p) for p in exact_paths)
        lines.append("__ok=1")
        lines.append(f'for __p in {quoted}; do [ -e "$__p" ] || __ok=0; done')
        lines.append('if [ "$__ok" = 1 ]; then echo __PRESENT__; exit 0; fi')
    if roots and names:
        rq = " ".join(shlex.quote(r) for r in roots)
        iname_expr = " -o ".join(
            "-iname " + shlex.quote(_glob_escape(n)) for n in names)
        # find -iname is portable (GNU/BSD/busybox); head closes the pipe on
        # the first hit so a match returns fast. Only find's stdout (matched
        # paths) counts as present; its stderr flows through to ssh stderr so
        # a broken find or a missing root surfaces for diagnosis without ever
        # being mistaken for a match.
        lines.append(
            f'if [ -n "$(find {rq} -maxdepth {int(depth)} '
            f'\\( {iname_expr} \\) | head -n 1)" ]; '
            f'then echo __PRESENT__; exit 0; fi'
        )
    lines.append("echo __MISSING__")
    remote_cmd = "\n".join(lines)

    try:
        r = subprocess.run(
            ["ssh",
             "-o", "BatchMode=yes",
             "-o", "ConnectTimeout=10",
             "-o", "StrictHostKeyChecking=accept-new",
             "-p", str(port), f"{user}@{host}", remote_cmd],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return None, f"ssh failed: {e}"
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if out.endswith("__PRESENT__"):
        return True, err
    if out.endswith("__MISSING__"):
        return False, err
    return None, (err or out or "no verdict from remote")[:300]


def _trigger_library_refresh(cfg, folder, tid=None, torrent_name=None):
    """Fire-and-forget Plex/Jellyfin library refresh after a successful copy.

    Failures are logged as events but never raised — the copy itself
    already succeeded.
    """
    lib = (cfg or {}).get("library_refresh") or {}
    lib_type = (lib.get("type") or "").lower()
    if lib_type in ("", "none"):
        return
    url = (lib.get("url") or "").rstrip("/")
    token = lib.get("token") or ""
    if not url or not token:
        db.log_event(
            "library.refresh.skipped",
            "warn",
            "Library refresh skipped: url or token not configured",
            torrent_id=tid, torrent_name=torrent_name,
        )
        return
    try:
        if lib_type == "plex":
            section_id = (folder or {}).get("plex_section_id") or "all"
            target = f"{url}/library/sections/{section_id}/refresh"
            r = requests.get(
                target,
                params={"X-Plex-Token": token},
                timeout=15,
            )
        elif lib_type == "jellyfin":
            target = f"{url}/Library/Refresh"
            r = requests.post(
                target,
                headers={"X-Emby-Token": token},
                timeout=15,
            )
        else:
            db.log_event(
                "library.refresh.skipped",
                "warn",
                f"Unknown library_refresh.type: {lib_type}",
                torrent_id=tid, torrent_name=torrent_name,
            )
            return
        if 200 <= r.status_code < 300:
            db.log_event(
                "library.refresh.success",
                "info",
                f"{lib_type.capitalize()} library refresh triggered",
                torrent_id=tid, torrent_name=torrent_name,
                details={"type": lib_type, "status_code": r.status_code},
            )
        else:
            db.log_event(
                "library.refresh.failure",
                "warn",
                f"{lib_type.capitalize()} refresh returned HTTP {r.status_code}",
                torrent_id=tid, torrent_name=torrent_name,
                details={"type": lib_type, "status_code": r.status_code,
                         "body": (r.text or "")[:200]},
            )
    except Exception as e:
        db.log_event(
            "library.refresh.failure",
            "warn",
            f"{lib_type.capitalize()} refresh failed: {e}",
            torrent_id=tid, torrent_name=torrent_name,
        )


def _collect_video_files(root):
    """Recursively collect video files under `root`, skipping sample/extras dirs."""
    out = []
    if os.path.isfile(root):
        if root.lower().endswith(VIDEO_EXTS):
            out.append(root)
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS]
        for fn in filenames:
            low = fn.lower()
            if not low.endswith(VIDEO_EXTS):
                continue
            if low.startswith("sample"):
                continue
            out.append(os.path.join(dirpath, fn))
    out.sort()
    return out


# Season patterns tried in order of confidence. Same set the JS parseTorrentMeta
# uses, kept in sync deliberately so detection matches what the modal previews.
_SEASON_PATTERNS = (
    re.compile(r"\bS(\d{1,2})\s*[._-]?\s*E\d{1,3}\b", re.I),
    re.compile(r"\b(\d{1,2})x\d{1,3}\b"),
    re.compile(r"\bSeason[\s._-]*(\d{1,2})\b", re.I),
    re.compile(r"\bS(\d{1,2})\b(?![A-Za-z])", re.I),
)


def _detect_season_for_path(path, root):
    """Return a zero-padded 2-digit season string for `path`, or None.

    Looks at the path relative to `root` so a `Season 02/` parent folder
    rescues files whose own name lacks an SxxExx marker.
    """
    try:
        rel = os.path.relpath(path, root) if root else os.path.basename(path)
    except ValueError:
        rel = os.path.basename(path)
    for pat in _SEASON_PATTERNS:
        m = pat.search(rel)
        if not m:
            continue
        try:
            n = int(m.group(1))
        except ValueError:
            continue
        if 0 <= n <= 99:
            return f"{n:02d}"
    return None


def _group_videos_by_season(videos, root):
    """Group video paths by detected season number.

    Returns (groups, unclassified) where groups is an OrderedDict-like dict
    keyed by zero-padded season ("01", "02", …) with sorted file lists, and
    unclassified is a list of paths where no season could be detected.
    """
    groups = {}
    unclassified = []
    for v in videos:
        s = _detect_season_for_path(v, root)
        if s is None:
            unclassified.append(v)
        else:
            groups.setdefault(s, []).append(v)
    for k in groups:
        groups[k].sort()
    return groups, unclassified


# ----- media bitrate (file size / movie runtime) -----

# The "Downloading / Paused" column shows each torrent's finished media
# bitrate. Runtime comes from ffprobe reading whatever is already on disk;
# size is the *declared* final length from Transmission rather than the
# bytes landed so far, so the figure is the finished file's bitrate from the
# first probe instead of one that creeps upwards as pieces arrive.

# Containers that carry duration in a header near the start of the file, so
# ffprobe reports the whole runtime off a partial download. Everything else
# (AVI, MPEG-TS, …) makes ffprobe estimate from the bytes it can see, which
# on an incomplete file yields a truncated runtime and a wildly inflated
# bitrate — those we only probe once the file is complete.
_HEADER_DURATION_EXTS = (".mkv", ".webm", ".mp4", ".m4v", ".mov")

# A probe that came back empty is usually "the header hasn't downloaded
# yet", which fixes itself — retry, but not on every poll.
_BITRATE_RETRY_S = 180
_BITRATE_CACHE_MAX = 500

_bitrate_lock = threading.Lock()
_bitrate_cache = {}       # hashString -> {bps, bytes, duration, ts}
_bitrate_pending = set()  # hashStrings currently queued or in flight
_bitrate_jobs = queue.Queue()
_bitrate_worker = None


def _probe_media_duration(path):
    """Runtime of `path` in seconds via ffprobe, or None if unreadable.

    Returns None when ffprobe isn't installed — the UI just omits the
    bitrate rather than showing an error, since this is a nicety.
    """
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1",
             "--", path],
            capture_output=True, text=True, timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    try:
        d = float((r.stdout or "").strip())
    except ValueError:
        return None
    # Guard against ffprobe reporting the runtime of a readable prefix.
    # Nothing worth a bitrate readout is under half a minute.
    return d if d >= 30 else None


def _bitrate_probe_target(tid, expect_hash, incomplete_dir):
    """Pick the file to probe: (path on disk, declared bytes, is_complete).

    The largest video file stands in for the torrent — for a movie that's
    the movie, and for a season pack it's a representative episode, which is
    the number you'd compare against a quality target either way.
    """
    rows = client.get_torrent_files([tid])
    if not rows:
        return None
    row = rows[0]
    # Ids are reassigned when the daemon restarts; if this id now holds a
    # different torrent, whatever we probe belongs to someone else.
    if expect_hash and row.get("hashString") != expect_hash:
        return None
    download_dir = row.get("downloadDir") or ""

    best = None
    for f in row.get("files") or []:
        rel = f.get("name") or ""
        low = rel.lower()
        if not low.endswith(VIDEO_EXTS):
            continue
        if os.path.basename(low).startswith("sample"):
            continue
        if set(os.path.dirname(low).split("/")) & SKIP_DIRS:
            continue
        length = int(f.get("length") or 0)
        if length <= 0:
            continue
        if best is None or length > best[0]:
            best = (length, rel, int(f.get("bytesCompleted") or 0))
    if best is None:
        return None
    length, rel, completed = best

    # Transmission parks in-progress files under incomplete-dir when it's
    # enabled, and suffixes them with .part while pieces are still missing.
    roots = [download_dir] + ([incomplete_dir] if incomplete_dir else [])
    for root in roots:
        if not root:
            continue
        base = os.path.join(root, rel)
        for candidate in (base, base + ".part"):
            if os.path.isfile(candidate):
                return candidate, length, completed >= length
    return None


def _compute_bitrate(tid, expect_hash, incomplete_dir):
    """Return bits-per-second for the torrent's main video, or None."""
    target = _bitrate_probe_target(tid, expect_hash, incomplete_dir)
    if not target:
        return None
    path, length, is_complete = target
    stem = path[:-5] if path.endswith(".part") else path
    if not is_complete and not stem.lower().endswith(_HEADER_DURATION_EXTS):
        return None
    duration = _probe_media_duration(path)
    if not duration:
        return None
    return length * 8.0 / duration, length, duration


def _bitrate_worker_loop():
    while True:
        tid, thash = _bitrate_jobs.get()
        entry = {"bps": None, "bytes": None, "duration": None, "ts": time.time()}
        try:
            incomplete_dir = client.get_incomplete_dir()
        except Exception:
            incomplete_dir = None
        try:
            got = _compute_bitrate(tid, thash, incomplete_dir)
            if got:
                bps, nbytes, duration = got
                entry = {"bps": bps, "bytes": nbytes,
                         "duration": duration, "ts": time.time()}
        except Exception:
            pass
        with _bitrate_lock:
            _bitrate_cache[thash] = entry
            _bitrate_pending.discard(thash)
            if len(_bitrate_cache) > _BITRATE_CACHE_MAX:
                for stale in sorted(_bitrate_cache,
                                    key=lambda k: _bitrate_cache[k]["ts"]
                                    )[:len(_bitrate_cache) - _BITRATE_CACHE_MAX]:
                    _bitrate_cache.pop(stale, None)


def _ensure_bitrate_worker():
    """Start the probe thread on first use.

    Single worker on purpose: ffprobe on a Pi reading a file that's actively
    being written is not something to run a dozen of at once, and the result
    is cached forever once it lands.
    """
    global _bitrate_worker
    with _bitrate_lock:
        if _bitrate_worker is None or not _bitrate_worker.is_alive():
            _bitrate_worker = threading.Thread(
                target=_bitrate_worker_loop, daemon=True,
                name="bitrate-probe",
            )
            _bitrate_worker.start()


def _tag_copied(tid):
    # Best-effort: a copy already succeeded; failing to tag shouldn't
    # surface as a copy error. Merge with existing labels to avoid
    # clobbering whatever the user had set. Failures are logged as a
    # warning event so concurrent races don't disappear silently — the
    # UI's "sent" indicator falls back to copy_state when this fails.
    try:
        detail = client.get_torrent_detail(tid)
        existing = list(detail.get("labels") or []) if detail else []
        if COPIED_LABEL in existing:
            return
        client.set_labels(tid, existing + [COPIED_LABEL])
    except Exception as e:
        try:
            db.log_event(
                "copy.tag_failed",
                "warn",
                f"Failed to apply '{COPIED_LABEL}' label after copy: {e}",
                torrent_id=tid,
                torrent_name=_torrent_name(tid),
            )
        except Exception:
            pass


def _read_rsync_progress(stream):
    """Yield lines from rsync stdout, splitting on both \\r and \\n.

    rsync's --info=progress2 overwrites a single line with \\r updates; the
    default line iterator only splits on \\n, so we'd see one giant line at
    EOF instead of live updates.
    """
    buf = bytearray()
    while True:
        b = stream.read(1)
        if not b:
            if buf:
                yield buf.decode("utf-8", errors="replace")
            return
        if b in (b"\r", b"\n"):
            if buf:
                yield buf.decode("utf-8", errors="replace")
                buf.clear()
        else:
            buf += b


def _finalize_copy(tid, folder, torrent_name, media_type):
    """Terminal cleanup for a copy run — remove from active-copies table,
    persist to the history DB, and log the copy.completed event.

    Extracted so the multi-disk fallback path can finalize its own early
    failure the same way _run_copy's finally block does.
    """
    with _active_copies_lock:
        _active_copies.pop(tid, None)
    try:
        entry = load_copy_state().get(str(tid)) or {}
        final_status = entry.get("status") or "error"
        name = torrent_name or _torrent_name(tid)
        db.record_copy(
            tid,
            name,
            started_at=entry.get("started_at"),
            finished_at=entry.get("finished_at"),
            status=final_status,
            dest_host=entry.get("dest_host"),
            dest_path=entry.get("dest_path"),
            folder_name=(folder or {}).get("name"),
            media_type=media_type,
            total_bytes=entry.get("total_bytes"),
            bytes_transferred=entry.get("bytes_transferred"),
            error_message=entry.get("error_message"),
        )
        if final_status == "done":
            msg = f"Copied to {entry.get('dest_host')}:{entry.get('dest_path')}"
        elif final_status == "cancelled":
            msg = "Copy cancelled by user"
        else:
            msg = f"Copy failed: {entry.get('error_message') or 'unknown error'}"
        db.log_event(
            "copy.completed",
            _copy_severity(final_status),
            msg,
            torrent_id=tid,
            torrent_name=name,
            details={
                "status": final_status,
                "dest_host": entry.get("dest_host"),
                "dest_path": entry.get("dest_path"),
                "folder": (folder or {}).get("name"),
                "media_type": media_type,
                "bytes_transferred": entry.get("bytes_transferred"),
                "total_bytes": entry.get("total_bytes"),
            },
        )
    except Exception:
        pass


def _run_copy(tid, sources, dest_root, subfolder, rename,
              host, user, port, delete_after, media_type="movie",
              folder=None, cfg=None, torrent_name=None,
              season_groups=None, candidates=None):
    # api_copy_start reserves the _active_copies slot before spawning this
    # thread (closing the double-click race); reuse that entry so its cancel
    # Event stays valid. Fall back to self-registration for direct callers.
    with _active_copies_lock:
        info = _active_copies.get(tid)
        if info is None:
            info = {"cancel": threading.Event(), "proc": None}
            _active_copies[tid] = info
    cancel_event = info["cancel"]

    def register_proc(p):
        with _active_copies_lock:
            info = _active_copies.get(tid)
            if info is not None:
                info["proc"] = p

    def cancelled():
        return cancel_event.is_set()

    # Bind the state entry to this torrent's identity before anything else
    # writes to it. Every later update_copy_entry(tid, ...) merges into this
    # entry, so all outcomes — done, error, cancelled — inherit the hash and
    # survive the id-reassignment sweep in _gc_state_for_live_torrents.
    update_copy_entry(tid, hash=_torrent_hash(tid))

    # Optional rsync bandwidth cap (KB/s) from media config. Unlimited
    # copies compete with Transmission for CPU (ssh encryption) and disk.
    try:
        bwlimit_kbps = int((cfg or {}).get("bwlimit_kbps") or 0)
    except (TypeError, ValueError):
        bwlimit_kbps = 0
    bwlimit_args = [f"--bwlimit={bwlimit_kbps}"] if bwlimit_kbps > 0 else []

    # ---- Multi-disk fallback selection ----
    # `candidates` is the ordered list of same-name folders configured for
    # this destination label. When more than one exists we df each and pick
    # the first with enough free space. Size estimation happens up front
    # (moved from the per-branch code below) so both branches can reuse it.
    preflight_bytes = 0
    try:
        if media_type == "show" and season_groups:
            for grp in season_groups:
                for p in grp["sources"]:
                    try:
                        preflight_bytes += os.path.getsize(p)
                    except OSError:
                        pass
        else:
            preflight_bytes = _estimate_source_size(
                media_type,
                sources[0] if media_type != "show" else None,
                video_files=sources if media_type == "show" else None,
            )
    except Exception:
        preflight_bytes = 0

    if candidates and len(candidates) > 1 and preflight_bytes > 0:
        picked = None
        tried = []
        for cand in candidates:
            try:
                disk_total, _, free, cand_mount = _remote_df(
                    user, host, port, cand["path"])
            except RuntimeError as e:
                tried.append({"path": cand["path"], "free": None, "error": str(e)})
                continue
            cand_margin = _space_margin_fraction(cfg, cand_mount)
            tried.append({"path": cand["path"], "free": free,
                          "margin_percent": round(cand_margin * 100)})
            if _space_shortfall(preflight_bytes, disk_total, free,
                                cand_margin) == 0:
                picked = cand
                break
        if picked is None:
            reachable = [t for t in tried if t.get("free") is not None]
            if reachable:
                summary = "; ".join(
                    f"{t['path']}={t['free']} (margin {t['margin_percent']}%)"
                    for t in reachable
                )
                msg = (
                    f"Not enough space on any '{folder.get('name') if folder else ''}' "
                    f"destination — need {preflight_bytes} bytes plus "
                    f"each drive's margin; tried {summary}"
                )
                db.log_event(
                    "copy.space_insufficient",
                    "error", msg,
                    torrent_id=tid, torrent_name=torrent_name,
                    details={"need": preflight_bytes, "tried": tried,
                             "host": host},
                )
                update_copy_entry(
                    tid, status="error", finished_at=_now_iso(),
                    error_message=msg,
                )
                _finalize_copy(tid, folder, torrent_name, media_type)
                return
            # No candidate was reachable — fall through to the first one so
            # rsync surfaces the real error the way it always has.
            picked = candidates[0]
        if picked is not candidates[0]:
            db.log_event(
                "copy.fallback",
                "info",
                f"Primary {candidates[0]['path']} is full — falling back to {picked['path']}",
                torrent_id=tid, torrent_name=torrent_name,
                details={"need": preflight_bytes, "tried": tried,
                         "chosen": picked["path"]},
            )
        folder = picked
        dest_root = picked["path"]

    # The destination drive's free-space margin (per-drive override, else
    # the config-wide value) is resolved inside each preflight block below,
    # once df has told us which drive dest_root actually lives on.

    # Expected remote destination paths, filled in per branch below, and the
    # source byte total we expect to find there — used by the post-copy
    # existence check (_remote_verify_paths) once rsync reports success.
    verify_targets = []
    verify_expected = preflight_bytes

    if media_type == "show" and season_groups:
        # Multi-season: each detected season copies into its own
        # Season NN/ subfolder under the show root. The single-pass
        # rsync command can't fan files out to different remote dirs,
        # so we run one rsync per season inside the try block below.
        show_root_remote = dest_root.rstrip("/")
        if subfolder:
            show_root_remote = show_root_remote + "/" + subfolder
        season_labels = [g["season"] for g in season_groups]
        final_dest_path = (
            f"{show_root_remote} [Seasons {', '.join(season_labels)}]"
        )
        rsync_sources = None
        rsync_target = None
        remote_mkdir = None
        source = None
    elif media_type == "show":
        # Multi-file flat copy: every video lands directly in dest_root/subfolder/
        parts = [dest_root.rstrip("/")]
        if subfolder:
            parts.append(subfolder)
        final_dest_path = "/".join(parts)
        rsync_sources = list(sources)
        rsync_target = f"{user}@{host}:{final_dest_path}/"
        remote_mkdir = final_dest_path
        source = None
        # Each video lands in final_dest_path/ under its own basename.
        verify_targets = [
            f"{final_dest_path}/{os.path.basename(s.rstrip('/'))}"
            for s in sources
        ]
    else:
        source = sources[0]
        is_dir = os.path.isdir(source)
        if rename and not is_dir:
            src_ext = os.path.splitext(os.path.basename(source))[1]
            if src_ext and os.path.splitext(rename)[1].lower() != src_ext.lower():
                rename = rename + src_ext
        parts = [dest_root.rstrip("/")]
        if subfolder:
            parts.append(subfolder)
        if rename:
            parts.append(rename)
        final_dest_path = "/".join(parts)

        # rsync semantics:
        # - source with trailing slash copies *contents* into the target dir
        # - source without trailing slash copies the source itself into target
        # When the user supplied a rename, we want the renamed dir/file at the
        # exact final path; without rename, rsync preserves the source's basename
        # inside the (sub)folder.
        if rename:
            if is_dir:
                rsync_source = source.rstrip("/") + "/"
                rsync_target = f"{user}@{host}:{final_dest_path}/"
                remote_mkdir = final_dest_path
            else:
                rsync_source = source
                rsync_target = f"{user}@{host}:{final_dest_path}"
                remote_mkdir = "/".join(parts[:-1])
        else:
            rsync_source = source
            rsync_target = f"{user}@{host}:{final_dest_path}/"
            remote_mkdir = final_dest_path
        rsync_sources = [rsync_source]
        # With a rename the source lands exactly at final_dest_path; without
        # one, rsync preserves the source basename inside final_dest_path/.
        if rename:
            verify_targets = [final_dest_path]
        else:
            verify_targets = [
                f"{final_dest_path}/{os.path.basename(source.rstrip('/'))}"
            ]

    update_copy_entry(
        tid,
        status="copying",
        started_at=_now_iso(),
        finished_at=None,
        dest_host=f"{user}@{host}",
        dest_path=final_dest_path,
        progress_pct=0,
        bytes_transferred=0,
        total_bytes=0,
        rate=None,
        eta_seconds=None,
        error_message=None,
        delete_after=bool(delete_after),
        deleted=False,
        tailscale_auth_url=None,
    )

    def verify_remote_copy():
        """Post-copy existence check. Returns an error message when the remote
        copy can't be confirmed, or None when it's confirmed (or skipped)."""
        if not verify_targets:
            return None
        try:
            remote_bytes = _remote_verify_paths(user, host, port, verify_targets)
        except RuntimeError as e:
            return f"post-copy verification failed: {e}"
        if remote_bytes is None:
            db.log_event(
                "copy.verify_skipped", "warn",
                "Post-copy verification skipped ('du' unavailable on remote)",
                torrent_id=tid, torrent_name=torrent_name,
            )
            return None
        # du's apparent size is >= the summed source file sizes (directory
        # entries add a little), so a remote total below the source total
        # means files are missing or truncated. Allow 2% slack for safety.
        if verify_expected > 0 and remote_bytes < int(verify_expected * 0.98):
            return (
                f"post-copy verification failed: remote destination holds "
                f"{remote_bytes} bytes, expected ~{verify_expected}"
            )
        return None

    try:
        if media_type == "show" and season_groups:
            # ---- Multi-season copy path ----
            # Sum sizes across all selected seasons up front so the UI
            # can show a stable total and progress aggregates correctly
            # across each per-season rsync pass.
            total_bytes_est = 0
            group_sizes = []
            for grp in season_groups:
                gs = 0
                for p in grp["sources"]:
                    try:
                        gs += os.path.getsize(p)
                    except OSError:
                        pass
                group_sizes.append(gs)
                total_bytes_est += gs
            if total_bytes_est > 0:
                update_copy_entry(tid, total_bytes=total_bytes_est)

            if cancelled():
                update_copy_entry(tid, status="cancelled", finished_at=_now_iso())
                return

            if total_bytes_est > 0:
                free_bytes = disk_total = None
                margin_fraction = _space_margin_fraction(cfg)
                try:
                    disk_total, _, free_bytes, dest_mount = _remote_df(
                        user, host, port, dest_root,
                    )
                    margin_fraction = _space_margin_fraction(cfg, dest_mount)
                except RuntimeError as e:
                    db.log_event(
                        "copy.preflight.skipped",
                        "warn",
                        f"Free-space check skipped: {e}",
                        torrent_id=tid, torrent_name=torrent_name,
                    )
                if free_bytes is not None and _space_shortfall(
                        total_bytes_est, disk_total, free_bytes,
                        margin_fraction):
                    msg = (
                        f"Not enough space on {host}:{dest_root} — "
                        f"need {total_bytes_est} bytes plus "
                        f"{margin_fraction:.0%} margin, "
                        f"{free_bytes} available."
                    )
                    db.log_event(
                        "copy.space_insufficient",
                        "error",
                        msg,
                        torrent_id=tid, torrent_name=torrent_name,
                        details={"need": total_bytes_est, "free": free_bytes,
                                 "path": dest_root, "host": host},
                    )
                    update_copy_entry(tid, status="error",
                                      finished_at=_now_iso(),
                                      error_message=msg)
                    return

            if cancelled():
                update_copy_entry(tid, status="cancelled", finished_at=_now_iso())
                return

            ssh_opts_ms = _copy_ssh_transport(port)
            STDERR_MAX_LINES = 500
            cumulative_bytes = 0

            for grp, grp_size in zip(season_groups, group_sizes):
                if cancelled():
                    update_copy_entry(tid, status="cancelled", finished_at=_now_iso())
                    return

                season = grp["season"]
                files = list(grp["sources"])
                remote_dir = f"{show_root_remote}/Season {season}"
                verify_targets.extend(
                    f"{remote_dir}/{os.path.basename(f.rstrip('/'))}"
                    for f in files
                )

                try:
                    quoted = shlex.quote(remote_dir)
                    mk = subprocess.Popen(
                        ["ssh",
                         "-o", "BatchMode=yes",
                         "-o", "ConnectTimeout=10",
                         "-o", "StrictHostKeyChecking=accept-new",
                         "-p", str(port), f"{user}@{host}", f"mkdir -p {quoted}"],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    )
                    register_proc(mk)
                    try:
                        _, mk_err = mk.communicate(timeout=30)
                    except subprocess.TimeoutExpired:
                        mk.kill()
                        _, mk_err = mk.communicate()
                    if mk.returncode != 0:
                        msg = (mk_err or b"").decode("utf-8", errors="replace").strip()
                        raise RuntimeError(f"remote mkdir failed: {msg or 'unknown error'}")
                except RuntimeError as e:
                    update_copy_entry(tid, status="error", finished_at=_now_iso(),
                                      error_message=str(e),
                                      tailscale_auth_url=_tailscale_auth_hint(str(e)))
                    return
                except Exception as e:
                    update_copy_entry(tid, status="error", finished_at=_now_iso(),
                                      error_message=f"ssh failed: {e}",
                                      tailscale_auth_url=_tailscale_auth_hint(str(e)))
                    return

                if cancelled():
                    update_copy_entry(tid, status="cancelled", finished_at=_now_iso())
                    return

                cmd_ms = [
                    "rsync", "-a", "-s", "--partial",
                    f"--timeout={_COPY_RSYNC_IO_TIMEOUT}",
                    "--info=progress2", "--no-i-r",
                    *bwlimit_args,
                    "-e", ssh_opts_ms, "--",
                    *files, f"{user}@{host}:{remote_dir}/",
                ]
                try:
                    proc = subprocess.Popen(
                        cmd_ms, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    )
                except FileNotFoundError:
                    update_copy_entry(tid, status="error", finished_at=_now_iso(),
                                      error_message="rsync not installed")
                    return
                except Exception as e:
                    update_copy_entry(tid, status="error", finished_at=_now_iso(),
                                      error_message=str(e))
                    return

                register_proc(proc)

                stderr_chunks = []
                stderr_truncated = [False]

                def drain_err_ms(p=proc, c=stderr_chunks, t=stderr_truncated):
                    try:
                        for line in p.stderr:
                            if len(c) >= STDERR_MAX_LINES:
                                t[0] = True
                                continue
                            c.append(line.decode("utf-8", errors="replace"))
                    except Exception:
                        pass

                err_t = threading.Thread(target=drain_err_ms, daemon=True)
                err_t.start()

                last_write = 0.0
                for line in _read_rsync_progress(proc.stdout):
                    if cancelled():
                        break
                    m = COPY_PROGRESS_RE.match(line)
                    if not m:
                        continue
                    try:
                        bytes_done = int(m.group(1).replace(",", ""))
                    except ValueError:
                        continue
                    rate = m.group(3)
                    eta = (int(m.group(4)) * 3600
                           + int(m.group(5)) * 60
                           + int(m.group(6)))
                    aggregated = cumulative_bytes + bytes_done
                    if total_bytes_est > 0:
                        pct = max(0, min(100,
                                         int(aggregated * 100 / total_bytes_est)))
                    else:
                        pct = int(m.group(2))
                    now = time.monotonic()
                    if pct < 100 and now - last_write < 0.5:
                        continue
                    last_write = now
                    update_copy_entry(
                        tid, _persist=False,
                        progress_pct=pct,
                        bytes_transferred=aggregated,
                        rate=rate, eta_seconds=eta,
                    )

                proc.wait()
                err_t.join(timeout=2)
                rc = proc.returncode

                if cancelled():
                    update_copy_entry(tid, status="cancelled", finished_at=_now_iso())
                    return

                if rc != 0:
                    raw_err = "".join(stderr_chunks)
                    if stderr_truncated[0]:
                        raw_err += (
                            f"\n... (stderr truncated after {STDERR_MAX_LINES} lines)"
                        )
                    err_msg = raw_err.strip() or f"rsync exited with code {rc}"
                    err_msg = f"Season {season}: {err_msg}"
                    update_copy_entry(
                        tid, status="error", finished_at=_now_iso(),
                        error_message=err_msg[:1000],
                        tailscale_auth_url=_tailscale_auth_hint(raw_err),
                    )
                    return

                cumulative_bytes += grp_size

            verify_err = verify_remote_copy()
            if verify_err:
                db.log_event(
                    "copy.verify_failed", "error", verify_err,
                    torrent_id=tid, torrent_name=torrent_name,
                    details={"targets": verify_targets, "host": host},
                )
                update_copy_entry(
                    tid, status="error", finished_at=_now_iso(),
                    error_message=verify_err[:1000],
                )
                return

            update_copy_entry(
                tid, status="done", progress_pct=100, eta_seconds=0,
                bytes_transferred=cumulative_bytes, finished_at=_now_iso(),
                dest_targets=list(verify_targets),
            )
            if delete_after:
                try:
                    client.remove(tid, delete_local_data=True)
                    update_copy_entry(tid, deleted=True)
                except Exception as e:
                    update_copy_entry(
                        tid,
                        error_message=f"copy succeeded but delete failed: {e}",
                    )
                    _tag_copied(tid)
            else:
                _tag_copied(tid)
            _trigger_library_refresh(cfg, folder, tid=tid, torrent_name=torrent_name)
            return

        # Source size + remote free-space check moved off the request thread.
        # The `du -sb` here can take tens of seconds on a multi-GB folder, and
        # the SSH `df` adds another ~10-20s — running them on the gunicorn
        # worker tied up a request slot for the whole pre-flight.
        estimated_bytes = 0
        try:
            estimated_bytes = _estimate_source_size(
                media_type,
                source if media_type != "show" else None,
                video_files=sources if media_type == "show" else None,
            )
        except Exception:
            estimated_bytes = 0
        if estimated_bytes > 0:
            update_copy_entry(tid, total_bytes=estimated_bytes)

        if cancelled():
            update_copy_entry(tid, status="cancelled", finished_at=_now_iso())
            return

        # Preflight free-space check. Failure to reach the remote isn't fatal
        # — let rsync surface the real error — but a confirmed shortfall is.
        if estimated_bytes > 0:
            free_bytes = disk_total = None
            margin_fraction = _space_margin_fraction(cfg)
            try:
                disk_total, _, free_bytes, dest_mount = _remote_df(
                    user, host, port, dest_root,
                )
                margin_fraction = _space_margin_fraction(cfg, dest_mount)
            except RuntimeError as e:
                db.log_event(
                    "copy.preflight.skipped",
                    "warn",
                    f"Free-space check skipped: {e}",
                    torrent_id=tid, torrent_name=torrent_name,
                )
            if free_bytes is not None and _space_shortfall(
                    estimated_bytes, disk_total, free_bytes,
                    margin_fraction):
                msg = (
                    f"Not enough space on {host}:{dest_root} — "
                    f"need {estimated_bytes} bytes plus "
                    f"{margin_fraction:.0%} margin, "
                    f"{free_bytes} available."
                )
                db.log_event(
                    "copy.space_insufficient",
                    "error",
                    msg,
                    torrent_id=tid, torrent_name=torrent_name,
                    details={"need": estimated_bytes, "free": free_bytes,
                             "path": dest_root, "host": host},
                )
                update_copy_entry(
                    tid,
                    status="error",
                    finished_at=_now_iso(),
                    error_message=msg,
                )
                return

        if cancelled():
            update_copy_entry(tid, status="cancelled", finished_at=_now_iso())
            return

        # Pre-create remote directory tree so rsync doesn't fail on a
        # nested subfolder that doesn't exist yet.
        try:
            quoted = shlex.quote(remote_mkdir)
            mk = subprocess.Popen(
                ["ssh",
                 "-o", "BatchMode=yes",
                 "-o", "ConnectTimeout=10",
                 "-o", "StrictHostKeyChecking=accept-new",
                 "-p", str(port), f"{user}@{host}", f"mkdir -p {quoted}"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            register_proc(mk)
            try:
                _, mk_err = mk.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                mk.kill()
                _, mk_err = mk.communicate()
            if mk.returncode != 0:
                msg = (mk_err or b"").decode("utf-8", errors="replace").strip()
                raise RuntimeError(f"remote mkdir failed: {msg or 'unknown error'}")
        except RuntimeError as e:
            update_copy_entry(tid, status="error", finished_at=_now_iso(),
                              error_message=str(e),
                              tailscale_auth_url=_tailscale_auth_hint(str(e)))
            return
        except Exception as e:
            update_copy_entry(tid, status="error", finished_at=_now_iso(),
                              error_message=f"ssh failed: {e}",
                              tailscale_auth_url=_tailscale_auth_hint(str(e)))
            return

        if cancelled():
            update_copy_entry(tid, status="cancelled", finished_at=_now_iso())
            return

        ssh_opts = _copy_ssh_transport(port)
        cmd = [
            "rsync",
            "-a",
            "-s",
            "--partial",
            f"--timeout={_COPY_RSYNC_IO_TIMEOUT}",
            "--info=progress2",
            "--no-i-r",
            *bwlimit_args,
            "-e", ssh_opts,
            "--",
            *rsync_sources,
            rsync_target,
        ]

        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError:
            update_copy_entry(tid, status="error", finished_at=_now_iso(),
                              error_message="rsync not installed")
            return
        except Exception as e:
            update_copy_entry(tid, status="error", finished_at=_now_iso(),
                              error_message=str(e))
            return

        register_proc(proc)

        # Cap stderr accumulation. rsync stderr is normally tiny, but a
        # misconfigured remote with thousands of per-file warnings would
        # otherwise grow this list unboundedly for the lifetime of the copy.
        STDERR_MAX_LINES = 500
        stderr_chunks = []
        stderr_truncated = [False]
        def drain_err():
            try:
                for line in proc.stderr:
                    if len(stderr_chunks) >= STDERR_MAX_LINES:
                        stderr_truncated[0] = True
                        continue
                    stderr_chunks.append(line.decode("utf-8", errors="replace"))
            except Exception:
                pass
        err_t = threading.Thread(target=drain_err, daemon=True)
        err_t.start()

        last_write = 0.0
        for line in _read_rsync_progress(proc.stdout):
            if cancelled():
                break
            m = COPY_PROGRESS_RE.match(line)
            if not m:
                continue
            try:
                bytes_done = int(m.group(1).replace(",", ""))
            except ValueError:
                continue
            pct = int(m.group(2))
            rate = m.group(3)
            eta = int(m.group(4)) * 3600 + int(m.group(5)) * 60 + int(m.group(6))
            now = time.monotonic()
            # Throttle writes; always emit when we hit 100% so the final
            # state isn't stuck at 99%.
            if pct < 100 and now - last_write < 0.5:
                continue
            last_write = now
            update_copy_entry(
                tid,
                _persist=False,
                progress_pct=pct,
                bytes_transferred=bytes_done,
                rate=rate,
                eta_seconds=eta,
            )

        proc.wait()
        err_t.join(timeout=2)
        rc = proc.returncode

        if cancelled():
            update_copy_entry(tid, status="cancelled", finished_at=_now_iso())
            return

        if rc == 0:
            verify_err = verify_remote_copy()
            if verify_err:
                db.log_event(
                    "copy.verify_failed", "error", verify_err,
                    torrent_id=tid, torrent_name=torrent_name,
                    details={"targets": verify_targets, "host": host},
                )
                update_copy_entry(
                    tid, status="error", finished_at=_now_iso(),
                    error_message=verify_err[:1000],
                )
                return
            update_copy_entry(
                tid,
                status="done",
                progress_pct=100,
                eta_seconds=0,
                finished_at=_now_iso(),
                dest_targets=list(verify_targets),
            )
            if delete_after:
                try:
                    client.remove(tid, delete_local_data=True)
                    update_copy_entry(tid, deleted=True)
                except Exception as e:
                    update_copy_entry(
                        tid,
                        error_message=f"copy succeeded but delete failed: {e}",
                    )
                    _tag_copied(tid)
            else:
                _tag_copied(tid)
            _trigger_library_refresh(cfg, folder, tid=tid, torrent_name=torrent_name)
        else:
            raw_err = "".join(stderr_chunks)
            if stderr_truncated[0]:
                raw_err += f"\n... (stderr truncated after {STDERR_MAX_LINES} lines)"
            err_msg = raw_err.strip() or f"rsync exited with code {rc}"
            update_copy_entry(
                tid,
                status="error",
                finished_at=_now_iso(),
                error_message=err_msg[:1000],
                tailscale_auth_url=_tailscale_auth_hint(raw_err),
            )
    except Exception as e:
        update_copy_entry(
            tid,
            status="error",
            finished_at=_now_iso(),
            error_message=f"copy worker crashed: {e}",
            tailscale_auth_url=_tailscale_auth_hint(str(e)),
        )
    finally:
        _finalize_copy(tid, folder, torrent_name, media_type)


# ---------- auth ----------

def _check_credentials(username, password):
    if not DASHBOARD_USER or not DASHBOARD_PASS:
        return False
    user_ok = hmac.compare_digest(
        username.encode("utf-8"), DASHBOARD_USER.encode("utf-8")
    )
    pass_ok = hmac.compare_digest(
        password.encode("utf-8"), DASHBOARD_PASS.encode("utf-8")
    )
    return user_ok and pass_ok


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "unauthorized"}), 401
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if session.get("logged_in"):
            return redirect(url_for("index"))
        return render_template("login.html")

    username = request.form.get("username", "")
    password = request.form.get("password", "")
    if _check_credentials(username, password):
        session.clear()
        session["logged_in"] = True
        session.permanent = True
        return redirect(url_for("index"))
    return (
        render_template(
            "login.html",
            error="Invalid username or password.",
            username=username,
        ),
        401,
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------- main routes ----------

@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/settings")
@login_required
def settings():
    return render_template("settings.html")


@app.route("/settings/vpn-binding")
@login_required
def vpn_binding_guide():
    return render_template("vpn_binding.html")


@app.route("/settings/systemd-service")
@login_required
def systemd_service_guide():
    return render_template("systemd_service.html")


@app.route("/system")
@login_required
def system():
    return render_template("system.html")


@app.route("/history")
@login_required
def history():
    return render_template("history.html")


@app.route("/events")
@login_required
def events():
    return render_template("events.html")


@app.route("/api/history/removed")
@login_required
def api_history_removed():
    try:
        limit = int(request.args.get("limit") or 100)
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(limit, 500))
    return jsonify({"ok": True, "removed": db.list_removed_torrents(limit=limit)})


@app.route("/api/history/removed/<int:rid>/redownload", methods=["POST"])
@login_required
def api_history_removed_redownload(rid):
    entry = db.get_removed_torrent(rid)
    if not entry:
        return _err("removed torrent not found", 404)
    magnet = (entry.get("magnet_link") or "").strip()
    if not magnet:
        return _err("no magnet link saved for this torrent", 400)
    try:
        client.add_magnet(magnet)
    except Exception as e:
        return _err(e)
    # Carry the custom name forward by re-attaching it to the hash. The
    # new torrent will share the same info-hash, so the existing
    # custom_names row already keys onto it; if the user forgot the
    # entry earlier we re-attach using the archived custom_name.
    h = entry.get("hash")
    if h and entry.get("custom_name") and not db.get_custom_name(h):
        try:
            db.set_custom_name(h, entry["custom_name"], default_name=entry.get("name"))
        except Exception:
            pass
    db.log_event(
        "torrent.redownload",
        "info",
        f"Re-downloading {entry.get('name') or 'torrent'}",
        torrent_name=entry.get("name"),
    )
    return jsonify({"ok": True})


@app.route("/api/history/removed/<int:rid>/forget", methods=["POST"])
@login_required
def api_history_removed_forget(rid):
    entry = db.get_removed_torrent(rid)
    if not entry:
        return _err("removed torrent not found", 404)
    db.delete_removed_torrent(rid)
    return jsonify({"ok": True})


@app.route("/api/history/copies")
@login_required
def api_history_copies():
    try:
        limit = int(request.args.get("limit") or 50)
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 500))
    return jsonify({"ok": True, "copies": db.list_copies(limit=limit)})


@app.route("/api/events")
@login_required
def api_events():
    try:
        limit = int(request.args.get("limit") or 200)
    except (TypeError, ValueError):
        limit = 200
    limit = max(1, min(limit, 2000))
    since = (request.args.get("since") or "").strip() or None
    return jsonify({"ok": True, "events": db.list_events(limit=limit, since=since)})


# psutil's cpu_percent needs a baseline call so the first non-blocking
# read returns a real number instead of 0.0. Prime it at import time.
psutil.cpu_percent(interval=None)
_prev_net = psutil.net_io_counters()
_prev_net_ts = time.monotonic()
_net_last_rates = (0.0, 0.0)
_net_lock = threading.Lock()
# Minimum sampling window. The baseline is shared across all callers, so
# two open System tabs used to reset it on every poll — each computed its
# rate over a fraction of a second and the numbers jittered wildly. Below
# this window we serve the last computed rates and leave the baseline alone.
_NET_RATE_MIN_WINDOW = 1.0


def _net_rates():
    global _prev_net, _prev_net_ts, _net_last_rates
    with _net_lock:
        now = time.monotonic()
        cur = psutil.net_io_counters()
        dt = now - _prev_net_ts
        if dt < _NET_RATE_MIN_WINDOW:
            rx_rate, tx_rate = _net_last_rates
        else:
            rx_rate = max(0, cur.bytes_recv - _prev_net.bytes_recv) / dt
            tx_rate = max(0, cur.bytes_sent - _prev_net.bytes_sent) / dt
            _prev_net = cur
            _prev_net_ts = now
            _net_last_rates = (rx_rate, tx_rate)
        return cur, rx_rate, tx_rate


# Sensor chips that report the CPU package/core temperature, most specific
# first. Intel exposes "coretemp", AMD "k10temp", the Raspberry Pi (where
# this runs in production) "cpu_thermal". We fall back to the first sensor
# with any reading if none of the known chips are present.
_CPU_TEMP_CHIPS = ("coretemp", "k10temp", "cpu_thermal", "acpitz", "zenpower")


def _cpu_temp():
    """Best-effort current CPU temperature in °C, plus its high/critical
    thresholds when the sensor advertises them. Returns None when the host
    exposes no thermal sensors (common on VMs and some Pis without the
    kernel module loaded)."""
    sensors = getattr(psutil, "sensors_temperatures", None)
    if sensors is None:
        return None
    try:
        temps = sensors() or {}
    except Exception:
        return None
    if not temps:
        return None

    def _pick(entries):
        # Prefer a "Package"/"Tctl" label (the whole-die reading) over an
        # individual core; otherwise just take the first entry.
        for e in entries:
            label = (e.label or "").lower()
            if "package" in label or "tctl" in label or "composite" in label:
                return e
        return entries[0] if entries else None

    chosen = None
    for chip in _CPU_TEMP_CHIPS:
        if temps.get(chip):
            chosen = _pick(temps[chip])
            break
    if chosen is None:
        # Fall back to any chip that reported a numeric current reading.
        for entries in temps.values():
            picked = _pick(entries)
            if picked is not None and picked.current is not None:
                chosen = picked
                break
    if chosen is None or chosen.current is None:
        return None
    return {
        "current": round(chosen.current, 1),
        "high": chosen.high if chosen.high else None,
        "critical": chosen.critical if chosen.critical else None,
        "label": chosen.label or None,
    }


_disk_target_cache = {"at": 0.0, "path": "/"}
_DISK_TARGET_TTL = 60.0


def _disk_target():
    # Prefer the Transmission download dir so the number on screen is the
    # one that actually matters for "can I still grab more torrents?".
    # Fall back to root if the daemon is unreachable or the path is bogus.
    #
    # Cached so /api/system polling (every 2-10s) doesn't fire a
    # session-get RPC against transmission for every disk read — the
    # download-dir rarely changes.
    now = time.monotonic()
    if now - _disk_target_cache["at"] < _DISK_TARGET_TTL:
        return _disk_target_cache["path"]
    try:
        download_dir = client.get_session().get("download-dir") or ""
    except Exception:
        download_dir = ""
    path = download_dir if (download_dir and os.path.isdir(download_dir)) else "/"
    _disk_target_cache["path"] = path
    _disk_target_cache["at"] = now
    return path


@app.route("/api/storage")
@login_required
def api_storage():
    """Slim disk-only endpoint for the index page's storage bar.

    The torrents page only needs disk free/total/percent. /api/system also
    spends time on psutil cpu/mem/network/load/pids and a session-get RPC
    every 10s — pointless for a one-bar widget. This skips all of that.
    """
    try:
        disk_path = _disk_target()
        du = psutil.disk_usage(disk_path)
        return jsonify({
            "ok": True,
            "disk": {
                "path": disk_path,
                "total": du.total,
                "used": du.used,
                "free": du.free,
                "percent": du.percent,
            },
        })
    except Exception as e:
        return _err(e)


# Cache the remote df result. The SSH round-trip is multi-hundred-ms (and a
# whole new process), so without this each browser tab would spawn an ssh
# every 10 minutes. With the cache, all tabs share one probe per window.
#
# The probe runs in a single-flight background thread, never on the request
# thread. The old synchronous version held the lock through a 20s SSH
# timeout when the media server was offline — every media-storage request
# from every tab then queued behind it, each pinning a gunicorn thread,
# and page loads stalled 10-15s behind the exhausted pool.
_media_storage_cache = {"at": 0.0, "data": None, "error": None}
_media_storage_lock = threading.Lock()
_media_storage_refreshing = False
_MEDIA_STORAGE_TTL = 600.0


def _media_storage_refresh_worker(cfg):
    global _media_storage_refreshing
    try:
        data, error = _media_storage_probe(cfg)
        with _media_storage_lock:
            _media_storage_cache["at"] = time.monotonic()
            _media_storage_cache["data"] = data
            _media_storage_cache["error"] = error
    finally:
        with _media_storage_lock:
            _media_storage_refreshing = False


def _media_storage_probe(cfg):
    """Run df on the media server. Returns (data, error) — never raises.

    Aggregates across every configured folder's filesystem, deduped by
    device, so mounting an additional disk under a new folder makes the
    headline number grow instead of hiding behind whichever mount the
    first folder happens to live on.
    """
    folders = cfg.get("folders") or []
    host = (cfg.get("host") or "").strip()
    user = (cfg.get("user") or "").strip()
    if not host or not user or not folders:
        return None, "not configured"
    paths = []
    for f in folders:
        p = (f.get("path") or "").strip()
        if p:
            paths.append(p)
    if not paths:
        return None, "no folder paths configured"
    port = int(cfg.get("port") or 22)
    try:
        rows = _remote_df_multi(user, host, port, paths)
    except RuntimeError as e:
        return None, str(e)
    total = used = free = 0
    seen_devices = set()
    for row in rows:
        dev = row["device"]
        if dev in seen_devices:
            continue
        seen_devices.add(dev)
        total += row["total"]
        used += row["used"]
        free += row["available"]
    if total == 0:
        return None, "df returned no usable rows"
    percent = used / total * 100.0
    return {
        "host": host,
        # Kept for backwards-compat with UI code that reads `path`. When
        # multiple filesystems back the library, this is the primary one.
        "path": paths[0],
        "total": total,
        "used": used,
        "free": free,
        "percent": percent,
        "filesystem_count": len(seen_devices),
    }, None


@app.route("/api/media-storage")
@login_required
def api_media_storage():
    """Disk usage on the media server (over SSH, cached 10 min).

    Returns {ok: True, configured: False} when no media destination is set
    so the UI can hide the indicator without showing an error.
    """
    cfg = _read_media_config()
    if not cfg.get("host") or not cfg.get("user") or not (cfg.get("folders") or []):
        return jsonify({"ok": True, "configured": False})

    global _media_storage_refreshing
    force = request.args.get("refresh") in ("1", "true", "yes")
    now = time.monotonic()
    kick = False
    with _media_storage_lock:
        data = _media_storage_cache["data"]
        error = _media_storage_cache["error"]
        have_result = data is not None or error is not None
        cache_fresh = (
            not force
            and have_result
            and now - _media_storage_cache["at"] < _MEDIA_STORAGE_TTL
        )
        if not cache_fresh and not _media_storage_refreshing:
            _media_storage_refreshing = True
            kick = True
        refreshing = _media_storage_refreshing
    if kick:
        threading.Thread(
            target=_media_storage_refresh_worker, args=(cfg,), daemon=True,
        ).start()

    # Serve whatever we have; the `refreshing` flag tells the UI a fresh
    # probe is in flight so it can re-poll shortly instead of waiting for
    # its normal 10-minute cadence.
    if data is None:
        return jsonify({
            "ok": False, "configured": True,
            "error": error or "checking media server…",
            "refreshing": refreshing,
        })
    return jsonify({
        "ok": True, "configured": True, "disk": data,
        "refreshing": refreshing,
    })


# Same single-flight background pattern as media-storage: the two Mullvad
# API calls take up to 20s combined when the API is slow/unreachable, and
# with the 30s error TTL nearly every page load used to re-probe on the
# request thread.
_mullvad_cache = {"at": 0.0, "data": None, "error": None}
_mullvad_lock = threading.Lock()
_mullvad_refreshing = False
_MULLVAD_CACHE_TTL = 3600.0
# Errors expire faster than successes so a fixed account number (typo, etc.)
# isn't masked behind an hour of stale error.
_MULLVAD_ERROR_TTL = 30.0


def _mullvad_refresh_worker():
    global _mullvad_refreshing
    try:
        data, error = _fetch_mullvad_account()
        with _mullvad_lock:
            _mullvad_cache["at"] = time.monotonic()
            _mullvad_cache["data"] = data
            _mullvad_cache["error"] = error
    finally:
        with _mullvad_lock:
            _mullvad_refreshing = False


def _fetch_mullvad_account():
    # Two-step flow used by the official Mullvad app: POST the account number
    # to /auth/v1/token to get a bearer token, then GET /accounts/v1/accounts/me
    # to read the account expiry. The older /www/accounts/{n}/ endpoint returns
    # 404 for accounts created after the API split, so it's unreliable.
    account = (config.MULLVAD_ACCOUNT or "").strip()
    try:
        tok_r = requests.post(
            "https://api.mullvad.net/auth/v1/token",
            json={"account_number": account},
            timeout=10,
            headers={"Accept": "application/json"},
        )
    except Exception as e:
        return None, f"auth request failed: {e}"
    if tok_r.status_code != 200:
        body = (tok_r.text or "")[:200]
        return None, f"auth HTTP {tok_r.status_code}: {body}"
    try:
        token = tok_r.json().get("access_token")
    except Exception:
        return None, "auth response was not JSON"
    if not token:
        return None, "auth response missing access_token"

    try:
        acc_r = requests.get(
            "https://api.mullvad.net/accounts/v1/accounts/me",
            timeout=10,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
    except Exception as e:
        return None, f"account fetch failed: {e}"
    if acc_r.status_code != 200:
        body = (acc_r.text or "")[:200]
        return None, f"account HTTP {acc_r.status_code}: {body}"
    try:
        d = acc_r.json()
    except Exception:
        return None, "account response was not JSON"
    expiry_str = d.get("expiry") or d.get("paid_until")
    if not expiry_str:
        return None, "no expiry field in Mullvad response"
    try:
        expiry = datetime.fromisoformat(expiry_str.replace("Z", "+00:00"))
        days = max(0, (expiry - datetime.now(timezone.utc)).days)
    except Exception as e:
        return None, f"could not parse expiry: {e}"
    return {"days_remaining": days, "expiry": expiry_str}, None


@app.route("/api/mullvad")
@login_required
def api_mullvad():
    global _mullvad_refreshing
    if not config.MULLVAD_ACCOUNT:
        return jsonify({"ok": True, "configured": False})
    now = time.monotonic()
    kick = False
    with _mullvad_lock:
        data = _mullvad_cache["data"]
        error = _mullvad_cache["error"]
        age = now - _mullvad_cache["at"]
        have_success = data is not None
        have_error = error is not None
        cache_fresh = (
            (have_success and age < _MULLVAD_CACHE_TTL)
            or (have_error and not have_success and age < _MULLVAD_ERROR_TTL)
        )
        if not cache_fresh and not _mullvad_refreshing:
            _mullvad_refreshing = True
            kick = True
        refreshing = _mullvad_refreshing
    if kick:
        threading.Thread(target=_mullvad_refresh_worker, daemon=True).start()

    if data is None:
        return jsonify({
            "ok": False, "configured": True,
            "error": error or "checking Mullvad…",
            "refreshing": refreshing,
        })
    return jsonify({"ok": True, "configured": True, "refreshing": refreshing, **data})


@app.route("/api/system")
@login_required
def api_system():
    try:
        vm = psutil.virtual_memory()
        sm = psutil.swap_memory()
        disk_path = _disk_target()
        du = psutil.disk_usage(disk_path)
        net, rx_rate, tx_rate = _net_rates()
        try:
            load1, load5, load15 = os.getloadavg()
        except (AttributeError, OSError):
            load1 = load5 = load15 = 0.0
        boot_ts = psutil.boot_time()
        return jsonify({
            "ok": True,
            "cpu": {
                "percent": psutil.cpu_percent(interval=None),
                "count": psutil.cpu_count(logical=True) or 0,
                "count_physical": psutil.cpu_count(logical=False) or 0,
                "load_avg": [load1, load5, load15],
                "temp": _cpu_temp(),
            },
            "memory": {
                "total": vm.total,
                "used": vm.used,
                "available": vm.available,
                "percent": vm.percent,
            },
            "swap": {
                "total": sm.total,
                "used": sm.used,
                "percent": sm.percent,
            },
            "disk": {
                "path": disk_path,
                "total": du.total,
                "used": du.used,
                "free": du.free,
                "percent": du.percent,
            },
            "network": {
                "bytes_sent": net.bytes_sent,
                "bytes_recv": net.bytes_recv,
                "tx_rate": tx_rate,
                "rx_rate": rx_rate,
            },
            "host": {
                "boot_time": datetime.fromtimestamp(boot_ts, tz=timezone.utc).isoformat(),
                "uptime_seconds": int(time.time() - boot_ts),
                "process_count": len(psutil.pids()),
            },
        })
    except Exception as e:
        return _err(e)


# ---------- aggregate stats ----------

# Fixed bucket edges for the System page distribution charts. Ratio bands are
# the ones seeders actually care about (below 1.0 = still owing); size bands
# are roughly log-scaled across the range of a typical library.
_RATIO_BUCKETS = (
    ("< 0.5", 0.0, 0.5), ("0.5–1", 0.5, 1.0), ("1–2", 1.0, 2.0),
    ("2–5", 2.0, 5.0), ("5+", 5.0, float("inf")),
)
_GB = 1024 ** 3
_MB = 1024 ** 2
_SIZE_BUCKETS = (
    ("< 100 MB", 0, 100 * _MB), ("100 MB–1 GB", 100 * _MB, _GB),
    ("1–5 GB", _GB, 5 * _GB), ("5–20 GB", 5 * _GB, 20 * _GB),
    ("20 GB+", 20 * _GB, float("inf")),
)


def _annotate_recent_upload(entries, hours=24):
    """Add `recent` (bytes uploaded in the last `hours`) to ranked entries,
    in place.

    Best-effort: the traffic sampler is optional and only starts accumulating
    once enabled, so a missing table or an empty window leaves `recent` as
    None. That's deliberately distinct from 0 — "not being measured" and
    "measured, uploaded nothing" should not render the same.
    """
    if not entries:
        return
    hashes = [e["hash"] for e in entries if e.get("hash")]
    if not hashes:
        return
    try:
        if not db.has_torrent_traffic():
            return
        since = int(time.time()) - hours * 3600
        sums = db.get_torrent_traffic_sums(since, hashes, field="up_bytes")
    except Exception:
        return
    for e in entries:
        h = e.get("hash")
        if h:
            e["recent"] = sums.get(h, 0)


def _torrent_distribution(torrents):
    """Snapshot distributions of the current torrent set for the System page
    charts: ratio histogram, size histogram, and library bytes by label."""
    ratio_counts = [0] * len(_RATIO_BUCKETS)
    size_counts = [0] * len(_SIZE_BUCKETS)
    by_label = {}
    for t in torrents:
        size = int(t.get("totalSize") or 0)
        for i, (_, lo, hi) in enumerate(_SIZE_BUCKETS):
            if lo <= size < hi:
                size_counts[i] += 1
                break
        r = t.get("uploadRatio")
        if r is not None and r >= 0 and (t.get("uploadedEver") or 0) > 0:
            for i, (_, lo, hi) in enumerate(_RATIO_BUCKETS):
                if lo <= r < hi:
                    ratio_counts[i] += 1
                    break
        labels = t.get("labels") or []
        if not labels:
            labels = ["Unlabeled"]
        for lb in labels:
            agg = by_label.setdefault(lb, {"bytes": 0, "count": 0})
            agg["bytes"] += size
            agg["count"] += 1
    label_list = sorted(
        ({"label": k, "bytes": v["bytes"], "count": v["count"]}
         for k, v in by_label.items()),
        key=lambda d: d["bytes"], reverse=True,
    )[:8]
    return {
        "ratio_buckets": [
            {"label": _RATIO_BUCKETS[i][0], "count": n}
            for i, n in enumerate(ratio_counts)
        ],
        "size_buckets": [
            {"label": _SIZE_BUCKETS[i][0], "count": n}
            for i, n in enumerate(size_counts)
        ],
        "by_label": label_list,
    }


@app.route("/api/stats")
@login_required
def api_stats():
    try:
        sess = client.get_session_stats() or {}
        torrents = client.get_stats_torrents() or []
    except Exception as e:
        return _err(e)

    cumulative = sess.get("cumulative-stats") or {}
    current = sess.get("current-stats") or {}

    total_uploaded = int(cumulative.get("uploadedBytes") or 0)
    total_downloaded = int(cumulative.get("downloadedBytes") or 0)
    ratio = (total_uploaded / total_downloaded) if total_downloaded > 0 else None

    # Aggregate per-torrent fields the session-stats payload doesn't expose:
    # combined library size, combined seed time, live swarm size, and the
    # superlatives ("biggest", "best ratio", "oldest seeding").
    library_bytes = sum(int(t.get("totalSize") or 0) for t in torrents)
    seed_seconds = sum(int(t.get("secondsSeeding") or 0) for t in torrents)
    swarm_peers = sum(int(t.get("peersConnected") or 0) for t in torrents)
    status_counts = {"downloading": 0, "seeding": 0, "paused": 0, "other": 0}
    for t in torrents:
        s = t.get("status")
        if s in (3, 4):
            status_counts["downloading"] += 1
        elif s in (5, 6):
            status_counts["seeding"] += 1
        elif s == 0:
            status_counts["paused"] += 1
        else:
            status_counts["other"] += 1

    def superlative(items, key, min_value=0):
        best = None
        for t in items:
            v = t.get(key) or 0
            if v <= min_value:
                continue
            if best is None or v > (best.get(key) or 0):
                best = t
        if not best:
            return None
        return {"id": best.get("id"), "name": best.get("name"), "value": best.get(key)}

    def top_n(items, key, n=10, min_value=0):
        ranked = sorted(
            (t for t in items if (t.get(key) or 0) > min_value),
            key=lambda t: t.get(key) or 0,
            reverse=True,
        )
        return [
            {"id": t.get("id"), "name": t.get("name"), "value": t.get(key),
             "hash": t.get("hashString")}
            for t in ranked[:n]
        ]

    biggest = superlative(torrents, "totalSize")
    top_uploaders = top_n(torrents, "uploadedEver", n=10)
    # Annotate each with what it actually did in the last 24h. uploadedEver is
    # a lifetime counter, so the ranking alone can't tell a torrent that's
    # still working from one that banked its total months ago.
    _annotate_recent_upload(top_uploaders)
    top_uploader = top_uploaders[0] if top_uploaders else None
    longest_seeder = superlative(torrents, "secondsSeeding")
    # Ratio superlative only makes sense once a torrent has actually uploaded
    # something — a brand-new torrent with ratio 0 shouldn't win the prize.
    best_ratio = None
    for t in torrents:
        r = t.get("uploadRatio")
        if r is None or r <= 0 or (t.get("uploadedEver") or 0) <= 0:
            continue
        if best_ratio is None or r > best_ratio["value"]:
            best_ratio = {"id": t.get("id"), "name": t.get("name"), "value": r}
    oldest = None
    for t in torrents:
        added = t.get("addedDate") or 0
        if added <= 0:
            continue
        if oldest is None or added < oldest["value"]:
            oldest = {"id": t.get("id"), "name": t.get("name"), "value": added}

    distribution = _torrent_distribution(torrents)

    try:
        lifetime = db.get_lifetime_stats()
    except Exception:
        lifetime = {}
    try:
        lifetime["copies_daily"] = db.get_copy_history_daily(30)
    except Exception:
        lifetime["copies_daily"] = []

    return jsonify({
        "ok": True,
        "distribution": distribution,
        "torrents": {
            "count": int(sess.get("torrentCount") or len(torrents)),
            "active": int(sess.get("activeTorrentCount") or 0),
            "paused": int(sess.get("pausedTorrentCount") or 0),
            "status_counts": status_counts,
            "library_bytes": library_bytes,
            "swarm_peers": swarm_peers,
            "download_speed": int(sess.get("downloadSpeed") or 0),
            "upload_speed": int(sess.get("uploadSpeed") or 0),
        },
        "lifetime": {
            "uploaded_bytes": total_uploaded,
            "downloaded_bytes": total_downloaded,
            "ratio": ratio,
            "seconds_active": int(cumulative.get("secondsActive") or 0),
            "session_count": int(cumulative.get("sessionCount") or 0),
            "files_added": int(cumulative.get("filesAdded") or 0),
            "seed_seconds": seed_seconds,
        },
        "session": {
            "uploaded_bytes": int(current.get("uploadedBytes") or 0),
            "downloaded_bytes": int(current.get("downloadedBytes") or 0),
            "seconds_active": int(current.get("secondsActive") or 0),
        },
        "superlatives": {
            "biggest": biggest,
            "top_uploader": top_uploader,
            "top_uploaders": top_uploaders,
            "longest_seeder": longest_seeder,
            "best_ratio": best_ratio,
            "oldest": oldest,
        },
        "history": lifetime,
    })


# Named ranges the System page graphs offer, in seconds.
_METRICS_RANGES = {
    "1h": 3600, "6h": 6 * 3600, "24h": 24 * 3600,
    "7d": 7 * 86400, "30d": 30 * 86400,
}


@app.route("/api/metrics/history")
@login_required
def api_metrics_history():
    rng = request.args.get("range", "1h")
    span = _METRICS_RANGES.get(rng)
    if span is None:
        return jsonify({"ok": False, "error": "invalid range"}), 400
    try:
        buckets = min(500, max(20, int(request.args.get("buckets", 240))))
    except (TypeError, ValueError):
        buckets = 240
    try:
        since = int(time.time()) - span
        series = db.get_metrics_range(since, buckets=buckets)
    except Exception as e:
        return _err(e)
    return jsonify({
        "ok": True,
        "range": rng,
        "sample_interval": config.METRICS_SAMPLE_INTERVAL,
        "series": series,
    })


# Colors are assigned per rank on the client, so cap the combined chart at
# the number of validated categorical slots the stylesheet defines.
_TORRENT_SERIES_MAX = 5
# The seeding list carries no points per row — each row's graph is fetched
# only when expanded — so this only bounds the totals lookup behind it.
_SEEDING_LIST_MAX = 500


@app.route("/api/metrics/torrents")
@login_required
def api_metrics_torrents():
    """Per-torrent transfer totals for a range, plus a time series for the
    top few. `metric` picks which direction ranks and plots."""
    rng = request.args.get("range", "24h")
    span = _METRICS_RANGES.get(rng)
    if span is None:
        return jsonify({"ok": False, "error": "invalid range"}), 400
    metric = request.args.get("metric", "up")
    if metric not in ("up", "down"):
        return jsonify({"ok": False, "error": "invalid metric"}), 400
    field = "up_bytes" if metric == "up" else "down_bytes"
    try:
        limit = min(20, max(1, int(request.args.get("limit", 8))))
    except (TypeError, ValueError):
        limit = 8

    since = int(time.time()) - span
    try:
        totals = db.get_torrent_traffic_totals(since, limit=limit, field=field)
        plotted = totals[:_TORRENT_SERIES_MAX]
        series_map = db.get_torrent_traffic_series(
            since, [r["hash"] for r in plotted], buckets=120, field=field)
        # The plotted step is wider than one storage bucket on long ranges;
        # the chart labels itself with this, so report what's actually drawn.
        _, step_secs = db.traffic_series_grid(since, 120)
        # Lets the empty state say which kind of empty this is.
        has_history = bool(totals) or db.has_torrent_traffic()
    except Exception as e:
        return _err(e)

    # Prefer the user's custom name, same as the torrent list does.
    custom = db.get_custom_names_map()
    for r in totals:
        r["display_name"] = (
            custom.get(r["hash"]) or r.get("name") or r["hash"][:12])
    return jsonify({
        "ok": True,
        "range": rng,
        "metric": metric,
        "bucket_secs": db.TRAFFIC_BUCKET_SECS,
        "step_secs": step_secs,
        "has_history": has_history,
        "sampler_enabled": config.METRICS_SAMPLE_INTERVAL > 0,
        "totals": totals,
        "series": [
            {
                "hash": r["hash"],
                "name": r["display_name"],
                "points": series_map.get(r["hash"], []),
            }
            for r in plotted
        ],
    })


# Transmission status codes for "seeding": 5 is queued to seed, 6 is
# actively seeding. Matches how /api/stats buckets the status mix.
_SEEDING_STATUSES = (5, 6)
# Sanity filter on the URL segment, not a length assertion — real infohashes
# are 40 hex chars, but the DB is the authority on which ones exist and the
# value only ever reaches SQL as a bound parameter. Hex-only is what matters:
# it keeps path-ish junk out of the route.
_HASH_RE = re.compile(r"^[0-9a-fA-F]{1,64}$")


@app.route("/api/metrics/torrents/seeding")
@login_required
def api_metrics_seeding():
    """Every torrent currently seeding, with how much it moved in the range.

    The list comes from live daemon state rather than the traffic history, so
    a torrent that is seeding but hasn't been asked for a byte still appears
    (with a zero total) instead of silently missing."""
    rng = request.args.get("range", "24h")
    span = _METRICS_RANGES.get(rng)
    if span is None:
        return jsonify({"ok": False, "error": "invalid range"}), 400
    metric = request.args.get("metric", "up")
    if metric not in ("up", "down"):
        return jsonify({"ok": False, "error": "invalid metric"}), 400
    field = "up_bytes" if metric == "up" else "down_bytes"

    since = int(time.time()) - span
    try:
        torrents = client.get_stats_torrents() or []
        # No limit: this is joined against the live seeding set, which bounds
        # it, and a torrent missing from here would look like it never seeded.
        totals = db.get_torrent_traffic_totals(
            since, limit=_SEEDING_LIST_MAX, field=field)
        has_history = bool(totals) or db.has_torrent_traffic()
    except Exception as e:
        return _err(e)

    by_hash = {r["hash"]: r for r in totals}
    custom = db.get_custom_names_map()
    rows = []
    for t in torrents:
        if t.get("status") not in _SEEDING_STATUSES:
            continue
        h = t.get("hashString")
        if not h:
            continue
        rec = by_hash.get(h) or {}
        rows.append({
            "hash": h,
            "id": t.get("id"),
            "name": custom.get(h) or t.get("name") or h[:12],
            "bytes": rec.get(field) or 0,
            "peers": int(t.get("peersConnected") or 0),
            # Lifetime counters straight from the daemon. Shown beside the
            # windowed total so a zero can be read correctly: large lifetime
            # means "quiet lately", near-zero means "never wanted", and the
            # two look identical without this.
            "uploaded_ever": int(t.get("uploadedEver") or 0),
            "downloaded_ever": int(t.get("downloadedEver") or 0),
            "ratio": t.get("uploadRatio"),
        })
    # Movers first; the idle tail keeps a stable alphabetical order so rows
    # don't shuffle under the user between polls.
    rows.sort(key=lambda r: (-r["bytes"], (r["name"] or "").lower()))
    return jsonify({
        "ok": True,
        "range": rng,
        "metric": metric,
        "has_history": has_history,
        "sampler_enabled": config.METRICS_SAMPLE_INTERVAL > 0,
        "count": len(rows),
        "active": sum(1 for r in rows if r["bytes"] > 0),
        "torrents": rows,
    })


@app.route("/api/metrics/torrents/<hash>/series")
@login_required
def api_metrics_torrent_series(hash):
    """One torrent's transfer series — fetched when its row is expanded, so
    a long seeding list doesn't pay for graphs nobody opened."""
    if not _HASH_RE.match(hash or ""):
        return jsonify({"ok": False, "error": "invalid hash"}), 400
    rng = request.args.get("range", "24h")
    span = _METRICS_RANGES.get(rng)
    if span is None:
        return jsonify({"ok": False, "error": "invalid range"}), 400
    metric = request.args.get("metric", "up")
    if metric not in ("up", "down"):
        return jsonify({"ok": False, "error": "invalid metric"}), 400
    field = "up_bytes" if metric == "up" else "down_bytes"

    since = int(time.time()) - span
    try:
        series = db.get_torrent_traffic_series(
            since, [hash], buckets=120, field=field)
        _, step_secs = db.traffic_series_grid(since, 120)
    except Exception as e:
        return _err(e)
    points = series.get(hash, [])
    return jsonify({
        "ok": True,
        "hash": hash,
        "range": rng,
        "metric": metric,
        "step_secs": step_secs,
        "total": sum(p["v"] for p in points),
        "points": points,
    })


# ---------- tunnel status ----------
#
# Transmission's outbound traffic is meant to be bound to the WireGuard
# tunnel via the daemon's bind-address-ipv4 setting. If the tunnel drops
# but the bind stays pinned to a now-vanished tunnel IP, peer connections
# silently stop working; if the bind isn't set at all, traffic leaks out
# the bare ISP link. The status indicator surfaces both failures.

def _wg_show_dump(iface):
    """Run `wg show <iface> dump` once and parse it. Returns:
      None — the wg binary is unavailable or the call timed out. Caller
             must treat this as "can't tell" (error), not "no peers" (down).
      {}   — wg ran but the interface is unknown to it.
      dict — parsed peer summary.

    Replaces three back-to-back `wg show <iface> <latest-handshakes|transfer|
    endpoints>` invocations the dashboard used to make per tunnel check.
    """
    try:
        r = subprocess.run(
            ["wg", "show", iface, "dump"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    except Exception:
        return None
    if r.returncode != 0:
        return {}
    return _parse_wg_dump(r.stdout)


def _parse_wg_dump(text):
    """`wg show <iface> dump` is one tab-separated line for the interface
    followed by one per peer. Peer columns are:
      0 public-key  1 preshared-key  2 endpoint  3 allowed-ips
      4 latest-handshake (unix ts)   5 rx-bytes  6 tx-bytes  7 keepalive
    """
    info = {
        "last_handshake_seconds": None,
        "rx_bytes": None,
        "tx_bytes": None,
        "endpoint": None,
    }
    lines = [l for l in text.strip().splitlines() if l]
    if len(lines) < 2:
        return info
    rx_total = tx_total = 0
    any_xfer = False
    newest_hs = 0
    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) < 8:
            # Fall back to whitespace split for older wg-tools builds that
            # emit space-separated columns.
            parts = line.split()
        if len(parts) < 8:
            continue
        endpoint = parts[2]
        try:
            hs = int(parts[4])
            rx = int(parts[5])
            tx = int(parts[6])
        except ValueError:
            continue
        if endpoint and endpoint != "(none)" and info["endpoint"] is None:
            info["endpoint"] = endpoint
        if hs > newest_hs:
            newest_hs = hs
        rx_total += rx
        tx_total += tx
        any_xfer = True
    if newest_hs > 0:
        info["last_handshake_seconds"] = max(0, int(time.time() - newest_hs))
    if any_xfer:
        info["rx_bytes"] = rx_total
        info["tx_bytes"] = tx_total
    return info


def _tunnel_status(iface, dump=None):
    """Inspect a WireGuard interface and return a dict with up/down state,
    assigned IPv4 address, and (best-effort) last-handshake age + transfer
    counters. Never raises — every field can be None if we couldn't check.

    `dump`, when passed, is a pre-fetched _wg_show_dump() result; otherwise
    the call fetches its own. Sharing one dump across _do_tunnel_check and
    /api/tunnel callers avoids spawning wg twice in a row.
    """
    info = {
        "interface": iface,
        "interface_up": False,
        "interface_exists": False,
        "interface_address": None,
        "interface_address6": None,
        "last_handshake_seconds": None,
        "rx_bytes": None,
        "tx_bytes": None,
        "endpoint": None,
        "wg_available": True,
        "error": None,
    }
    try:
        stats = psutil.net_if_stats().get(iface)
        addrs = psutil.net_if_addrs().get(iface) or []
    except Exception as e:
        info["error"] = f"interface lookup failed: {e}"
        return info
    if stats is None:
        info["error"] = f"interface {iface} not found"
        return info
    info["interface_exists"] = True
    info["interface_up"] = bool(stats.isup)
    for a in addrs:
        if a.family == socket.AF_INET and info["interface_address"] is None:
            info["interface_address"] = a.address
        elif a.family == socket.AF_INET6 and info["interface_address6"] is None:
            # The tunnel's own IPv6 (skip link-local / unspecified). Used to
            # confirm transmission is bound to it, not leaking out a bare v6.
            addr6 = (a.address or "").split("%")[0]
            if addr6 and not addr6.lower().startswith("fe80") and addr6 not in ("::1", "::"):
                info["interface_address6"] = addr6

    if dump is None:
        dump = _wg_show_dump(iface)
    if dump is None:
        info["wg_available"] = False
        return info
    info["last_handshake_seconds"] = dump.get("last_handshake_seconds")
    info["rx_bytes"] = dump.get("rx_bytes")
    info["tx_bytes"] = dump.get("tx_bytes")
    info["endpoint"] = dump.get("endpoint")
    return info


def _transmission_bind_check(iface_addr):
    """Return (bound, bind_address). bound is True/False/None — None when
    we couldn't read the daemon's bind-address-ipv4 setting at all.
    """
    try:
        bind = client.get_session_bind_address()
    except Exception:
        return None, None
    if bind is None:
        return None, None
    if not iface_addr:
        # No tunnel address to compare against; surface the bind for the
        # UI tooltip but don't claim it's bound or unbound.
        return None, bind
    return (bind == iface_addr), bind


def _route_egress_dev(dst, src):
    """Which interface would a packet to `dst`, sourced from `src`, actually
    leave by? Returns the device name, or None if it can't be determined
    (no route, `src` not local, `ip` missing, or unparseable output).

    This verifies the piece the bind check can't: binding transmission to the
    tunnel IP only keeps traffic on the tunnel if packets *from* that IP are
    routed out the tunnel interface (the policy-routing rule from the VPN
    setup). If that rule is missing, a tunnel-sourced packet egresses the bare
    link instead — a leak the bind alone would never catch. `ip route get`
    is provider-agnostic: it reports the real egress dev regardless of which
    routing table or scheme puts it there.
    """
    if not src:
        return None
    try:
        r = subprocess.run(
            ["ip", "route", "get", dst, "from", src],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    except Exception:
        return None
    # On error (e.g. "Network is unreachable") ip writes to stderr and leaves
    # stdout empty → no dev match → None. A success line looks like:
    #   1.1.1.1 from <src> dev mullvad table 200 ...
    m = re.search(r"\bdev\s+(\S+)", r.stdout or "")
    return m.group(1) if m else None


def _is_global_ipv6(addr):
    """True if `addr` is a globally-routable IPv6 (2000::/3) — the kind that
    can actually leak to the internet. Excludes link-local (fe80::/10),
    unique-local (fc00::/7), loopback and unspecified."""
    if not addr:
        return False
    a = addr.split("%")[0].strip().lower()
    if a in ("::1", "::"):
        return False
    # Global unicast is 2000::/3 → first hextet starts 2xxx or 3xxx. Everything
    # else (fe80 link-local, fc00/fd00 ULA, etc.) is not internet-routable.
    return a[:1] in ("2", "3")


def _host_has_bare_global_ipv6(tunnel_iface):
    """Does any non-tunnel, non-loopback interface carry a globally-routable
    IPv6? If so, transmission can leak v6 traffic out the bare link unless it's
    pinned to the tunnel's own v6. Returns True/False, or None if interface
    enumeration failed (can't tell)."""
    try:
        all_addrs = psutil.net_if_addrs()
    except Exception:
        return None
    for ifname, addrs in all_addrs.items():
        if ifname == tunnel_iface or ifname == "lo":
            continue
        for a in addrs:
            if a.family == socket.AF_INET6 and _is_global_ipv6(a.address):
                return True
    return False


def _transmission_bind6_check(iface_addr6):
    """(bound, bind_address) for IPv6, mirroring _transmission_bind_check.
    bound is True/False/None — None when the setting couldn't be read at all."""
    try:
        bind = client.get_session_bind_address_ipv6()
    except Exception:
        return None, None
    if not iface_addr6:
        # Tunnel has no IPv6 to compare against; surface the bind but don't
        # claim bound/unbound.
        return None, bind
    return (bind == iface_addr6), bind


def _ipv6_leak_check(iface, tunnel_addr6):
    """Assess whether transmission could leak IPv6 out a bare link. The IPv4
    bind check says nothing about IPv6, so a host with native IPv6 can leak the
    real v6 address while the v4 indicator is green.

    Returns (verdict, detail) where verdict is one of:
      'safe'    — no bare global IPv6 on the host, or transmission is bound to
                  the tunnel's own IPv6 and that traffic egresses the tunnel.
      'leak'    — a bare global IPv6 exists and transmission is not pinned to
                  the tunnel's IPv6 (unset/::, mismatched, or the tunnel has no
                  IPv6 to bind to) → a real leak path.
      'route'   — bound to the tunnel v6, but v6 packets from it egress a
                  different interface (routing leak).
      'unknown' — couldn't enumerate addresses or read the bind setting.
    """
    detail = {"host_bare_ipv6": None, "bind6": None, "route_egress_dev6": None}
    host_v6 = _host_has_bare_global_ipv6(iface)
    detail["host_bare_ipv6"] = host_v6
    if host_v6 is None:
        return "unknown", detail
    if not host_v6:
        # No globally-routable IPv6 on any bare interface → nothing to leak
        # through, whatever transmission's v6 bind is.
        return "safe", detail
    bound6, bind6 = _transmission_bind6_check(tunnel_addr6)
    detail["bind6"] = bind6
    if bound6 is None and bind6 is None:
        # Couldn't read the v6 bind → can't confirm it's confined.
        return "unknown", detail
    if not (tunnel_addr6 and bound6 is True):
        # Bare v6 present and transmission isn't pinned to the tunnel's v6.
        return "leak", detail
    # Pinned to the tunnel v6 — confirm it actually routes out the tunnel.
    route6 = _route_egress_dev("2606:4700:4700::1111", tunnel_addr6)
    detail["route_egress_dev6"] = route6
    if route6 is not None and route6 != iface:
        return "route", detail
    return "safe", detail


# Map the authoritative _do_tunnel_check() status onto the coarse "overall"
# the System page's tunnel panel renders. Both endpoints now share one check
# so the panel and the topbar can never disagree (and both catch leaks).
_STATUS_TO_OVERALL = {
    "up": "ok",
    "down": "down",
    "error": "unknown",
    "disabled": "off",
}


@app.route("/api/tunnel")
@login_required
def api_tunnel():
    data = _cached_tunnel_check()
    return jsonify({
        "ok": True,
        "overall": _STATUS_TO_OVERALL.get(data.get("status"), "unknown"),
        **data,
    })


# ---------- tunnel-status indicator ----------
#
# The visible "Tunnel" indicator must mean: transmission-daemon's outbound
# traffic is actually confined to the WireGuard tunnel. That requires all of
# these to hold simultaneously, all derived from live state — nothing
# hardcoded, since the tunnel IP can change every time the WG config is
# regenerated:
#   1. the wg binary is callable
#   2. config.TUNNEL_IFACE exists, is up, and has an assigned IPv4
#   3. at least one peer handshake within WG_HANDSHAKE_STALE_SEC
#   4. transmission's bind-address-ipv4 equals the iface's current IPv4
#   5. packets from the tunnel IPv4 actually egress the tunnel interface
#      (the policy route is in place — a bind with no route still leaks/fails)
#   6. no IPv6 leak: either the host has no bare global IPv6, or transmission's
#      bind-address-ipv6 is the tunnel's IPv6 AND it egresses the tunnel
# A green light asserts all six; any one failing is down (leak/misconfig) or
# error (can't confirm) — never a false green.

_tunnel_check_cache = {"at": 0.0, "data": None}
_tunnel_check_lock = threading.Lock()


def _do_tunnel_check():
    """Probe the tunnel and transmission's bind. Never raises. Returns:
      up    — interface healthy with a fresh handshake AND transmission is
              bound to the interface's current IPv4.
      down  — interface missing/down, no IPv4, no/stale handshake, or
              transmission is not bound to the tunnel IP.
      error — the wg binary is unavailable, psutil failed, or we couldn't
              read transmission's bind setting; we can't tell either way.
    """
    iface = config.TUNNEL_IFACE
    result = {
        "status": "error",
        "reason": None,
        "interface": iface,
        "interface_exists": None,
        "interface_up": None,
        "interface_address": None,
        "interface_address6": None,
        "last_handshake_seconds": None,
        "stale_after_seconds": config.WG_HANDSHAKE_STALE_SEC,
        "endpoint": None,
        "rx_bytes": None,
        "tx_bytes": None,
        "transmission_bound": None,
        "transmission_bind_address": None,
        "transmission_bind_address6": None,
        "host_bare_ipv6": None,
        "route_egress_dev": None,
        "route_egress_dev6": None,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "error": None,
        "cached": False,
    }

    if not iface:
        # No interface configured. Per config.py this means the operator hasn't
        # opted into the tunnel indicator — surface a distinct "disabled" state
        # so the UI hides the indicator, rather than probing an empty iface name
        # (which `wg`/psutil report as missing → a bogus red "down").
        result["status"] = "disabled"
        result["reason"] = "not_configured"
        return result

    dump = _wg_show_dump(iface)
    if dump is None:
        result["reason"] = "wg_missing"
        result["error"] = "wg binary not available"
        return result

    try:
        info = _tunnel_status(iface, dump=dump)
    except Exception as e:
        result["reason"] = "iface_lookup_failed"
        result["error"] = f"interface lookup failed: {e}"
        return result

    # Collect every live signal up front — including transmission's bind —
    # so the payload/tooltip shows the full picture no matter which check
    # ends up tripping the verdict below. (The old code early-returned at
    # the first failure and threw away the rest, e.g. an "interface down"
    # verdict hid the handshake age and bind address entirely.)
    tunnel_addr = info.get("interface_address")
    tunnel_addr6 = info.get("interface_address6")
    bound, bind_addr = _transmission_bind_check(tunnel_addr)
    result["interface_exists"] = info.get("interface_exists")
    result["interface_up"] = info.get("interface_up")
    result["interface_address"] = tunnel_addr
    result["interface_address6"] = tunnel_addr6
    result["last_handshake_seconds"] = info.get("last_handshake_seconds")
    result["endpoint"] = info.get("endpoint")
    result["rx_bytes"] = info.get("rx_bytes")
    result["tx_bytes"] = info.get("tx_bytes")
    result["transmission_bound"] = bound
    result["transmission_bind_address"] = bind_addr

    # Verdict, most-fundamental failure first. `reason` is a stable machine
    # code (unlike the free-text `error`) so the UI and history can key off it.
    if not info.get("interface_exists"):
        result["status"] = "down"
        result["reason"] = "iface_missing"
        result["error"] = f"interface {iface} not found"
        return result
    if not info.get("interface_up"):
        result["status"] = "down"
        result["reason"] = "iface_down"
        result["error"] = f"interface {iface} is down"
        return result
    if not tunnel_addr:
        result["status"] = "down"
        result["reason"] = "no_ipv4"
        result["error"] = f"interface {iface} has no IPv4 address"
        return result
    hs = info.get("last_handshake_seconds")
    if hs is None:
        result["status"] = "down"
        result["reason"] = "no_handshake"
        result["error"] = "no peer handshake recorded"
        return result
    if hs > config.WG_HANDSHAKE_STALE_SEC:
        result["status"] = "down"
        result["reason"] = "stale_handshake"
        result["error"] = (
            f"last handshake {hs}s ago "
            f"(threshold {config.WG_HANDSHAKE_STALE_SEC}s)"
        )
        return result

    if bound is None:
        # Interface healthy but the bind setting is unreadable — we can't
        # confirm traffic is pinned to the tunnel, so it's error (can't tell),
        # not down.
        result["reason"] = "bind_unreadable"
        result["error"] = "could not read transmission bind-address"
        return result
    if not bound:
        result["status"] = "down"
        result["reason"] = "not_bound"
        result["error"] = (
            f"transmission bound to {bind_addr or '0.0.0.0'} — "
            f"not the tunnel IP {tunnel_addr}"
        )
        return result

    # --- Beyond the IPv4 bind: verify the pieces the bind alone can't. ---

    # (5) IPv4 routing. Binding to the tunnel IP only confines traffic if
    # packets from it actually egress the tunnel; a missing policy route sends
    # them out the bare link instead — a leak the bind check can't see.
    route_dev = _route_egress_dev("1.1.1.1", tunnel_addr)
    result["route_egress_dev"] = route_dev
    if route_dev is not None and route_dev != iface:
        result["status"] = "down"
        result["reason"] = "route_leak"
        result["error"] = (
            f"packets from the tunnel IP {tunnel_addr} egress '{route_dev}', "
            f"not the tunnel '{iface}' — routing leak (check the policy route)"
        )
        return result

    # (6) IPv6. Everything above is IPv4-only; a host with native IPv6 can leak
    # the real v6 address over BitTorrent while v4 looks perfect.
    v6_verdict, v6_detail = _ipv6_leak_check(iface, tunnel_addr6)
    result["host_bare_ipv6"] = v6_detail.get("host_bare_ipv6")
    result["transmission_bind_address6"] = v6_detail.get("bind6")
    result["route_egress_dev6"] = v6_detail.get("route_egress_dev6")
    if v6_verdict == "leak":
        result["status"] = "down"
        result["reason"] = "ipv6_leak"
        result["error"] = (
            "IPv6 leak: the host has a global IPv6 address but transmission's "
            f"bind-address-ipv6 ({v6_detail.get('bind6') or 'unset'}) is not the "
            f"tunnel's IPv6 ({tunnel_addr6 or 'none'}). Set bind-address-ipv6 to "
            "the tunnel's IPv6 or disable IPv6 on the host."
        )
        return result
    if v6_verdict == "route":
        result["status"] = "down"
        result["reason"] = "route_leak_v6"
        result["error"] = (
            f"IPv6 packets from the tunnel address {tunnel_addr6} egress "
            f"'{v6_detail.get('route_egress_dev6')}', not the tunnel '{iface}'"
        )
        return result
    if v6_verdict == "unknown":
        # Bare v6 may be present but we couldn't read the v6 bind or enumerate
        # addresses — can't confirm v6 is confined, so don't flip green.
        result["status"] = "error"
        result["reason"] = "ipv6_unverifiable"
        result["error"] = (
            "could not verify IPv6 is confined to the tunnel "
            "(bind-address-ipv6 unreadable or interface enumeration failed)"
        )
        return result

    result["status"] = "up"
    result["reason"] = "ok"
    return result


def _cached_tunnel_check(force=False):
    """Return a recent _do_tunnel_check() result, refreshing at most once per
    config.TUNNEL_CHECK_CACHE_TTL seconds (unless force=True). The lock prevents
    a thundering herd of concurrent pollers from each firing their own probe.
    """
    now = time.time()
    with _tunnel_check_lock:
        cached = _tunnel_check_cache["data"]
        if not force and cached is not None and (now - _tunnel_check_cache["at"]) < config.TUNNEL_CHECK_CACHE_TTL:
            return dict(cached, cached=True)
        fresh = _do_tunnel_check()
        _tunnel_check_cache["at"] = time.time()
        _tunnel_check_cache["data"] = fresh
        return dict(fresh, cached=False)


@app.route("/api/tunnel-status")
@login_required
def api_tunnel_status():
    force = request.args.get("fresh", "").lower() in ("1", "true", "yes")
    data = _cached_tunnel_check(force=force)
    return jsonify({
        "ok": True,
        "cache_ttl_seconds": config.TUNNEL_CHECK_CACHE_TTL,
        "recovery": _tunnel_recovery_state(),
        **data,
    })


# ---------- tunnel auto-recovery watchdog ----------
#
# A WireGuard session can wedge permanently: the kernel retries handshakes
# from one fixed source port, so if the path dies (relay reboot, stale NAT
# mapping) every retry lands in a black hole and the tunnel never heals —
# observed 2026-07-16 as 16.5h of "Tunnel down" that a `wg-quick down && up`
# (fresh source port) fixed instantly. When TUNNEL_RECOVERY_CMD is set, this
# watchdog runs it after the check has been continuously down with that
# wedged-session signature. Guards, in order:
#   - only reasons a bounce can plausibly fix (stale/missing handshake) —
#     an operator's deliberate `wg-quick down` removes the interface, which
#     reads as iface_missing and is left alone;
#   - the outage must persist TUNNEL_RECOVERY_AFTER_SEC before the first try;
#   - TUNNEL_RECOVERY_COOLDOWN_SEC between tries;
#   - at most TUNNEL_RECOVERY_MAX_ATTEMPTS consecutive tries (a bounce can't
#     fix an expired account — don't flap all night), reset when the tunnel
#     comes back up.
# The thread only starts when both TUNNEL_IFACE and TUNNEL_RECOVERY_CMD are
# configured, so unconfigured installs (and the test suite) never spawn it.

_RECOVERABLE_REASONS = {"stale_handshake", "no_handshake"}

_tunnel_recovery_lock = threading.Lock()
_tunnel_recovery = {
    "down_since": None,        # monotonic; start of the current qualifying outage
    "attempts": 0,             # consecutive attempts in this outage
    "last_attempt_mono": None, # monotonic; cooldown reference
    "last_attempt_at": None,   # ISO wall time, for the UI tooltip
    "last_result": None,       # "ok" | "exit N" | "timeout" | "failed: ..."
    "gave_up": False,          # attempts exhausted for this outage
}


def _tunnel_recovery_state():
    """Snapshot for the /api/tunnel-status payload."""
    with _tunnel_recovery_lock:
        return {
            "enabled": bool(config.TUNNEL_RECOVERY_CMD),
            "attempts": _tunnel_recovery["attempts"],
            "max_attempts": config.TUNNEL_RECOVERY_MAX_ATTEMPTS,
            "last_attempt_at": _tunnel_recovery["last_attempt_at"],
            "last_result": _tunnel_recovery["last_result"],
            "gave_up": _tunnel_recovery["gave_up"],
        }


def _run_tunnel_recovery_cmd():
    """Run the operator's bounce command. Returns a short result string."""
    try:
        proc = subprocess.run(
            config.TUNNEL_RECOVERY_CMD,
            shell=True, capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return "timeout"
    except Exception as e:
        return f"failed: {e}"
    if proc.returncode == 0:
        return "ok"
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()
    return f"exit {proc.returncode}" + (f": {tail[-1][:200]}" if tail else "")


def _tunnel_recovery_tick(now=None):
    """One watchdog evaluation. Split from the worker loop for testability."""
    now = time.monotonic() if now is None else now
    check = _cached_tunnel_check()
    st = _tunnel_recovery
    with _tunnel_recovery_lock:
        if check.get("status") == "up":
            # Healthy again — close out the outage and restore the attempt
            # budget for the next one.
            st.update(down_since=None, attempts=0, gave_up=False)
            return
        if not (check.get("status") == "down"
                and check.get("reason") in _RECOVERABLE_REASONS):
            # Down for a reason a bounce can't fix (unbound transmission,
            # deliberately-removed interface) or an error state — stand down,
            # but keep the attempt counter: flipping reasons mustn't refill
            # the budget mid-outage.
            st["down_since"] = None
            return
        if st["down_since"] is None:
            st["down_since"] = now
        if now - st["down_since"] < config.TUNNEL_RECOVERY_AFTER_SEC:
            return
        if st["attempts"] >= config.TUNNEL_RECOVERY_MAX_ATTEMPTS:
            if not st["gave_up"]:
                st["gave_up"] = True
                db.log_event(
                    "tunnel.recovery", "error",
                    f"Tunnel auto-recovery giving up after "
                    f"{st['attempts']} attempts — manual intervention needed",
                    details={"reason": check.get("reason")},
                )
            return
        if (st["last_attempt_mono"] is not None
                and now - st["last_attempt_mono"] < config.TUNNEL_RECOVERY_COOLDOWN_SEC):
            return
        st["attempts"] += 1
        st["last_attempt_mono"] = now
        st["last_attempt_at"] = datetime.now(timezone.utc).isoformat()
        attempt = st["attempts"]

    # Run the command outside the lock — it can take tens of seconds and
    # /api/tunnel-status readers must not block behind it.
    result = _run_tunnel_recovery_cmd()
    with _tunnel_recovery_lock:
        st["last_result"] = result
    db.log_event(
        "tunnel.recovery",
        "info" if result == "ok" else "error",
        f"Tunnel auto-recovery attempt {attempt}/"
        f"{config.TUNNEL_RECOVERY_MAX_ATTEMPTS}: {result} "
        f"(down: {check.get('reason')})",
        details={"reason": check.get("reason"), "result": result},
    )
    # Refresh the cached verdict promptly so the UI (and the next tick)
    # reflects the post-bounce state instead of a pre-bounce cache entry.
    # A successful handshake can take a few seconds; give it a moment.
    time.sleep(5)
    _cached_tunnel_check(force=True)


def _tunnel_recovery_worker():
    # Tick at the check-cache cadence — _cached_tunnel_check() makes each
    # tick nearly free when the UI is already polling.
    interval = max(config.TUNNEL_CHECK_CACHE_TTL, 10.0)
    while True:
        time.sleep(interval)
        try:
            _tunnel_recovery_tick()
        except Exception:
            # Never let the watchdog die; next tick retries from scratch.
            pass


if config.TUNNEL_IFACE and config.TUNNEL_RECOVERY_CMD:
    threading.Thread(target=_tunnel_recovery_worker, daemon=True).start()


# ---------- metrics sampler ----------
#
# Snapshots torrent + system stats into SQLite on a fixed interval so the
# System page can chart trends over time. Runs in one background daemon (the
# production deploy is a single gunicorn worker); the metrics_samples primary
# key is the second-granularity timestamp, so even an accidental double-start
# (e.g. the Werkzeug reloader) can't create duplicate rows.

_metrics_sampler_started = False
# Own net-counter baseline so rate deltas don't contend with the /api/system
# request path, which keeps its own baseline in _net_rates().
_sampler_prev_net = None
_sampler_prev_ts = None


def _sampler_net_rates():
    global _sampler_prev_net, _sampler_prev_ts
    now = time.monotonic()
    cur = psutil.net_io_counters()
    if _sampler_prev_net is None or _sampler_prev_ts is None:
        _sampler_prev_net, _sampler_prev_ts = cur, now
        return 0, 0
    dt = now - _sampler_prev_ts
    if dt <= 0:
        return 0, 0
    rx = max(0, cur.bytes_recv - _sampler_prev_net.bytes_recv) / dt
    tx = max(0, cur.bytes_sent - _sampler_prev_net.bytes_sent) / dt
    _sampler_prev_net, _sampler_prev_ts = cur, now
    return int(rx), int(tx)


def _collect_metric_sample():
    """Build one metrics_samples row from live Transmission + psutil state.

    Returns (sample, torrents) — the per-torrent payload is handed back so the
    caller can derive traffic deltas from it without a second RPC round-trip."""
    sess = client.get_session_stats() or {}
    torrents = client.get_stats_torrents() or []
    cumulative = sess.get("cumulative-stats") or {}
    up = int(cumulative.get("uploadedBytes") or 0)
    down = int(cumulative.get("downloadedBytes") or 0)
    rx_rate, tx_rate = _sampler_net_rates()
    try:
        disk_pct = psutil.disk_usage(_disk_target()).percent
    except Exception:
        disk_pct = None
    sample = {
        "download_speed": int(sess.get("downloadSpeed") or 0),
        "upload_speed": int(sess.get("uploadSpeed") or 0),
        "uploaded_bytes": up,
        "downloaded_bytes": down,
        "ratio": (up / down) if down > 0 else None,
        "active_count": int(sess.get("activeTorrentCount") or 0),
        "torrent_count": int(sess.get("torrentCount") or len(torrents)),
        "swarm_peers": sum(int(t.get("peersConnected") or 0) for t in torrents),
        "cpu_percent": psutil.cpu_percent(interval=None),
        "mem_percent": psutil.virtual_memory().percent,
        "disk_percent": disk_pct,
        "net_rx_rate": rx_rate,
        "net_tx_rate": tx_rate,
    }
    return sample, torrents


# Last-seen cumulative uploadedEver/downloadedEver per torrent hash, used to
# turn Transmission's monotonic counters into per-bucket deltas. In memory
# only: after a restart the first tick just re-establishes the baseline, which
# costs at most one sampler interval of attribution.
_traffic_prev = {}


def _record_torrent_traffic(torrents, now):
    """Diff per-torrent byte counters against the previous tick and add the
    deltas to the current time bucket."""
    bucket = (int(now) // db.TRAFFIC_BUCKET_SECS) * db.TRAFFIC_BUCKET_SECS
    seen = {}
    rows = []
    for t in torrents:
        h = t.get("hashString")
        if not h:
            continue
        up = int(t.get("uploadedEver") or 0)
        down = int(t.get("downloadedEver") or 0)
        seen[h] = (up, down)
        prev = _traffic_prev.get(h)
        if prev is None:
            # First sighting this process — nothing to attribute yet.
            continue
        # A counter that went backwards means the torrent was re-added or
        # re-verified and Transmission reset it. Skip the tick rather than
        # recording a negative or treating the new low value as a huge delta.
        d_up = up - prev[0]
        d_down = down - prev[1]
        if d_up < 0 or d_down < 0:
            continue
        if d_up or d_down:
            rows.append((h, t.get("name"), d_up, d_down))
    # Replace wholesale so hashes for removed torrents don't leak.
    _traffic_prev.clear()
    _traffic_prev.update(seen)
    db.add_torrent_traffic(bucket, rows)


def _metrics_sampler_worker():
    interval = max(5.0, config.METRICS_SAMPLE_INTERVAL)
    # Prime the net-rate baseline so the first stored sample isn't a zero.
    _sampler_net_rates()
    # A sampler that fails every tick used to look exactly like a quiet
    # swarm — empty charts, no explanation. Surface it as an event instead,
    # but only when the failure signature changes or an hour has passed, so
    # a persistent fault can't flood the events log.
    last_error = None
    last_error_at = 0.0
    while True:
        time.sleep(interval)
        try:
            now = int(time.time())
            sample, torrents = _collect_metric_sample()
            db.insert_metric_sample(now, **sample)
            _record_torrent_traffic(torrents, now)
            db._maybe_prune_metrics()
            if last_error is not None:
                db.log_event("metrics.sampler", "info",
                             "Metrics sampler recovered")
                last_error = None
        except Exception as e:
            # Never let the sampler die; the next tick retries from scratch.
            sig = f"{type(e).__name__}: {e}"
            mono = time.monotonic()
            if sig != last_error or mono - last_error_at > 3600:
                last_error, last_error_at = sig, mono
                try:
                    db.log_event(
                        "metrics.sampler", "error",
                        "Metrics sampler tick failed; System page graphs "
                        "will stop updating until it recovers",
                        details={"error": sig},
                    )
                except Exception:
                    # The DB itself is the likely fault — don't recurse.
                    pass


if config.METRICS_SAMPLE_INTERVAL > 0 and not _metrics_sampler_started:
    _metrics_sampler_started = True
    threading.Thread(target=_metrics_sampler_worker, daemon=True).start()


# ---------- update-available indicator ----------
#
# When the running checkout is behind its git upstream (someone pushed new
# commits), the topbar shows an "N updates" badge (static/updates.js). The
# check mirrors the tunnel-status pattern: a cache dict + Lock + TTL, with a
# best-effort `git fetch` running at most once per TTL. Every failure mode —
# not a git checkout, no upstream configured, git missing, or offline —
# degrades to "no badge" rather than erroring.

_REPO_DIR = os.path.dirname(os.path.abspath(__file__))
_update_check_cache = {"at": 0.0, "data": None}
_update_check_lock = threading.Lock()


def _git(args, timeout=10):
    """Run a git command inside the repo. Raises on missing binary/timeout;
    otherwise returns the CompletedProcess (caller inspects returncode)."""
    return subprocess.run(
        ["git", "-C", _REPO_DIR, *args],
        capture_output=True, text=True, timeout=timeout,
    )


def _do_update_check():
    """Compare HEAD against its upstream tracking branch. Never raises.

    Returns a dict the frontend uses to decide whether to show a badge.
    `behind` > 0 means new commits were pushed upstream. `stale` is set when
    the `git fetch` failed (offline/remote down) but we could still compare
    against the last-known remote-tracking ref — the count may be out of date.
    """
    result = {
        "ok": True,
        "enabled": True,
        "is_git": False,
        "has_upstream": False,
        "behind": 0,
        "current_sha": None,
        "upstream_sha": None,
        "upstream_subject": None,
        "upstream_date": None,
        "fetched": False,
        "stale": False,
        "error": None,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    def run(args, timeout=10):
        try:
            return _git(args, timeout=timeout)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return None

    # 1. git present and this is a work tree?
    r = run(["rev-parse", "--is-inside-work-tree"], timeout=5)
    if r is None:
        result["error"] = "git not available"
        return result
    if r.returncode != 0 or r.stdout.strip() != "true":
        result["error"] = "not a git checkout"
        return result
    result["is_git"] = True

    # 2. an upstream tracking branch configured?
    up = run(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], timeout=5)
    if up is None or up.returncode != 0 or not up.stdout.strip():
        result["error"] = "no upstream configured"
        return result
    result["has_upstream"] = True

    cur = run(["rev-parse", "--short", "HEAD"], timeout=5)
    if cur is not None and cur.returncode == 0:
        result["current_sha"] = cur.stdout.strip()

    # 3. best-effort fetch. Failure isn't fatal — we still compare against the
    #    last-known remote-tracking ref and flag the result as stale.
    fetched = run(["fetch", "--quiet"], timeout=20)
    if fetched is not None and fetched.returncode == 0:
        result["fetched"] = True
    else:
        result["stale"] = True

    # 4. how many commits is HEAD behind the upstream?
    behind = run(["rev-list", "--count", "HEAD..@{u}"], timeout=10)
    if behind is not None and behind.returncode == 0:
        try:
            result["behind"] = int(behind.stdout.strip())
        except ValueError:
            pass

    # 5. describe the newest upstream commit for the tooltip. %x1f is the
    #    ASCII unit separator — safe against subjects containing anything.
    info = run(["log", "-1", "--format=%h%x1f%s%x1f%cI", "@{u}"], timeout=10)
    if info is not None and info.returncode == 0 and info.stdout.strip():
        parts = info.stdout.strip().split("\x1f")
        if len(parts) == 3:
            result["upstream_sha"] = parts[0]
            result["upstream_subject"] = parts[1]
            result["upstream_date"] = parts[2]

    return result


def _cached_update_check(force=False):
    """Return a recent _do_update_check() result, refreshing at most once per
    config.UPDATE_CHECK_CACHE_TTL seconds. The lock keeps concurrent pollers
    from each firing their own `git fetch`."""
    now = time.time()
    with _update_check_lock:
        cached = _update_check_cache["data"]
        if not force and cached is not None and (now - _update_check_cache["at"]) < config.UPDATE_CHECK_CACHE_TTL:
            return dict(cached, cached=True)
        fresh = _do_update_check()
        _update_check_cache["at"] = time.time()
        _update_check_cache["data"] = fresh
        return dict(fresh, cached=False)


@app.route("/api/update-status")
@login_required
def api_update_status():
    if not config.UPDATE_CHECK_ENABLED:
        return jsonify({"ok": True, "enabled": False, "behind": 0})
    force = request.args.get("fresh", "").lower() in ("1", "true", "yes")
    data = _cached_update_check(force=force)
    return jsonify({
        "cache_ttl_seconds": config.UPDATE_CHECK_CACHE_TTL,
        **data,
    })


@app.route("/api/settings", methods=["GET"])
@login_required
def api_settings_get():
    try:
        session_args = client.get_session()
        return jsonify({
            "ok": True,
            "download_dir": session_args.get("download-dir", ""),
        })
    except Exception as e:
        return _err(e)


@app.route("/api/settings", methods=["POST"])
@login_required
def api_settings_set():
    data = request.get_json(silent=True) or {}
    download_dir = (data.get("download_dir") or "").strip()
    if not download_dir:
        return _err("download_dir is required", 400)
    if not download_dir.startswith("/"):
        return _err("download_dir must be an absolute path", 400)
    try:
        client.set_download_dir(download_dir)
        session_args = client.get_session()
        return jsonify({
            "ok": True,
            "download_dir": session_args.get("download-dir", download_dir),
        })
    except Exception as e:
        return _err(e)


@app.route("/api/settings/relocate-all", methods=["POST"])
@login_required
def api_settings_relocate_all():
    try:
        session_args = client.get_session()
        location = session_args.get("download-dir", "")
        if not location:
            return _err("download-dir is not set on the Transmission daemon", 400)
        torrents = client.get_torrents()
        updated = 0
        failures = []
        for t in torrents:
            tid = t.get("id")
            if tid is None:
                continue
            if t.get("downloadDir") == location:
                continue
            try:
                client.set_location(tid, location, move=False)
                updated += 1
            except Exception as e:
                failures.append({"id": tid, "name": t.get("name"), "error": str(e)})
        return jsonify({
            "ok": True,
            "location": location,
            "total": len(torrents),
            "updated": updated,
            "failures": failures,
        })
    except Exception as e:
        return _err(e)


_legacy_copied_names = None


def _entry_belongs_to(entry, live_id, live_hash, live_name):
    """Does this copy-state entry actually describe the torrent now holding
    its id? Returns True (keep), False (stale — drop).

    Transmission ids are session-scoped: they are reassigned on daemon
    restart and reused after removals, so an entry keyed only by id can be
    inherited by an unrelated torrent — which then reads as already copied
    (the UI treats status == 'done' as sent even without the 'Copied'
    label). Entries written since the fix carry the infohash and are matched
    exactly. Older entries have no hash, so they are validated against
    copy_history: an id that finished a copy under exactly this torrent's
    name is that torrent. A legacy entry that can't be corroborated is
    dropped — genuine copies keep their 'Copied' label in Transmission, so
    the card still shows as sent.
    """
    global _legacy_copied_names
    if not live_hash:
        # Transmission didn't say what this id currently is, so there is no
        # basis to judge — never drop on missing evidence.
        return True
    recorded = entry.get("hash")
    if recorded:
        return recorded == live_hash
    if entry.get("status") != "done":
        # A hash-less entry that doesn't claim 'done' can't produce a false
        # "sent" chip or a delete-after-copy, so leave it be — it picks up a
        # hash on the next copy. Only the 'done' claim is worth challenging.
        return True
    if _legacy_copied_names is None:
        try:
            _legacy_copied_names = db.get_copied_names_by_id()
        except Exception:
            # No history to check against — keep the entry rather than risk
            # un-marking a real copy.
            return True
    return live_name in _legacy_copied_names.get(live_id, ())


def _gc_state_for_live_torrents(torrents):
    """Reconcile copy state against the torrents Transmission actually has.

    Drops entries whose id no longer exists (the state file grew unboundedly
    otherwise — every torrent you ever copied left a row behind after
    removal) and entries whose id has since been reassigned to a different
    torrent (see _entry_belongs_to).

    Skips entries belonging to active workers so an in-flight copy can
    still write its terminal state.
    """
    live = {}
    for t in torrents:
        tid = t.get("id")
        if tid is not None:
            live[str(tid)] = t

    with _copy_state_lock:
        cache = _ensure_copy_state_cache_unlocked()
        with _active_copies_lock:
            active = {str(t) for t in _active_copies}
        stale = []
        adopted = 0
        for k, entry in cache.items():
            if k in active:
                continue
            t = live.get(k)
            if t is None:
                stale.append((k, "torrent no longer exists"))
                continue
            live_hash = t.get("hashString") or None
            if not _entry_belongs_to(entry, t.get("id"), live_hash,
                                     t.get("name")):
                stale.append((k, f"id reassigned to {t.get('name')!r}"))
            elif (entry.get("status") == "done" and not entry.get("hash")
                  and live_hash):
                # Legacy entry corroborated by copy_history — stamp the hash
                # so it never has to be re-validated.
                entry["hash"] = live_hash
                adopted += 1
        dropped = []
        for k, reason in stale:
            entry = cache.pop(k, None) or {}
            # Only worth an event when we're discarding a copy the UI would
            # have shown as sent; idle/error rows are noise.
            if entry.get("status") == "done":
                dropped.append((k, reason, entry.get("dest_path")))
        if stale or adopted:
            _flush_copy_state_unlocked(force=True)

    # Logged outside the lock — an event write can block on the sqlite busy
    # timeout, and every copy-state reader would be stuck behind it.
    for k, reason, dest_path in dropped:
        try:
            db.log_event(
                "copy.state.stale", "info",
                f"Dropped stale copy state for id {k}: {reason}",
                torrent_id=int(k) if k.isdigit() else None,
                details={"dest_path": dest_path},
            )
        except Exception:
            pass


@app.route("/api/torrents")
@login_required
def api_torrents():
    try:
        torrents = client.get_torrents()
        names_map = db.get_custom_names_map()
        for t in torrents:
            h = t.get("hashString")
            if h and names_map.get(h):
                t["custom_name"] = names_map[h]
                t["default_name"] = t.get("name")
        _gc_state_for_live_torrents(torrents)
        return jsonify(torrents)
    except Exception as e:
        return _err(e)


# Transmission status codes for the export's readable status column.
_EXPORT_STATUS_LABELS = {
    0: "Stopped", 1: "Check pending", 2: "Checking",
    3: "Download pending", 4: "Downloading",
    5: "Seed pending", 6: "Seeding",
}


@app.route("/api/torrents/export.csv")
@login_required
def api_torrents_export_csv():
    """Every torrent's magnet link as a downloadable CSV, split by whether
    the data is complete. Purpose-built for rebuilding the daemon elsewhere:
    the two magnet columns let you paste all the incomplete ones (to fetch)
    separately from the finished ones (already-seeding). Completeness is by
    percentDone, so a *paused-but-finished* torrent correctly lands in the
    "downloaded / seeding" column rather than with the in-progress ones."""
    try:
        torrents = client.get_torrents_export()
    except Exception as e:
        return _err(e)
    names_map = db.get_custom_names_map()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "name", "status", "percent_done",
        "magnet_downloading_paused", "magnet_downloaded_seeding",
    ])
    for t in sorted(torrents, key=lambda x: (x.get("name") or "").lower()):
        h = t.get("hashString") or ""
        name = names_map.get(h) or t.get("name") or ""
        # magnetLink is empty until the daemon has fetched metadata; fall back
        # to a bare btih magnet so no torrent exports without a usable link.
        magnet = t.get("magnetLink") or (f"magnet:?xt=urn:btih:{h}" if h else "")
        pct = t.get("percentDone") or 0
        complete = pct >= 1.0
        writer.writerow([
            name,
            _EXPORT_STATUS_LABELS.get(t.get("status"), "Unknown"),
            f"{pct * 100:.1f}",
            "" if complete else magnet,
            magnet if complete else "",
        ])

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="torrents-{ts}.csv"',
        },
    )


@app.route("/api/torrent/<int:tid>")
@login_required
def api_torrent_detail(tid):
    try:
        detail = client.get_torrent_detail(tid)
        if detail is None:
            return _err("torrent not found", 404)
        h = detail.get("hashString")
        if h:
            custom = db.get_custom_name(h)
            if custom:
                detail["custom_name"] = custom
                detail["default_name"] = detail.get("name")
        return jsonify(detail)
    except Exception as e:
        return _err(e)


@app.route("/api/torrents/details", methods=["POST"])
@login_required
def api_torrents_details():
    """Multi-id detail fetch for the expanded-card poll.

    The /torrents page polls one detail per expanded card every 2s; with N
    expansions that was N parallel HTTP round-trips and N Transmission RPCs.
    Batching collapses both ends to one of each.
    """
    data = request.get_json(silent=True) or {}
    raw_ids = data.get("ids") or []
    if not isinstance(raw_ids, list):
        return _err("ids must be a list", 400)
    ids = []
    for r in raw_ids:
        try:
            ids.append(int(r))
        except (TypeError, ValueError):
            continue
    if not ids:
        return jsonify({"ok": True, "torrents": []})
    try:
        torrents = client.get_torrent_details(ids)
        names_map = db.get_custom_names_map()
        for t in torrents:
            h = t.get("hashString")
            if h and names_map.get(h):
                t["custom_name"] = names_map[h]
                t["default_name"] = t.get("name")
        return jsonify({"ok": True, "torrents": torrents})
    except Exception as e:
        return _err(e)


@app.route("/api/torrents/bitrates", methods=["POST"])
@login_required
def api_torrent_bitrates():
    """Media bitrate (declared file size / runtime) for the given torrents.

    Answers from cache only and queues anything unknown for the background
    probe, so the 5s poll never waits on ffprobe. Callers send the hash
    alongside the id because ids are reassigned across daemon restarts while
    the cache key has to stay stable.
    """
    data = request.get_json(silent=True) or {}
    items = data.get("torrents")
    if not isinstance(items, list):
        return _err("torrents must be a list", 400)

    want = []
    for it in items[:200]:
        if not isinstance(it, dict):
            continue
        thash = (it.get("hash") or "").strip()
        if not thash:
            continue
        try:
            want.append((int(it.get("id")), thash))
        except (TypeError, ValueError):
            continue

    out = {}
    queued = []
    now = time.time()
    with _bitrate_lock:
        for tid, thash in want:
            entry = _bitrate_cache.get(thash)
            if entry and entry["bps"]:
                out[str(tid)] = {
                    "bps": entry["bps"],
                    "bytes": entry["bytes"],
                    "duration": entry["duration"],
                }
                continue
            # A miss that failed recently, or is already in flight, waits.
            if entry and now - entry["ts"] < _BITRATE_RETRY_S:
                continue
            if thash in _bitrate_pending:
                continue
            _bitrate_pending.add(thash)
            queued.append((tid, thash))

    if queued:
        _ensure_bitrate_worker()
        for job in queued:
            _bitrate_jobs.put(job)

    return jsonify({"ok": True, "bitrates": out})


_TR_ERROR_KIND = {
    1: "tracker warning",
    2: "tracker error",
    3: "local error",
}


@app.route("/api/torrent/<int:tid>/start", methods=["POST"])
@login_required
def api_start(tid):
    try:
        client.start(tid)
    except Exception as e:
        app.logger.exception("torrent-start RPC failed for id=%s", tid)
        return _err(e)

    # Transmission accepts torrent-start even when the torrent immediately
    # halts (missing data, unreadable download dir, tracker rejection). Read
    # back the daemon's error field so the UI shows the actual reason
    # instead of a misleading "Started" toast.
    try:
        detail = client.get_torrent_detail(tid)
    except Exception as e:
        app.logger.exception("post-start detail fetch failed for id=%s", tid)
        return _err(f"Started, but could not read torrent state: {e}")

    err_code = (detail or {}).get("error", 0)
    err_str = ((detail or {}).get("errorString") or "").strip()
    if err_code:
        kind = _TR_ERROR_KIND.get(err_code, f"error code {err_code}")
        msg = f"Transmission reports {kind}: {err_str or 'no detail'}"
        app.logger.warning("torrent %s started with %s", tid, msg)
        return _err(msg)

    return jsonify({"ok": True})


@app.route("/api/torrent/<int:tid>/stop", methods=["POST"])
@login_required
def api_stop(tid):
    try:
        client.stop(tid)
        return jsonify({"ok": True})
    except Exception as e:
        return _err(e)


@app.route("/api/torrent/<int:tid>/verify", methods=["POST"])
@login_required
def api_verify(tid):
    try:
        client.verify(tid)
        return jsonify({"ok": True})
    except Exception as e:
        app.logger.exception("torrent-verify RPC failed for id=%s", tid)
        return _err(e)


def _archive_before_remove(tid, deleted_local_data):
    """Capture magnet + identifying info before a torrent is removed so it
    can be redownloaded from the Removed-torrents history later.

    Best-effort: failure here must not block the remove call. The user is
    asking to free space; we record what we can and move on.
    """
    try:
        detail = client.get_torrent_detail(tid)
    except Exception:
        detail = None
    if not detail:
        return
    h = detail.get("hashString")
    if not h:
        return
    custom = db.get_custom_name(h)
    try:
        db.record_removed_torrent(
            hash=h,
            name=detail.get("name"),
            magnet_link=detail.get("magnetLink"),
            total_size=detail.get("totalSize"),
            download_dir=detail.get("downloadDir"),
            custom_name=custom,
            deleted_local_data=deleted_local_data,
            uploaded_ever=detail.get("uploadedEver"),
        )
    except Exception:
        pass


@app.route("/api/torrent/<int:tid>/remove", methods=["POST"])
@login_required
def api_remove(tid):
    try:
        _archive_before_remove(tid, deleted_local_data=False)
        client.remove(tid, delete_local_data=False)
        return jsonify({"ok": True})
    except Exception as e:
        return _err(e)


@app.route("/api/torrent/<int:tid>/delete", methods=["POST"])
@login_required
def api_delete(tid):
    try:
        _archive_before_remove(tid, deleted_local_data=True)
        client.remove(tid, delete_local_data=True)
        return jsonify({"ok": True})
    except Exception as e:
        return _err(e)


@app.route("/api/torrent/<int:tid>/unload", methods=["POST"])
@login_required
def api_unload(tid):
    """Park a stopped torrent outside the daemon. Every torrent in the
    session — even fully stopped — inflates the torrent-get payload the
    UI polls every 5s and the daemon's own bookkeeping, so a backlog of
    added-but-not-started torrents slows everything down. Unload removes
    it from Transmission (keeping local data) and saves the magnet +
    download dir server-side so Start can re-add it later."""
    try:
        detail = client.get_torrent_detail(tid)
    except Exception as e:
        return _err(e)
    if not detail:
        return _err("torrent not found", 404)
    if detail.get("status") != 0:
        return _err("only stopped torrents can be unloaded — stop it first", 409)
    h = detail.get("hashString")
    magnet = detail.get("magnetLink")
    if not h or not magnet:
        return _err("torrent has no magnet link yet — try again in a moment", 409)
    try:
        db.record_unloaded_torrent(
            hash=h,
            name=detail.get("name"),
            magnet_link=magnet,
            total_size=detail.get("totalSize"),
            percent_done=detail.get("percentDone"),
            download_dir=detail.get("downloadDir"),
            custom_name=db.get_custom_name(h),
            labels=detail.get("labels") or None,
        )
    except Exception as e:
        return _err(e)
    try:
        client.remove(tid, delete_local_data=False)
    except Exception as e:
        # If the daemon still has the torrent, the parked row would show
        # up as a phantom duplicate — roll it back and report the failure.
        db.delete_unloaded_torrent_by_hash(h)
        return _err(e)
    db.log_event(
        "torrent.unload",
        "info",
        f"Unloaded {detail.get('name') or 'torrent'} (magnet link kept)",
        torrent_name=detail.get("name"),
    )
    return jsonify({"ok": True})


@app.route("/api/unloaded")
@login_required
def api_unloaded():
    return jsonify({"ok": True, "unloaded": db.list_unloaded_torrents()})


@app.route("/api/unloaded/<int:uid>/start", methods=["POST"])
@login_required
def api_unloaded_start(uid):
    entry = db.get_unloaded_torrent(uid)
    if not entry:
        return _err("unloaded torrent not found", 404)
    magnet = (entry.get("magnet_link") or "").strip()
    if not magnet:
        return _err("no magnet link saved for this torrent", 400)
    try:
        result = client.add_magnet(
            magnet, paused=False, download_dir=entry.get("download_dir"),
        )
    except Exception as e:
        return _err(e)
    args = (result or {}).get("arguments", {})
    added = args.get("torrent-added") or args.get("torrent-duplicate") or {}
    new_id = added.get("id")
    if new_id and entry.get("labels"):
        # Labels don't survive torrent-add — reattach them best-effort.
        try:
            client.set_labels(new_id, entry["labels"])
        except Exception:
            pass
    # The custom name keys on the info-hash, so the re-added torrent picks
    # it up automatically; re-save from the archive if it was cleared.
    h = entry.get("hash")
    if h and entry.get("custom_name") and not db.get_custom_name(h):
        try:
            db.set_custom_name(h, entry["custom_name"], default_name=entry.get("name"))
        except Exception:
            pass
    db.delete_unloaded_torrent(uid)
    db.log_event(
        "torrent.load",
        "info",
        f"Loaded and started {entry.get('name') or 'torrent'}",
        torrent_name=entry.get("name"),
    )
    return jsonify({"ok": True})


@app.route("/api/unloaded/<int:uid>/forget", methods=["POST"])
@login_required
def api_unloaded_forget(uid):
    entry = db.get_unloaded_torrent(uid)
    if not entry:
        return _err("unloaded torrent not found", 404)
    db.delete_unloaded_torrent(uid)
    return jsonify({"ok": True})


@app.route("/api/torrent/<int:tid>/name", methods=["POST"])
@login_required
def api_set_name(tid):
    data = request.get_json(silent=True) or {}
    custom_name = (data.get("custom_name") or "").strip()
    try:
        detail = client.get_torrent_detail(tid)
    except Exception as e:
        return _err(e)
    if not detail:
        return _err("torrent not found", 404)
    h = detail.get("hashString")
    if not h:
        return _err("torrent has no info hash yet — try again in a moment", 409)
    if not custom_name:
        db.delete_custom_name(h)
        return jsonify({"ok": True, "custom_name": None})
    if len(custom_name) > 200:
        return _err("custom name is too long (max 200 chars)", 400)
    try:
        db.set_custom_name(h, custom_name, default_name=detail.get("name"))
    except Exception as e:
        return _err(e)
    return jsonify({"ok": True, "custom_name": custom_name})


@app.route("/api/torrent/<int:tid>/name", methods=["DELETE"])
@login_required
def api_clear_name(tid):
    try:
        detail = client.get_torrent_detail(tid)
    except Exception as e:
        return _err(e)
    if not detail:
        return _err("torrent not found", 404)
    h = detail.get("hashString")
    if h:
        db.delete_custom_name(h)
    return jsonify({"ok": True})


@app.route("/api/torrent/<int:tid>/label", methods=["POST"])
@login_required
def api_label(tid):
    data = request.get_json(silent=True) or {}
    labels = data.get("labels", [])
    if not isinstance(labels, list):
        return _err("labels must be a list", 400)
    try:
        client.set_labels(tid, [str(l) for l in labels])
        return jsonify({"ok": True})
    except Exception as e:
        return _err(e)


@app.route("/api/torrent/add/magnet", methods=["POST"])
@login_required
def api_add_magnet():
    data = request.get_json(silent=True) or {}
    magnet = (data.get("magnet") or "").strip()
    if not magnet:
        return _err("magnet is required", 400)
    try:
        client.add_magnet(magnet)
        return jsonify({"ok": True})
    except Exception as e:
        return _err(e)


@app.route("/api/torrent/add/file", methods=["POST"])
@login_required
def api_add_file():
    f = request.files.get("file")
    if not f:
        return _err("file is required", 400)
    try:
        encoded = base64.b64encode(f.read()).decode("ascii")
        client.add_torrent_file(encoded)
        return jsonify({"ok": True})
    except Exception as e:
        return _err(e)


# ---------- media-server copy endpoints ----------

@app.route("/api/media/config", methods=["GET"])
@login_required
def api_media_config_get():
    return jsonify({"ok": True, "config": _read_media_config()})


@app.route("/api/media/config", methods=["POST"])
@login_required
def api_media_config_set():
    data = request.get_json(silent=True) or {}
    host = (data.get("host") or "").strip()
    user = (data.get("user") or "").strip()
    port_raw = data.get("port", 22)
    folders_raw = data.get("folders") or []
    if not host:
        return _err("host is required", 400)
    if not user:
        return _err("user is required", 400)
    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        return _err("port must be an integer", 400)
    if port < 1 or port > 65535:
        return _err("port must be between 1 and 65535", 400)
    # Optional rsync bandwidth cap so copies don't starve Transmission's
    # own traffic (or the Pi's disk) — 0/absent means unlimited.
    try:
        bwlimit_kbps = int(data.get("bwlimit_kbps") or 0)
    except (TypeError, ValueError):
        return _err("bwlimit_kbps must be an integer (KB/s)", 400)
    if bwlimit_kbps < 0:
        return _err("bwlimit_kbps must be zero or positive", 400)
    # Free-space margin copies must leave on the destination disk, as a
    # percent of the disk's total size. 0 disables the margin entirely.
    margin_raw = data.get("space_margin_percent", DEFAULT_SPACE_MARGIN_PERCENT)
    try:
        space_margin_percent = int(margin_raw)
    except (TypeError, ValueError):
        return _err("space_margin_percent must be an integer (percent)", 400)
    if not 0 <= space_margin_percent <= 50:
        return _err("space_margin_percent must be between 0 and 50", 400)
    if not isinstance(folders_raw, list) or not folders_raw:
        return _err("at least one destination folder is required", 400)
    folders = []
    seen_paths = set()
    for i, f in enumerate(folders_raw):
        if not isinstance(f, dict):
            return _err(f"folder #{i+1} is malformed", 400)
        name = (f.get("name") or "").strip()
        path = (f.get("path") or "").strip()
        section_raw = f.get("plex_section_id")
        section_id = ""
        if section_raw is not None and str(section_raw).strip():
            section_id = str(section_raw).strip()
            # Plex section IDs are integers; reject anything else so a typo
            # doesn't turn into a 404 on every refresh.
            if not section_id.isdigit():
                return _err(
                    f"folder '{name}' plex_section_id must be a number",
                    400,
                )
        if not name:
            return _err(f"folder #{i+1} is missing a name", 400)
        if not path:
            return _err(f"folder '{name}' is missing a path", 400)
        if not path.startswith("/"):
            return _err(f"folder '{name}' path must be absolute", 400)
        # Duplicate names are allowed — same-name folders form a fallback
        # group so copies can spill over to a second disk when the first is
        # full. Duplicate *paths* still make no sense.
        if path in seen_paths:
            return _err(f"duplicate folder path '{path}'", 400)
        seen_paths.add(path)
        entry = {"name": name, "path": path}
        if section_id:
            entry["plex_section_id"] = section_id
        folders.append(entry)

    # Optional per-drive free-space margin overrides, keyed by the drive's
    # mountpoint on the media server (Settings renders one field per
    # detected drive). Blank values mean "use the config-wide margin" and
    # are simply dropped.
    dm_raw = data.get("drive_margins") or {}
    if not isinstance(dm_raw, dict):
        return _err("drive_margins must be an object of {mountpoint: percent}", 400)
    drive_margins = {}
    for mp_raw, val in dm_raw.items():
        mp = str(mp_raw).strip()
        if not mp.startswith("/"):
            return _err(f"drive mountpoint '{mp}' must be an absolute path", 400)
        if val is None or str(val).strip() == "":
            continue
        try:
            pct = int(str(val).strip())
        except (TypeError, ValueError):
            return _err(f"margin for drive '{mp}' must be an integer (percent)", 400)
        if not 0 <= pct <= 50:
            return _err(f"margin for drive '{mp}' must be between 0 and 50", 400)
        drive_margins[mp] = pct

    lib_raw = data.get("library_refresh") or {}
    lib_type = (lib_raw.get("type") or "none").strip().lower()
    if lib_type not in ("none", "plex", "jellyfin"):
        return _err("library_refresh.type must be plex, jellyfin, or none", 400)
    library_refresh = {"type": lib_type}
    if lib_type != "none":
        url = (lib_raw.get("url") or "").strip()
        token = (lib_raw.get("token") or "").strip()
        if not url or not (url.startswith("http://") or url.startswith("https://")):
            return _err("library_refresh.url must start with http:// or https://", 400)
        if not token:
            return _err("library_refresh.token is required", 400)
        library_refresh["url"] = url
        library_refresh["token"] = token

    cfg = {"host": host, "user": user, "port": port,
           "folders": folders, "library_refresh": library_refresh,
           "space_margin_percent": space_margin_percent}
    if drive_margins:
        cfg["drive_margins"] = drive_margins
    if bwlimit_kbps:
        cfg["bwlimit_kbps"] = bwlimit_kbps
    try:
        _write_media_config(cfg)
    except OSError as e:
        return _err(f"failed to write media config: {e}")
    return jsonify({"ok": True, "config": cfg})


@app.route("/api/media/drives")
@login_required
def api_media_drives():
    """The distinct drives (filesystems) behind the configured destination
    folders, so Settings can render one free-space-margin field per drive.

    SSHes the media server, dfs every folder path in one call, and dedupes
    by device. Folders are attached to their drive by longest-mountpoint
    prefix match. Returns ok=False with an error string when the server is
    unreachable — the UI then falls back to the saved margins alone.
    """
    cfg = _read_media_config()
    folders = cfg.get("folders") or []
    host = (cfg.get("host") or "").strip()
    user = (cfg.get("user") or "").strip()
    saved = cfg.get("drive_margins") if isinstance(
        cfg.get("drive_margins"), dict) else {}
    if not host or not user or not folders:
        return jsonify({"ok": True, "configured": False, "drives": [],
                        "drive_margins": saved})
    paths = [p for p in ((f.get("path") or "").strip() for f in folders) if p]
    if not paths:
        return jsonify({"ok": True, "configured": False, "drives": [],
                        "drive_margins": saved})
    try:
        rows = _remote_df_multi(user, host, int(cfg.get("port") or 22), paths)
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e), "drives": [],
                        "drive_margins": saved})
    drives = []
    seen_devices = set()
    for row in rows:
        if row["device"] in seen_devices or not row.get("mountpoint"):
            continue
        seen_devices.add(row["device"])
        drives.append({
            "device": row["device"],
            "mountpoint": row["mountpoint"],
            "total": row["total"],
            "free": row["available"],
            "folders": [],
        })
    for f in folders:
        p = (f.get("path") or "").strip()
        if not p:
            continue
        best = None
        for d in drives:
            mp = d["mountpoint"]
            if p == mp or p.startswith(mp.rstrip("/") + "/"):
                if best is None or len(mp) > len(best["mountpoint"]):
                    best = d
        if best is not None:
            best["folders"].append(f.get("name") or p)
    return jsonify({"ok": True, "configured": True, "drives": drives,
                    "drive_margins": saved})


@app.route("/api/media/test", methods=["POST"])
@login_required
def api_media_test():
    # Prefer values supplied in the request body so you can test connection
    # before saving the form. Fall back to whatever is persisted.
    data = request.get_json(silent=True) or {}
    cfg = _read_media_config()
    host = (data.get("host") or cfg.get("host") or "").strip()
    user = (data.get("user") or cfg.get("user") or "").strip()
    port_raw = data.get("port", cfg.get("port") or 22)
    if not host or not user:
        return _err("host and user are required", 400)
    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        return _err("port must be an integer", 400)
    if port < 1 or port > 65535:
        return _err("port must be between 1 and 65535", 400)
    try:
        r = subprocess.run(
            ["ssh",
             "-o", "BatchMode=yes",
             "-o", "ConnectTimeout=10",
             "-o", "StrictHostKeyChecking=accept-new",
             "-p", str(port), f"{user}@{host}", "true"],
            capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired:
        hint = _tailscale_auth_hint()
        return jsonify({"ok": False, "error": "ssh connection timed out",
                        "tailscale_auth_url": hint})
    except FileNotFoundError:
        return _err("ssh client is not installed on this host", 500)
    if r.returncode == 0:
        return jsonify({"ok": True})
    raw = (r.stderr or r.stdout or "")
    msg = raw.strip()[:500] or f"ssh exited with code {r.returncode}"
    hint = _tailscale_auth_hint(raw)
    return jsonify({"ok": False, "error": msg, "tailscale_auth_url": hint})


@app.route("/api/torrent/<int:tid>/seasons")
@login_required
def api_torrent_seasons(tid):
    """Inspect a torrent's files and report detected seasons.

    The modal uses this to decide between the single-season UI and the
    multi-season checklist. Returns `{seasons: [...], unclassified: [...]}`.
    Each season entry has `season`, `file_count`, `total_bytes`, and `files`
    (paths relative to the torrent root).
    """
    try:
        source = client.get_torrent_location(tid)
    except Exception as e:
        return _err(e)
    if not source or not os.path.exists(source):
        return _err("torrent files not found on disk", 404)
    if not os.path.isdir(source):
        return jsonify({"is_dir": False, "seasons": [], "unclassified": []})

    videos = _collect_video_files(source)
    groups, unclassified = _group_videos_by_season(videos, source)

    def _safe_size(p):
        try:
            return os.path.getsize(p)
        except OSError:
            return 0

    seasons_out = []
    for s in sorted(groups.keys()):
        files = groups[s]
        seasons_out.append({
            "season": s,
            "file_count": len(files),
            "total_bytes": sum(_safe_size(f) for f in files),
            "files": [os.path.relpath(f, source) for f in files],
        })
    unclassified_out = [
        {"path": os.path.relpath(f, source), "size": _safe_size(f)}
        for f in unclassified
    ]
    return jsonify({
        "is_dir": True,
        "total_videos": len(videos),
        "seasons": seasons_out,
        "unclassified": unclassified_out,
    })


@app.route("/api/torrent/<int:tid>/copy", methods=["POST"])
@login_required
def api_copy_start(tid):
    cfg = _read_media_config()
    folders = cfg.get("folders") or []
    if not cfg.get("host") or not cfg.get("user") or not folders:
        return _err("media destination is not configured", 400)

    data = request.get_json(silent=True) or {}
    folder_name = (data.get("folder") or "").strip()
    if not folder_name:
        return _err("folder is required", 400)
    # Same-name folders form a fallback group. `candidates` is the ordered
    # list of destinations to try; `folder` is the primary (used for
    # user-facing labels until _run_copy picks the actual disk).
    candidates = [f for f in folders if f.get("name") == folder_name]
    if not candidates:
        return _err(f"unknown folder '{folder_name}'", 400)
    folder = candidates[0]

    media_type = (data.get("media_type") or "movie").strip()
    if media_type not in ("movie", "show"):
        return _err("media_type must be 'movie' or 'show'", 400)

    multi_seasons_raw = data.get("seasons") if media_type == "show" else None
    multi_season_mode = (
        media_type == "show"
        and isinstance(multi_seasons_raw, list)
        and len(multi_seasons_raw) > 0
    )

    try:
        if media_type == "show":
            show_name = _sanitize_show_name(data.get("show_name"))
            year = _sanitize_year(data.get("year"))
            if multi_season_mode:
                selected_seasons = []
                seen = set()
                for s in multi_seasons_raw:
                    label = _sanitize_season(s)
                    if label in seen:
                        continue
                    seen.add(label)
                    selected_seasons.append(label)
                # In multi-season mode subfolder is just the show root;
                # _run_copy appends `/Season NN` per detected group.
                subfolder = f"{show_name} ({year})"
            else:
                season = _sanitize_season(data.get("season"))
                subfolder = f"{show_name} ({year})/Season {season}"
            rename = ""
        else:
            subfolder = _sanitize_subfolder(data.get("subfolder"))
            rename = _sanitize_rename(data.get("rename"))
    except ValueError as e:
        return _err(str(e), 400)
    delete_after = bool(data.get("delete_after"))

    # Reserve the slot before any further work. The old check-only version
    # left a window between this check and the worker thread registering
    # itself in _run_copy, so two quick "Copy" clicks could launch two
    # rsyncs for the same torrent. Every early return below must release
    # the reservation — the finally block handles that.
    with _active_copies_lock:
        if tid in _active_copies:
            return _err("a copy is already running for this torrent", 409)
        _active_copies[tid] = {"cancel": threading.Event(), "proc": None}

    worker_started = False
    try:
        try:
            source = client.get_torrent_location(tid)
        except Exception as e:
            return _err(e)
        if not source or not os.path.exists(source):
            return _err("torrent files not found on disk", 404)

        season_groups = None
        if media_type == "show":
            if not os.path.isdir(source):
                return _err("show mode expects a torrent that is a folder", 400)
            videos = _collect_video_files(source)
            if not videos:
                return _err("no video files found in this torrent", 400)
            if multi_season_mode:
                groups, _unclassified = _group_videos_by_season(videos, source)
                season_groups = []
                for label in selected_seasons:
                    files = groups.get(label)
                    if not files:
                        return _err(
                            f"no videos detected for Season {label} in this torrent",
                            400,
                        )
                    season_groups.append({"season": label, "sources": files})
                # `sources` is unused in multi-season mode but _run_copy still
                # accepts it positionally — pass the flat list for safety.
                sources = videos
            else:
                sources = videos
        else:
            sources = [source]

        # Source-size estimate (du -sb) and remote `df` used to run here
        # synchronously, blocking the gunicorn worker thread for up to ~80s on a
        # large torrent over a slow tunnel. Moved into _run_copy so the POST
        # returns immediately and the UI's existing copy-progress polling
        # surfaces an "insufficient space" error the same way it surfaces an
        # rsync failure.
        torrent_name = _torrent_name(tid)
        started_details = {
            "folder": folder["name"],
            "media_type": media_type,
        }
        if season_groups:
            started_details["seasons"] = [g["season"] for g in season_groups]
        db.log_event(
            "copy.started",
            "info",
            f"Copy started → {cfg['host']}:{folder['path']}",
            torrent_id=tid, torrent_name=torrent_name,
            details=started_details,
        )

        threading.Thread(
            target=_run_copy,
            args=(tid, sources, folder["path"], subfolder, rename,
                  cfg["host"], cfg["user"], int(cfg.get("port") or 22), delete_after,
                  media_type),
            kwargs={
                "folder": folder,
                "cfg": cfg,
                "torrent_name": torrent_name,
                "season_groups": season_groups,
                "candidates": candidates,
            },
            daemon=True,
        ).start()
        worker_started = True
        return jsonify({"ok": True})
    finally:
        if not worker_started:
            with _active_copies_lock:
                _active_copies.pop(tid, None)


@app.route("/api/torrent/<int:tid>/copy/stop", methods=["POST"])
@login_required
def api_copy_stop(tid):
    with _active_copies_lock:
        info = _active_copies.get(tid)
        if info is None:
            return _err("no active copy for this torrent", 404)
        info["cancel"].set()
        proc = info.get("proc")
    if proc is not None:
        try:
            proc.terminate()
        except Exception:
            pass
    return jsonify({"ok": True, "stopping": True})


@app.route("/api/torrent/<int:tid>/copy/status")
@login_required
def api_copy_status(tid):
    state = load_copy_state()
    entry = state.get(str(tid)) or {"id": tid, "status": "idle"}
    return jsonify(entry)


@app.route("/api/copy/states")
@login_required
def api_copy_states():
    return jsonify(load_copy_state())


@app.route("/api/torrent/<int:tid>/copy/check")
@login_required
def api_copy_check(tid):
    """Check whether this torrent's recorded copy still exists on the
    currently-configured media server.

    Uses the destination path saved in copy_state (from when it was copied)
    but probes the *current* config's host/user/port — so after a server
    switch this reports what's actually present on the new box, not the old
    one. Read-only: state is only changed later via /copy/set-copied.

    Returns state = present | missing | unknown, where unknown means either
    no destination was on record or the server couldn't be reached.
    """
    cfg = _read_media_config()
    host = (cfg.get("host") or "").strip()
    user = (cfg.get("user") or "").strip()
    if not host or not user:
        return _err("media destination is not configured", 400)
    port = int(cfg.get("port") or 22)
    roots = [p for p in ((f.get("path") or "").strip()
                         for f in (cfg.get("folders") or [])) if p]

    try:
        detail = client.get_torrent_detail(tid)
    except Exception:
        detail = None

    entry = load_copy_state().get(str(tid)) or {}
    # Don't probe paths recorded for a different torrent. Ids are reassigned
    # on daemon restart, so an entry whose hash doesn't match the torrent now
    # holding the id describes something else entirely — its dest_path would
    # send the search after the wrong media. _gc_state_for_live_torrents
    # clears these on the next /api/torrents poll; ignore it until then.
    recorded_hash = entry.get("hash")
    live_hash = (detail or {}).get("hashString") or None
    if recorded_hash and live_hash and recorded_hash != live_hash:
        entry = {}
    dest_path = entry.get("dest_path")
    # Real per-item paths, recorded at copy time for new copies. Old copies
    # (pre-this-feature, or copied to the previous server) won't have them, so
    # we fall back to searching the configured folders by name.
    exact_paths = entry.get("dest_targets") or []

    # Candidate on-disk names to search for under the library folders:
    #  - the torrent's name (what a no-rename copy lands as),
    #  - the recorded destination leaf (what a renamed copy lands as),
    #  - that leaf with a trailing " [Seasons 1, 2]" label stripped (the show
    #    folder for a multi-season copy, whose dest_path is only a label).
    names = set()
    if detail and detail.get("name"):
        names.add(detail["name"])
    if dest_path:
        leaf = os.path.basename(dest_path.rstrip("/"))
        if leaf:
            names.add(leaf)
            stripped = re.sub(r"\s*\[Seasons?\b[^\]]*\]\s*$", "", leaf).strip()
            if stripped:
                names.add(stripped)

    if not exact_paths and not names:
        return jsonify({
            "ok": True, "id": tid, "state": "unknown",
            "reason": "nothing on record to check", "dest_path": dest_path,
            "host": host,
        })
    if not exact_paths and detail and (detail.get("percentDone") or 0) < 1:
        # Nothing exact on record and the download isn't finished, so the only
        # thing left is the fuzzy name search — which would happily match a
        # folder left by an earlier copy of the same show and report a torrent
        # that has never been sent as "present". Refuse to guess.
        return jsonify({
            "ok": True, "id": tid, "state": "unknown",
            "reason": "torrent is still downloading — nothing has been copied yet",
            "dest_path": dest_path, "host": host,
        })
    if not exact_paths and not roots:
        return jsonify({
            "ok": True, "id": tid, "state": "unknown",
            "reason": "no library folders configured in Settings",
            "dest_path": dest_path, "host": host,
        })

    name_list = sorted(names)
    present, debug = _remote_check_presence(
        user, host, port, exact_paths, roots, name_list)
    # Diagnostics echoed back so a wrong verdict can be traced (what names /
    # folders were searched, and any stderr the remote find emitted).
    diag = {"names": name_list, "roots": roots,
            "exact": exact_paths, "detail": debug or ""}
    if present is None:
        db.log_event(
            "copy.check.unknown", "warn",
            f"Copied-check couldn't verify torrent {tid}: {debug}",
            torrent_id=tid, details=diag,
        )
        return jsonify({
            "ok": True, "id": tid, "state": "unknown",
            "reason": "media server could not be reached or gave no answer",
            "dest_path": dest_path, "host": host, "diag": diag,
        })
    if debug:
        # A verdict came back but find also wrote to stderr (e.g. a configured
        # folder doesn't exist on this server) — worth recording.
        db.log_event(
            "copy.check.stderr", "info",
            f"Copied-check stderr for torrent {tid}: {debug}",
            torrent_id=tid, details=diag,
        )
    return jsonify({
        "ok": True, "id": tid,
        "state": "present" if present else "missing",
        "dest_path": dest_path, "host": host, "diag": diag,
    })


@app.route("/api/torrent/<int:tid>/copy/set-copied", methods=["POST"])
@login_required
def api_copy_set_copied(tid):
    """Reconcile a torrent's stored 'copied' state with reality after a check.

    body: {"copied": true|false}. Adds/removes the 'Copied' label and flips
    copy_state so the card settles on the right lifecycle state — used when
    the user clicks Okay on a check result that disagrees with what we had
    on record (e.g. a copy that didn't survive a media-server switch).
    """
    data = request.get_json(silent=True) or {}
    copied = bool(data.get("copied"))
    try:
        detail = client.get_torrent_detail(tid)
        existing = list(detail.get("labels") or []) if detail else []
    except Exception as e:
        return _err(e)
    live_hash = (detail or {}).get("hashString") or None

    if copied:
        if COPIED_LABEL not in existing:
            try:
                client.set_labels(tid, existing + [COPIED_LABEL])
            except Exception as e:
                return _err(e)
        update_copy_entry(tid, status="done", hash=live_hash,
                          finished_at=_now_iso(), error_message=None)
    else:
        if COPIED_LABEL in existing:
            try:
                client.set_labels(
                    tid, [l for l in existing if l != COPIED_LABEL])
            except Exception as e:
                return _err(e)
        # Reset to idle (keeping dest_path for reference) so the card falls
        # back to "ready to copy" instead of reading as done.
        update_copy_entry(tid, status="idle", hash=live_hash, progress_pct=0,
                          error_message=None)
    return jsonify({"ok": True, "id": tid, "copied": copied})


@app.route("/api/torrent/<int:tid>/copy/preflight")
@login_required
def api_copy_preflight(tid):
    """Dry-run of _run_copy's disk selection so the copy modal can show
    which drive the media will land on before the user hits Copy.

    Mirrors the worker's rules: size the source, df each same-name
    candidate in order, pick the first that fits with the safety margin.
    Never fails the modal for a df error — unreachable disks are reported
    per-entry so the UI can label them.
    """
    cfg = _read_media_config()
    folders = cfg.get("folders") or []
    if not cfg.get("host") or not cfg.get("user") or not folders:
        return _err("media destination is not configured", 400)
    folder_name = (request.args.get("folder") or "").strip()
    candidates = [f for f in folders if f.get("name") == folder_name]
    if not candidates:
        return _err(f"unknown folder '{folder_name}'", 400)
    media_type = (request.args.get("media_type") or "movie").strip()

    need = 0
    try:
        source = client.get_torrent_location(tid)
        if source and os.path.exists(source):
            if media_type == "show" and os.path.isdir(source):
                need = _estimate_source_size(
                    "show", None, video_files=_collect_video_files(source),
                )
            else:
                need = _estimate_source_size("movie", source)
    except Exception:
        need = 0

    user = cfg["user"]
    host = cfg["host"]
    port = int(cfg.get("port") or 22)
    disks = []
    for cand in candidates:
        # Until df tells us which drive the path lives on, report the
        # config-wide margin; a per-drive override replaces it below.
        entry = {"path": cand["path"], "total": None, "free": None,
                 "fits": None, "error": None,
                 "margin_fraction": _space_margin_fraction(cfg)}
        try:
            disk_total, _, free, mount = _remote_df(
                user, host, port, cand["path"])
            cand_margin = _space_margin_fraction(cfg, mount)
            entry["margin_fraction"] = cand_margin
            entry["mountpoint"] = mount
            entry["total"] = disk_total
            entry["free"] = free
            entry["fits"] = _space_shortfall(need, disk_total, free,
                                             cand_margin) == 0
        except RuntimeError as e:
            entry["error"] = str(e)
        disks.append(entry)

    # Which disk would the worker actually target?
    chosen = None
    if need <= 0 or len(candidates) == 1:
        # No size estimate (worker skips selection) or nothing to select
        # between — the copy targets the primary. With one candidate and a
        # confirmed shortfall the worker's preflight would reject it.
        if not (len(candidates) == 1 and disks[0]["fits"] is False):
            chosen = disks[0]["path"]
    else:
        for d in disks:
            if d["fits"]:
                chosen = d["path"]
                break
        if chosen is None and all(d["free"] is None for d in disks):
            # None reachable — worker falls through to the primary and
            # lets rsync surface the real error.
            chosen = disks[0]["path"]

    return jsonify({
        "ok": True,
        "need": need,
        # Config-wide value; per-drive overrides ride on each disks[] entry.
        "margin_fraction": _space_margin_fraction(cfg),
        "disks": disks,
        "chosen_path": chosen,
    })


if __name__ == "__main__":
    # Dev runner. Production uses gunicorn via systemd (see systemd/). Bind
    # to localhost so a developer running the file directly doesn't
    # accidentally expose an unauthenticated dev server on the LAN.
    app.run(host="127.0.0.1", port=5000)
