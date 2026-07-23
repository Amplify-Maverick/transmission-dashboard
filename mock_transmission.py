import time

import config

_NOW = int(time.time())

def _tracker(tier, host, last_ok=True, seeders=120, leechers=15, downloads=4500):
    return {
        "id": tier,
        "tier": tier,
        "announce": f"https://{host}/announce",
        "host": host,
        "scrape": f"https://{host}/scrape",
        "announceState": 1,
        "isBackup": False,
        "lastAnnounceSucceeded": last_ok,
        "lastAnnounceResult": "" if last_ok else "Connection timed out",
        "lastAnnounceTime": _NOW - 240,
        "nextAnnounceTime": _NOW + 1500,
        "seederCount": seeders,
        "leecherCount": leechers,
        "downloadCount": downloads,
    }


def _peer(addr, client, dl=0, ul=0, progress=1.0, encrypted=True, incoming=False):
    return {
        "address": addr,
        "port": 51413,
        "clientName": client,
        "flagStr": "uE" if encrypted else "u",
        "isEncrypted": encrypted,
        "isIncoming": incoming,
        "isDownloadingFrom": dl > 0,
        "isUploadingTo": ul > 0,
        "isUTP": True,
        "progress": progress,
        "rateToClient": dl,
        "rateToPeer": ul,
    }


