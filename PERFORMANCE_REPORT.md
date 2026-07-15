# Performance & Bug Report — transmission-dashboard

Reviewed 2026-07-03. Host: Raspberry Pi, 4 GB RAM, 4 cores, OS + data on **SSD**
(not SD card). Symptoms: dashboard slow when viewing the System page or when
switching back to the Torrents page; whole Pi less responsive as more torrents
seed/download.

Status legend: ✅ fixed · ⬜ not yet done

---

## ROOT CAUSE FOUND & FIXED (2026-07-03, after deploy of items 1–12)

The dashboard code fixes below were real but secondary. Live diagnosis on the
Pi found **transmission-daemon itself** was the bottleneck:

- A trivial RPC request took **11.2 s** (daemon event loop starved). The
  dashboard's polls (10 s read timeout) failed against it → "Live" flipped
  to "Stale". Before the timeout fix, those same stalls pinned gunicorn
  threads for 30 s each — the original "slow page switching" symptom.
- `top`: transmission at ~85% CPU with **30.9% system CPU**, busy
  `napi/mullvad` kernel thread, ~24k context switches/s. io-wait ~0, RAM
  fine, temp 49.7°C (no throttling), no USB/SSD errors.
- Only ~36 TCP connections but the dashboard reported **95 peers** → most
  peers were on **uTP**, multiplexed over one UDP socket and processed in
  user space inside transmission's single libevent loop (uTP is invisible
  to `ss` connection counts). Heavy sustained traffic (183 GB down in 22 h)
  through WireGuard + user-space uTP = the whole story.

**Fix:** `transmission-remote --no-utp` + `systemctl restart
transmission-daemon` (restart persists `"utp-enabled": false` to
settings.json and reconnects the swarm over TCP, which the kernel handles).

**Result:** RPC latency 11 s → **0.031 s**; transmission user CPU 14.5% →
2.2%; system CPU 30.9% → 13%; download running at ~15 MB/s afterwards.

Notes:
- VPN containment is unaffected: `bind-address-ipv4` (tunnel IP) binds TCP
  and UDP sockets alike; Mullvad has no port forwarding so all peer
  connections are outbound. Verify anytime with:
  `sudo ss -tnp state established | grep -i transmission` — local address
  must be the tunnel IP. Optional hardening (not done): nftables rule
  dropping any `debian-transmission` output not leaving via `mullvad`.
- Watch item: post-fix `top` showed **52% io-wait** while downloading at
  15 MB/s (likely transient post-restart catch-up). If sustained `wa` ever
  correlates with sluggishness, raise `"cache-size-mb"` (default 4 → 32)
  or cap download speed. Don't pre-tune.
- Transmission RPC password appeared in a pasted terminal output during
  diagnosis (2026-07-03) — RPC is localhost-bound so low risk, but rotate
  when convenient.

---

## Likely main culprit for the felt slowness

### 1. ✅ No in-flight guard on any polling loop (all pages)
Every poller used `setInterval`, which fires on schedule whether or not the
previous request finished. System page hit `/api/system` every 2 s; torrents
page hit `/api/torrents` + `/api/copy/states` every 5 s plus
`/api/torrents/details` every 2 s while cards are expanded. When
transmission-daemon gets sluggish (many active torrents), requests stack up.
**Fix applied:** in-flight guards on all recurring polls in `index.html`,
`system.html`, `events.html`, and `tunnel.js` — a tick is skipped if the
previous request for that endpoint is still pending.

### 2. ✅ 30-second RPC timeout × only 8 gunicorn threads
Service runs 1 worker × 8 threads (`systemd/transmission-dashboard.service`);
every Transmission RPC waited up to 30 s (`transmission.py`). Piled-up polls
could occupy all 8 threads for tens of seconds, stalling everything including
plain page navigation — matches the "slow when switching pages" symptom.
**Fix applied:** RPC timeout is now `(connect 3.05 s, read 10 s)`.

