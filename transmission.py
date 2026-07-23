import json
import logging
import os
import threading
import time

import requests
from requests.auth import HTTPBasicAuth

import config

# Slow/failed RPCs are logged at WARNING so they reach gunicorn's error log
# (journald) with no logging config needed — the server-side record to
# correlate with the UI's "Stale" indicator.
_SLOW_RPC_SEC = 2.0
_log = logging.getLogger("transmission.rpc")

# Transmission status codes:
# 0 = stopped
# 1 = check pending
# 2 = checking
# 3 = download pending
# 4 = downloading
# 5 = seed pending
# 6 = seeding

TORRENT_FIELDS = [
    "id",
    "name",
    "hashString",
    "status",
    "percentDone",
    # Only meaningful while status == 2 (checking): the fraction of the
    # torrent re-read from disk so far. Distinct from percentDone, which
    # during a recheck counts pieces that *passed*. Lives in the list
    # query (not just DETAIL_FIELDS) so the collapsed card can draw it.
    "recheckProgress",
    "rateDownload",
    "rateUpload",
    "uploadRatio",
    "eta",
    "totalSize",
    "labels",
    "downloadDir",
    "addedDate",
    "error",
    "errorString",
    "peersConnected",
    "peersSendingToUs",
    "peersGettingFromUs",
    "doneDate",
]

# Detail-view fields for the per-torrent expand panel. Kept separate from
# the list query so the main poll stays cheap — `peers` + `trackerStats`
# are big per-torrent payloads we don't want to ship for every row on
# every poll.
DETAIL_FIELDS = TORRENT_FIELDS + [
    "activityDate",
    "startDate",
    "secondsDownloading",
    "secondsSeeding",
    "downloadedEver",
    "uploadedEver",
    "corruptEver",
    "haveValid",
    "haveUnchecked",
    "leftUntilDone",
    "sizeWhenDone",
    "pieceCount",
    "pieceSize",
    "peersConnected",
    "peersGettingFromUs",
    "peersSendingToUs",
    "peers",
    "trackerStats",
    "seedRatioLimit",
    "seedRatioMode",
    "queuePosition",
    "isPrivate",
    "comment",
    "creator",
    "dateCreated",
    "magnetLink",
]


class TransmissionError(Exception):
    """Raised when the daemon is unreachable, rejects auth, or returns a
    non-success RPC result. Message is safe to surface to the UI."""
    pass