_FAKE_TORRENTS = [
    {
        "id": 1,
        "name": "ubuntu-24.04-desktop-amd64.iso",
        "hashString": "abcdef1234567890abcdef1234567890abcdef12",
        "status": 6,
        "percentDone": 1.0,
        "rateDownload": 0,
        "rateUpload": 124000,
        "uploadRatio": 2.34,
        "eta": -1,
        "totalSize": 5_400_000_000,
        "labels": ["linux", "iso"],
        "downloadDir": "/var/lib/transmission-daemon/downloads",
        "addedDate": _NOW - 86400 * 7,
        "error": 0,
        "errorString": "",
        "activityDate": _NOW - 5,
        "doneDate": _NOW - 86400 * 6,
        "startDate": _NOW - 86400 * 7,
        "secondsDownloading": 3600 * 4,
        "secondsSeeding": 86400 * 6,
        "downloadedEver": 5_400_000_000,
        "uploadedEver": 12_636_000_000,
        "corruptEver": 0,
        "haveValid": 5_400_000_000,
        "haveUnchecked": 0,
        "leftUntilDone": 0,
        "sizeWhenDone": 5_400_000_000,
        "pieceCount": 2576,
        "pieceSize": 2_097_152,
        "peersConnected": 18,
        "peersGettingFromUs": 6,
        "peersSendingToUs": 0,
        "seedRatioLimit": 2.0,
        "seedRatioMode": 0,
        "queuePosition": 0,
        "isPrivate": False,
        "comment": "Ubuntu Desktop 24.04 LTS",
        "creator": "mktorrent 1.1",
        "dateCreated": _NOW - 86400 * 30,
        "magnetLink": "magnet:?xt=urn:btih:abcdef1234567890",
        "peers": [
            _peer("203.0.113.10",  "qBittorrent 4.6.5", ul=44_000),
            _peer("198.51.100.22", "Transmission 4.0", ul=31_000),
            _peer("203.0.113.55",  "Deluge 2.1.1",     ul=22_000, incoming=True),
            _peer("192.0.2.7",     "libtorrent 2.0",   ul=15_000),
            _peer("198.51.100.4",  "BiglyBT 3.5",      ul=12_000),
            _peer("203.0.113.91",  "qBittorrent 5.0",  ul=0, progress=0.92),
        ],
        "trackerStats": [
            _tracker(0, "tracker.ubuntu.com",       seeders=842, leechers=27, downloads=18230),
            _tracker(1, "ipv6.torrent.ubuntu.com",  seeders=120, leechers=8,  downloads=4400),
        ],
    },
    {
        "id": 2,
        "name": "debian-12.5.0-amd64-netinst.iso",
        "hashString": "fedcba0987654321fedcba0987654321fedcba09",
        "status": 4,
        "percentDone": 0.42,
        "rateDownload": 2_800_000,
        "rateUpload": 50_000,
        "uploadRatio": 0.12,
        "eta": 1800,
        "totalSize": 700_000_000,
        "labels": ["linux"],
        "downloadDir": "/var/lib/transmission-daemon/downloads",
        "addedDate": _NOW - 3600,
        "error": 0,
        "errorString": "",
        "activityDate": _NOW - 2,
        "doneDate": 0,
        "startDate": _NOW - 3600,
        "secondsDownloading": 3600,
        "secondsSeeding": 0,
        "downloadedEver": 294_000_000,
        "uploadedEver": 35_280_000,
        "corruptEver": 0,
        "haveValid": 294_000_000,
        "haveUnchecked": 0,
        "leftUntilDone": 406_000_000,
        "sizeWhenDone": 700_000_000,
        "pieceCount": 1336,
        "pieceSize": 524_288,
        "peersConnected": 24,
        "peersGettingFromUs": 3,
        "peersSendingToUs": 11,
        "seedRatioLimit": 2.0,
        "seedRatioMode": 0,
        "queuePosition": 1,
        "isPrivate": False,
        "comment": "Debian 12.5 netinst",
        "creator": "mktorrent 1.1",
        "dateCreated": _NOW - 86400 * 14,
        "magnetLink": "magnet:?xt=urn:btih:fedcba0987654321",
        "peers": [
            _peer("198.51.100.50", "Transmission 4.0", dl=1_200_000, ul=14_000, progress=0.97),
            _peer("203.0.113.71",  "qBittorrent 5.0",  dl=820_000,   ul=11_000, progress=0.84, incoming=True),
            _peer("192.0.2.18",    "Deluge 2.1.1",     dl=410_000,   ul=9_000,  progress=0.61),
            _peer("198.51.100.66", "libtorrent 2.0",   dl=190_000,   ul=8_000,  progress=0.55),
            _peer("203.0.113.103", "BiglyBT 3.5",      dl=140_000,   ul=4_000,  progress=0.50),
            _peer("192.0.2.42",    "qBittorrent 4.6",  dl=40_000,    ul=4_000,  progress=0.48),
        ],
        "trackerStats": [
            _tracker(0, "bttracker.debian.org",  seeders=314, leechers=58, downloads=9120),
            _tracker(1, "tracker.opentrackr.org", seeders=512, leechers=92, downloads=15400),
        ],
    },
    {
        "id": 3,
        "name": "big-buck-bunny-1080p.mkv",
        "hashString": "1234567890abcdef1234567890abcdef12345678",
        "status": 0,
        "percentDone": 0.0,
        "rateDownload": 0,
        "rateUpload": 0,
        "uploadRatio": 0.0,
        "eta": -1,
        "totalSize": 1_200_000_000,
        "labels": [],
        "downloadDir": "/var/lib/transmission-daemon/downloads",
        "addedDate": _NOW - 600,
        "error": 0,
        "errorString": "",
        "activityDate": _NOW - 600,
        "doneDate": 0,
        "startDate": 0,
        "secondsDownloading": 0,
        "secondsSeeding": 0,
        "downloadedEver": 0,
        "uploadedEver": 0,
        "corruptEver": 0,
        "haveValid": 0,
        "haveUnchecked": 0,
        "leftUntilDone": 1_200_000_000,
        "sizeWhenDone": 1_200_000_000,
        "pieceCount": 2289,
        "pieceSize": 524_288,
        "peersConnected": 0,
        "peersGettingFromUs": 0,
        "peersSendingToUs": 0,
        "seedRatioLimit": 2.0,
        "seedRatioMode": 0,
        "queuePosition": 2,
        "isPrivate": False,
        "comment": "Big Buck Bunny test torrent",
        "creator": "mktorrent 1.1",
        "dateCreated": _NOW - 86400 * 365,
        "magnetLink": "magnet:?xt=urn:btih:1234567890abcdef",
        "peers": [],
        "trackerStats": [
            _tracker(0, "tracker.example.org", seeders=42, leechers=3, downloads=820),
        ],
    },
    {
        "id": 4,
        "name": "archlinux-2026.06.01-x86_64.iso",
        "hashString": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "status": 2,
        # Mid-recheck with a bad patch: 62% of the disk read, but only 41%
        # of what's been read hashed clean.
        "percentDone": 0.41,
        "recheckProgress": 0.62,
        "rateDownload": 0,
        "rateUpload": 0,
        "uploadRatio": 1.05,
        "eta": -1,
        "totalSize": 980_000_000,
        "labels": ["linux", "iso"],
        "downloadDir": "/var/lib/transmission-daemon/downloads",
        "addedDate": _NOW - 86400 * 2,
        "error": 0,
        "errorString": "",
        "activityDate": _NOW - 1,
        "doneDate": 0,
        "startDate": _NOW - 86400 * 2,
        "secondsDownloading": 7200,
        "secondsSeeding": 0,
        "downloadedEver": 862_400_000,
        "uploadedEver": 1_029_000_000,
        "corruptEver": 0,
        "haveValid": 686_000_000,
        "haveUnchecked": 176_400_000,
        "leftUntilDone": 117_600_000,
        "sizeWhenDone": 980_000_000,
        "pieceCount": 1869,
        "pieceSize": 524_288,
        "peersConnected": 12,
        "peersGettingFromUs": 4,
        "peersSendingToUs": 2,
        "seedRatioLimit": 2.0,
        "seedRatioMode": 0,
        "queuePosition": 3,
        "isPrivate": False,
        "comment": "Arch Linux 2026.06.01",
        "creator": "mktorrent 1.1",
        "dateCreated": _NOW - 86400 * 18,
        "magnetLink": "magnet:?xt=urn:btih:aaaaaaaaaaaaaaaa",
        "peers": [
            _peer("198.51.100.81", "qBittorrent 5.0", ul=22_000),
            _peer("203.0.113.30",  "Transmission 4.0", ul=8_000, incoming=True),
        ],
        "trackerStats": [
            _tracker(0, "tracker.archlinux.org", seeders=98, leechers=11, downloads=3201),
        ],
    },
    {
        "id": 5,
        "name": "broken-torrent-example",
        "hashString": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "status": 0,
        "percentDone": 0.17,
        "rateDownload": 0,
        "rateUpload": 0,
        "uploadRatio": 0.0,
        "eta": -1,
        "totalSize": 450_000_000,
        "labels": ["broken"],
        "downloadDir": "/var/lib/transmission-daemon/downloads",
        "addedDate": _NOW - 86400,
        "error": 3,
        "errorString": "Tracker returned an error",
        "activityDate": _NOW - 86400,
        "doneDate": 0,
        "startDate": _NOW - 86400,
        "secondsDownloading": 1200,
        "secondsSeeding": 0,
        "downloadedEver": 76_500_000,
        "uploadedEver": 0,
        "corruptEver": 524_288,
        "haveValid": 76_500_000,
        "haveUnchecked": 0,
        "leftUntilDone": 373_500_000,
        "sizeWhenDone": 450_000_000,
        "pieceCount": 858,
        "pieceSize": 524_288,
        "peersConnected": 0,
        "peersGettingFromUs": 0,
        "peersSendingToUs": 0,
        "seedRatioLimit": 2.0,
        "seedRatioMode": 0,
        "queuePosition": 4,
        "isPrivate": True,
        "comment": "",
        "creator": "unknown",
        "dateCreated": _NOW - 86400 * 90,
        "magnetLink": "magnet:?xt=urn:btih:bbbbbbbbbbbbbbbb",
        "peers": [],
        "trackerStats": [
            _tracker(0, "tracker.dead.example", last_ok=False, seeders=0, leechers=0, downloads=0),
        ],
    },
]