## Real bugs

### 3. ✅ System page fetched `/api/system` twice on overlapping timers
`pollDisk` in `system.html` fetched the full `/api/system` every 10 s just to
render the disk card, even though the main 2 s poll already returns the same
disk data (`render()` just didn't use it).
**Fix applied:** disk card renders from the main poll payload; `pollDisk`,
`DISK_POLL_MS`, and `diskTimer` deleted.

### 4. ✅ `/api/tunnel` had no server-side cache
System page polls it every 10 s; each call spawned a `wg show` subprocess,
called `psutil.net_if_stats()`/`net_if_addrs()`, made a Transmission RPC for
`bind-address-ipv4`, and — on Transmission 4.x where the RPC omits the field —
also opened and JSON-parsed `settings.json` every poll. The near-identical
`/api/tunnel-status` endpoint was already cached 30 s.
**Fix applied:** `/api/tunnel` result now cached with the same TTL
(`TUNNEL_CHECK_CACHE_TTL`, default 30 s), thundering-herd-safe behind a lock.
Also: `settings.json` bind-address read is now mtime-cached in
`TransmissionClient` so the file is only re-parsed when it changes.

### 5. ✅ Copy-start race allows duplicate rsync workers
`api_copy_start` checked `tid in _active_copies` but the entry was only
registered later, inside `_run_copy` once the worker thread started. Two quick
clicks on "Copy" could launch two rsyncs to the same destination.
**Fix applied:** the `_active_copies` slot is reserved atomically at the
check, before any further work; every early-return path releases it via a
`finally` block, and `_run_copy` reuses the reserved entry (keeping its
cancel Event valid). Verified with 5 concurrent POSTs → exactly one 200,
four 409s, and no leaked reservation after a failed start.

### 6. ✅ The `events` table grows forever
`db.log_event` inserted on every copy/refresh/redownload etc.; nothing pruned.
**Fix applied:** rows older than `EVENTS_RETENTION_DAYS` (env var, default
90 days) are deleted on startup and then at most once per day from the
`log_event` write path. Pruning failures never break event logging.

### 7. ✅ Background tabs keep polling on two pages
`static/tunnel.js` (15 s) and `events.html` (5 s) never stopped polling in
hidden tabs.
**Fix applied:** both now pause on `visibilitychange` and resume with an
immediate refresh when the tab becomes visible again, matching the torrents
and system pages. The events page's `since` cursor means the catch-up fetch
only pulls what was missed.

## Optimizations (smaller, but they add up on the Pi)

### 8. ✅ Access log to journald for every poll
`--access-logfile -` wrote a journald line per request — roughly 1 line/second
24/7 of journald CPU and I/O.
**Fix applied:** removed `--access-logfile -` from the systemd unit; errors
still go to the journal via `--error-logfile -`. Needs `systemctl
daemon-reload` + service restart on the Pi to take effect.

### 9. ✅ `copy_state.json` fsync'd once per second during copies
**Fix applied:** `_COPY_STATE_FLUSH_MIN_INTERVAL` raised 1 s → 15 s. API reads
come from the in-memory cache and terminal states force-persist, so the
periodic flush only matters for crash recovery.

### 10. ✅ Poll rates were aggressive for a Pi
**Fix applied:** system page `/api/system` poll 2 s → 5 s (it runs five psutil
probes including a full `/proc` scan per hit); torrents page expanded-card
detail poll 2 s → 5 s (its payload carries `peers` + `trackerStats`, the two
heaviest per-torrent fields for the daemon to serialize).

### 11. ✅ rsync copies ran at unlimited speed
Copies compete with transmission for CPU (ssh encryption) and disk I/O.
**Fix applied:** optional **Copy speed limit (KB/s)** field in Settings →
Media server, stored as `bwlimit_kbps` in `media_config.json` (0/empty =
unlimited, key omitted). When set, both rsync invocations in `_run_copy`
(single-pass and per-season) get `--bwlimit=N`. Validated round-trip,
rejection of negative/non-numeric values, and flag presence/absence in the
built rsync command.

### 12. ✅ Minor robustness notes
- **Fixed:** `_net_rates()` now enforces a 1 s minimum sampling window —
  calls landing sooner return the last computed rates without resetting the
  shared baseline, so two open System tabs no longer make the network rates
  jitter.
- **Fixed:** `TransmissionClient` session-id refresh now happens under a
  lock; concurrent 409s adopt whichever id is newest instead of clobbering
  each other and forcing extra retries.

## SECOND ROOT CAUSE (2026-07-03, found after the uTP fix)

Pages still took 10–15 s to load even with the daemon healthy (RPC 31 ms).
Measurements: app served `/login` in **0.004 s** on localhost and **0.031 s**
through Tailscale (all peers `direct`) — so neither the app path nor the
network was slow. The stalls were **gunicorn thread-pool exhaustion** caused
by two endpoints that could hang a request thread for up to 20 s each,
fired on every page load and every tab-return:

- `/api/media-storage`: on cache expiry it SSH'd to the media server
  (10 s connect / 20 s total timeout) **while holding its lock** — every
  other media-storage request from every tab queued behind it, each
  pinning a thread. `tailscale status` showed the media host **offline for
  a day**, so every probe ate the full timeout.
- `/api/mullvad`: up to two 10 s HTTP calls under its lock, with only a
  30 s error TTL — nearly every page load re-probed when the API was slow.

One page open fires ~6 API calls at once; a few page switches during a
stuck probe exhausted all 8 threads and the page HTML sat in the queue —
also the cause of the residual "Stale" flips.

### 13. ✅ Fix: probes moved off the request thread (single-flight)
`/api/media-storage` and `/api/mullvad` now kick their probe into a
single-flight background thread and answer instantly with cached data (or
a "checking…" error) plus a `refreshing: true` flag; `index.html` re-polls
4 s later when it sees the flag so fresh numbers land without waiting for
the normal cadence. Verified: endpoint answers in ~0 ms while a probe
hangs, concurrent calls don't spawn extra probes, results cache as before.

Also added (same session): `transmission.py` logs any RPC slower than 2 s
and every failed RPC at WARNING with method + duration — visible via
`journalctl -u transmission-dashboard` — so future daemon slowness is
diagnosable from the journal (access logs were removed in item 8).

### 14. ✅ Backlog of added-but-stopped torrents slows every poll (2026-07-03)
Every torrent in the daemon's session — even fully stopped — is serialized
into the `torrent-get` response the dashboard polls every 5 s, and adds to
the daemon's own bookkeeping. A growing backlog of added-but-never-started
torrents therefore makes every poll (and the daemon) slower forever.
**Fix applied:** new **Unload** feature. Unload (context menu or bulk bar,
stopped torrents only) removes the torrent from Transmission but saves its
magnet link, download dir, labels, and custom name in `dashboard.db`
(`unloaded_torrents` table). Parked torrents cost the daemon zero. They
appear in a collapsible "Unloaded" panel at the bottom of the torrents
page; **Start** re-adds the magnet (same download dir, so existing partial
data is picked up), restores labels/custom name, and starts it. **Forget**
drops the saved magnet. Verified with unit tests (8) plus a mock end-to-end
run: unload → 409 for running torrents → list → start → row cleared.

## Unrelated UI changes made along the way

- Removed the "Nothing in progress." / "Nothing finished." per-column empty
  placeholders from the bottom of the torrents page (user request,
  2026-07-03). The `.column-empty` CSS rule in `style.css` is now unused but
  left in place.

## Not the dashboard's fault

The baseline "Pi gets less responsive as more torrents seed" is mostly
transmission-daemon itself (peer connections, hashing, disk I/O). The
dashboard's contribution is the polling load on the daemon — items 1, 2, and
10 reduce that — but these fixes won't change transmission's own footprint.