class TransmissionClient:
    def __init__(self, host=None, port=None, user=None, password=None):
        self.host = host or config.TR_HOST
        self.port = port or config.TR_PORT
        self.user = user or config.TR_USER
        self.password = password or config.TR_PASS
        self.url = f"http://{self.host}:{self.port}/transmission/rpc"
        self.session_id = ""
        # Serialises session-id refreshes. Gunicorn threads share this
        # client; when the daemon rotates the CSRF id, several in-flight
        # requests all get 409s at once and used to clobber session_id
        # with whatever header each saw, causing extra 409 round-trips.
        self._sid_lock = threading.Lock()
        # Persistent HTTP session keeps the TCP connection to transmission-
        # daemon warm across polls. The dashboard polls every 5s and was
        # paying a fresh connect + auth handshake per call.
        self._http = requests.Session()
        self._http.auth = HTTPBasicAuth(self.user, self.password)
        # (mtime, value) caches for the settings.json bind-address reads so
        # repeated tunnel checks don't re-parse the file every call. Separate
        # slots for v4 and v6 since the leak check reads both.
        self._settings_bind_cache = (None, None)
        self._settings_bind6_cache = (None, None)

    # (connect, read) timeout. The old flat 30s meant a bogged-down daemon
    # could pin a gunicorn thread for half a minute per poll; with only 8
    # threads, piled-up polls stalled the whole dashboard. Failing fast at
    # 10s keeps threads available — the UI just shows one stale tick.
    _TIMEOUT = (3.05, 10)

    def request(self, method, arguments=None):
        start = time.monotonic()
        try:
            data = self._do_request(method, arguments)
        except TransmissionError as e:
            _log.warning("RPC %s failed after %.2fs: %s",
                         method, time.monotonic() - start, e)
            raise
        elapsed = time.monotonic() - start
        if elapsed >= _SLOW_RPC_SEC:
            _log.warning("RPC %s slow: %.2fs", method, elapsed)
        return data

    def _do_request(self, method, arguments=None):
        payload = {"method": method, "arguments": arguments or {}}

        try:
            response = self._http.post(
                self.url,
                json=payload,
                headers={"X-Transmission-Session-Id": self.session_id},
                timeout=self._TIMEOUT,
            )

            if response.status_code == 409:
                # Adopt the fresh id under the lock; another thread may have
                # already stored a newer one, in which case keep it rather
                # than racing each other back and forth.
                fresh = response.headers.get("X-Transmission-Session-Id", "")
                with self._sid_lock:
                    if fresh:
                        self.session_id = fresh
                    sid = self.session_id
                response = self._http.post(
                    self.url,
                    json=payload,
                    headers={"X-Transmission-Session-Id": sid},
                    timeout=self._TIMEOUT,
                )
        except requests.exceptions.ConnectionError as e:
            raise TransmissionError(
                f"Cannot reach Transmission RPC at {self.url}: {e}. "
                "Check the daemon is running, the port is open, and TR_HOST/TR_PORT are correct."
            ) from e
        except requests.exceptions.Timeout as e:
            raise TransmissionError(
                f"Timed out contacting Transmission RPC at {self.url}"
            ) from e

        if response.status_code == 401:
            raise TransmissionError(
                f"Transmission RPC rejected credentials (401) for user '{self.user}'. "
                "Check TR_USER / TR_PASS in .env match the new server's settings.json."
            )
        if response.status_code == 403:
            raise TransmissionError(
                "Transmission RPC denied (403). Check rpc-host-whitelist / "
                "rpc-whitelist in settings.json on the daemon."
            )

        if not response.ok:
            raise TransmissionError(
                f"Transmission RPC HTTP {response.status_code}: "
                f"{response.text[:300].strip() or response.reason}"
            )

        try:
            data = response.json()
        except ValueError as e:
            raise TransmissionError(
                f"Transmission RPC returned non-JSON: {response.text[:200].strip()}"
            ) from e

        # Transmission returns HTTP 200 even when the operation fails;
        # the JSON "result" field is the real status. e.g. "no such torrent",
        # "invalid argument", etc.
        result = data.get("result")
        if result and result != "success":
            raise TransmissionError(f"Transmission RPC error: {result}")
        return data

    def get_torrents(self):
        result = self.request("torrent-get", {"fields": TORRENT_FIELDS})
        return result.get("arguments", {}).get("torrents", [])

    def get_stats_torrents(self):
        """Slim per-torrent payload for aggregate stats. Kept off the main
        poll because secondsSeeding / uploadedEver / etc. aren't needed for
        the list view."""
        result = self.request("torrent-get", {"fields": [
            "id", "hashString", "name", "status", "totalSize", "uploadedEver",
            "downloadedEver", "secondsSeeding", "secondsDownloading",
            "uploadRatio", "peersConnected", "addedDate",
        ]})
        return result.get("arguments", {}).get("torrents", [])

    def get_torrents_export(self):
        """Minimal per-torrent payload for the CSV export: identity, state,
        completeness, and magnet link. Kept off the main poll — magnetLink
        isn't in TORRENT_FIELDS because the list view never needs it."""
        result = self.request("torrent-get", {"fields": [
            "id", "name", "hashString", "status", "percentDone", "magnetLink",
        ]})
        return result.get("arguments", {}).get("torrents", [])

    def get_session_stats(self):
        """`session-stats` gives cumulative + current totals straight from the
        daemon — avoids re-summing per-torrent payloads for headline numbers."""
        result = self.request("session-stats")
        return result.get("arguments", {})

    def get_torrent_detail(self, id):
        result = self.request(
            "torrent-get",
            {"ids": [id], "fields": DETAIL_FIELDS},
        )
        torrents = result.get("arguments", {}).get("torrents", [])
        return torrents[0] if torrents else None

    def get_torrent_details(self, ids):
        """Multi-id detail fetch. Transmission's torrent-get RPC already
        accepts ids: [...], so the entire expand-panel polling loop on the
        UI side can ride on one round-trip instead of N."""
        if not ids:
            return []
        result = self.request(
            "torrent-get",
            {"ids": list(ids), "fields": DETAIL_FIELDS},
        )
        return result.get("arguments", {}).get("torrents", [])

    def get_torrent_files(self, ids):
        """Per-file names, declared sizes and completion for the given ids.

        Only used by the bitrate probe, which needs a file's *final* size
        (`length`) rather than what's landed so far — Transmission
        preallocates, so on-disk size says nothing about progress.
        """
        if not ids:
            return []
        result = self.request("torrent-get", {
            "ids": list(ids),
            "fields": ["id", "hashString", "downloadDir", "name", "files"],
        })
        return result.get("arguments", {}).get("torrents", [])

    def get_incomplete_dir(self):
        """Return the daemon's incomplete-dir, or None when it's disabled.

        In-progress files live here rather than under the torrent's
        downloadDir, so anything reading bytes off disk mid-download has to
        look in both places.
        """
        result = self.request("session-get", {
            "fields": ["incomplete-dir", "incomplete-dir-enabled"],
        })
        args = result.get("arguments", {})
        if not args.get("incomplete-dir-enabled"):
            return None
        return (args.get("incomplete-dir") or "").strip() or None

    def start(self, id):
        return self.request("torrent-start", {"ids": [id]})

    def stop(self, id):
        return self.request("torrent-stop", {"ids": [id]})

    def remove(self, id, delete_local_data=False):
        return self.request(
            "torrent-remove",
            {"ids": [id], "delete-local-data": bool(delete_local_data)},
        )

    def verify(self, id):
        return self.request("torrent-verify", {"ids": [id]})

    def set_labels(self, id, labels):
        return self.request(
            "torrent-set",
            {"ids": [id], "labels": list(labels)},
        )

    def add_magnet(self, magnet, paused=True, download_dir=None):
        args = {"filename": magnet, "paused": bool(paused)}
        if download_dir:
            args["download-dir"] = download_dir
        return self.request("torrent-add", args)

    def add_torrent_file(self, base64_metainfo):
        return self.request("torrent-add", {"metainfo": base64_metainfo})

    def get_session(self):
        result = self.request("session-get", {"fields": ["download-dir"]})
        return result.get("arguments", {})

    def get_session_bind_address(self):
        """Return the daemon's bind-address-ipv4, or None if absent.

        Transmission 4.x dropped this field from the session-get RPC
        response (it's also no longer settable via session-set), so when
        the RPC omits it we fall back to parsing settings.json — which is
        now the actual source of truth for this option.
        """
        return self._read_bind_setting("bind-address-ipv4", "_settings_bind_cache")

    def get_session_bind_address_ipv6(self):
        """Return the daemon's bind-address-ipv6, or None if absent.

        Same story as the IPv4 read (RPC dropped it on 4.x, settings.json is
        the source of truth). The tunnel leak check needs this because binding
        only IPv4 leaves IPv6 BitTorrent traffic free to egress the bare link.
        """
        return self._read_bind_setting("bind-address-ipv6", "_settings_bind6_cache")

    def get_tracker_stats(self, id):
        """One torrent's tracker state. The tracker leak test reads the
        announce-result strings here: IP-echo trackers (TorGuard's Check My
        Torrent IP, ipleak.net) report the IP they saw for us inside that
        message, which is the only end-to-end view of what a real tracker
        sees. Returns the torrent dict or None if it's gone."""
        result = self.request(
            "torrent-get",
            {"ids": [id],
             "fields": ["id", "hashString", "status", "labels", "trackerStats"]},
        )
        torrents = result.get("arguments", {}).get("torrents", [])
        return torrents[0] if torrents else None

    def find_torrents_by_label(self, label):
        """All torrents carrying `label` — used to clean up leak-test magnets
        left behind by an interrupted run."""
        result = self.request("torrent-get", {"fields": ["id", "labels"]})
        torrents = result.get("arguments", {}).get("torrents", [])
        return [t for t in torrents if label in (t.get("labels") or [])]

    def get_peer_port(self):
        """The daemon's live peer port. Unlike the bind-address fields this is
        still in the session-get RPC on 4.x, so it always reflects the running
        process (including a random port from peer-port-random-on-start). The
        tunnel check uses it to find the daemon's sockets in /proc/net."""
        result = self.request("session-get", {"fields": ["peer-port"]})
        return result.get("arguments", {}).get("peer-port")

    def _read_bind_setting(self, field, cache_attr):
        """Shared reader for the bind-address-ipv4/ipv6 options: try the RPC
        first, then fall back to an mtime-cached parse of settings.json. Returns
        the value or None when absent/unreadable."""
        try:
            result = self.request("session-get", {"fields": [field]})
            v = result.get("arguments", {}).get(field)
            if v:
                return v
        except Exception:
            pass
        try:
            mtime = os.path.getmtime(config.TR_SETTINGS_FILE)
        except OSError:
            return None
        cached_mtime, cached_val = getattr(self, cache_attr)
        if cached_mtime == mtime:
            return cached_val
        try:
            with open(config.TR_SETTINGS_FILE, "r") as f:
                settings = json.load(f)
            v = settings.get(field) or None
        except (OSError, ValueError):
            return None
        setattr(self, cache_attr, (mtime, v))
        return v

    def set_download_dir(self, path):
        return self.request("session-set", {"download-dir": path})

    def set_location(self, id, location, move=False):
        return self.request(
            "torrent-set-location",
            {"ids": [id], "location": location, "move": bool(move)},
        )

    def get_torrent_location(self, id):
        result = self.request(
            "torrent-get",
            {"ids": [id], "fields": ["downloadDir", "name"]},
        )
        torrents = result.get("arguments", {}).get("torrents", [])
        if not torrents:
            return None
        t = torrents[0]
        download_dir = t.get("downloadDir") or ""
        name = t.get("name") or ""
        return os.path.join(download_dir, name) if name else download_dir