_MOCK_SESSION = {
    "download-dir": config.DOWNLOAD_DIR,
    "bind-address-ipv4": "0.0.0.0",
    "bind-address-ipv6": "::",
}


class MockTransmissionClient:
    def __init__(self, *args, **kwargs):
        pass

    def request(self, method, arguments=None):
        if method == "torrent-get":
            return {"arguments": {"torrents": list(_FAKE_TORRENTS)}, "result": "success"}
        return {"arguments": {}, "result": "success"}

    def get_session(self):
        return dict(_MOCK_SESSION)

    def get_session_bind_address(self):
        return _MOCK_SESSION.get("bind-address-ipv4")

    def get_session_bind_address_ipv6(self):
        return _MOCK_SESSION.get("bind-address-ipv6")

    def get_peer_port(self):
        return 51413

    def get_tracker_stats(self, id):
        # Canned echo-tracker response so the leak test is demo-able in mock
        # mode: the tracker "saw" a documentation IP.
        return {
            "id": id,
            "hashString": "f" * 40,
            "labels": ["ip-leak-test"],
            "trackerStats": [{
                "hasAnnounced": True,
                "lastAnnounceSucceeded": True,
                "lastAnnounceResult": "Success! Your torrent client IP is: 93.184.216.77",
                "lastScrapeResult": "",
            }],
        }

    def find_torrents_by_label(self, label):
        return []

    def add_magnet(self, magnet, paused=True, download_dir=None):
        return {"result": "success",
                "arguments": {"torrent-added": {"id": 9999, "hashString": "f" * 40}}}

    def start_now(self, id):
        return {"result": "success", "arguments": {}}

    def set_labels(self, id, labels):
        return {"result": "success", "arguments": {}}

    def remove(self, id, delete_local_data=False):
        return {"result": "success", "arguments": {}}

    def set_download_dir(self, path):
        _MOCK_SESSION["download-dir"] = path
        return {"result": "success", "arguments": {}}

    def set_location(self, id, location, move=False):
        for t in _FAKE_TORRENTS:
            if t["id"] == id:
                t["downloadDir"] = location
                break
        return {"result": "success", "arguments": {}}

    def get_torrents(self):
        return list(_FAKE_TORRENTS)

    def get_stats_torrents(self):
        return list(_FAKE_TORRENTS)

    def get_torrents_export(self):
        return list(_FAKE_TORRENTS)

    def get_torrent_files(self, ids):
        out = []
        for t in _FAKE_TORRENTS:
            if t["id"] not in ids:
                continue
            size = t.get("totalSize", 0)
            done = int(size * (t.get("percentDone") or 0))
            out.append({
                "id": t["id"],
                "hashString": t.get("hashString"),
                "downloadDir": t.get("downloadDir", "/downloads"),
                "name": t.get("name"),
                "files": [{
                    "name": f"{t.get('name')}/{t.get('name')}.mkv",
                    "length": size,
                    "bytesCompleted": done,
                }],
            })
        return out

    def get_incomplete_dir(self):
        return None

    def get_session_stats(self):
        up = sum(t.get("uploadedEver", 0) for t in _FAKE_TORRENTS)
        dn = sum(t.get("downloadedEver", 0) for t in _FAKE_TORRENTS)
        active = sum(
            1 for t in _FAKE_TORRENTS if t.get("status") in (4, 6)
        )
        paused = sum(1 for t in _FAKE_TORRENTS if t.get("status") == 0)
        return {
            "torrentCount": len(_FAKE_TORRENTS),
            "activeTorrentCount": active,
            "pausedTorrentCount": paused,
            "downloadSpeed": sum(t.get("rateDownload", 0) for t in _FAKE_TORRENTS),
            "uploadSpeed": sum(t.get("rateUpload", 0) for t in _FAKE_TORRENTS),
            "cumulative-stats": {
                "uploadedBytes": up,
                "downloadedBytes": dn,
                "filesAdded": len(_FAKE_TORRENTS),
                "sessionCount": 1,
                "secondsActive": 86400 * 30,
            },
            "current-stats": {
                "uploadedBytes": up,
                "downloadedBytes": dn,
                "filesAdded": len(_FAKE_TORRENTS),
                "sessionCount": 1,
                "secondsActive": 86400,
            },
        }

    def get_torrent_detail(self, id):
        for t in _FAKE_TORRENTS:
            if t["id"] == id:
                return dict(t)
        return None

    def get_torrent_details(self, ids):
        wanted = {int(i) for i in ids}
        return [dict(t) for t in _FAKE_TORRENTS if t["id"] in wanted]

    def start(self, id):
        return {"result": "success", "arguments": {}}

    def start_now(self, id):
        return {"result": "success", "arguments": {}}

    def stop(self, id):
        return {"result": "success", "arguments": {}}

    def remove(self, id, delete_local_data=False):
        return {"result": "success", "arguments": {}}

    def verify(self, id):
        return {"result": "success", "arguments": {}}

    def set_labels(self, id, labels):
        return {"result": "success", "arguments": {}}

    def add_magnet(self, magnet, paused=True, download_dir=None):
        return {"result": "success", "arguments": {"torrent-added": {"id": 9999}}}

    def add_torrent_file(self, base64_metainfo):
        return {"result": "success", "arguments": {"torrent-added": {"id": 9999}}}

    def get_torrent_location(self, id):
        return config.DOWNLOAD_DIR
