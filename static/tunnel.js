(function () {
    const indicator = document.getElementById('tunnel-indicator');
    if (!indicator) return;
    const dot = indicator.querySelector('.tunnel-dot');
    const label = indicator.querySelector('.tunnel-label');
    const POLL_MS = 15000;

    function fmtBytes(n) {
        if (n == null || Number.isNaN(n)) return null;
        const units = ['B', 'KB', 'MB', 'GB', 'TB'];
        let v = n, i = 0;
        while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
        return `${v >= 100 || i === 0 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
    }

    function fmtAgo(iso) {
        if (!iso) return null;
        const ts = Date.parse(iso);
        if (Number.isNaN(ts)) return null;
        const secs = Math.max(0, Math.floor((Date.now() - ts) / 1000));
        if (secs < 60) return `${secs}s ago`;
        if (secs < 3600) return `${Math.floor(secs / 60)}m ${secs % 60}s ago`;
        const h = Math.floor(secs / 3600);
        const m = Math.floor((secs % 3600) / 60);
        return `${h}h ${m}m ago`;
    }

    function apply(data) {
        // No interface configured server-side. Rather than hiding the
        // indicator (which made the whole tunnel feature invisible on a fresh
        // install — you couldn't tell it existed), show a neutral, muted
        // "Tunnel off" chip so the unconfigured state is explicit and the
        // feature is discoverable. It's deliberately NOT a red "down" — nothing
        // is wrong, monitoring is just off. Config is static, so render it once
        // and stop polling.
        if (data && data.status === 'disabled') {
            indicator.style.display = '';
            indicator.dataset.state = 'off';
            dot.dataset.state = 'off';
            label.textContent = 'Tunnel off';
            indicator.title = 'VPN tunnel monitoring is off.\n'
                + 'Set TUNNEL_IFACE in .env to your WireGuard interface name '
                + 'to watch tunnel health here.';
            stop();  // config is static; no point polling a disabled indicator
            return;
        }
        indicator.style.display = '';
        if (!data || data.ok === false) {
            indicator.dataset.state = 'unknown';
            dot.dataset.state = 'unknown';
            label.textContent = 'Tunnel ?';
            indicator.title = (data && data.error)
                ? `Tunnel status unavailable: ${data.error}`
                : 'Tunnel status unavailable';
            return;
        }
        const status = data.status || 'unknown';
        const uiState = status === 'up' ? 'ok'
                      : status === 'down' ? 'down'
                      : status === 'error' ? 'error'
                      : 'unknown';
        indicator.dataset.state = uiState;
        dot.dataset.state = uiState;
        // A leak is a "down" but deserves a sharper label than a plain dropped
        // tunnel — it means traffic is exposed, not just offline.
        let downLabel = 'Tunnel down';
        if (data.reason === 'ipv6_leak') downLabel = 'IPv6 leak';
        else if (data.reason === 'route_leak' || data.reason === 'route_leak_v6') downLabel = 'Route leak';
        else if (data.reason === 'live_bind_mismatch') downLabel = 'Bind leak';
        label.textContent = status === 'up' ? 'Tunnel'
                          : status === 'down' ? downLabel
                          : 'Tunnel ?';

        const lines = [];
        if (status === 'up') {
            lines.push('Transmission is routing through the tunnel');
        } else if (status === 'down') {
            lines.push(data.error ? `Down: ${data.error}` : 'Down');
        } else if (status === 'error') {
            lines.push(data.error ? `Check error: ${data.error}` : 'Check error');
        }
        if (data.interface) {
            const ifaceLine = data.interface_address
                ? `${data.interface}: ${data.interface_address}`
                : `${data.interface}`;
            lines.push(ifaceLine);
        }
        if (data.endpoint) {
            lines.push(`Endpoint: ${data.endpoint}`);
        }
        if (data.last_handshake_seconds != null) {
            const stale = data.stale_after_seconds != null
                && data.last_handshake_seconds > data.stale_after_seconds;
            lines.push(`Last handshake: ${data.last_handshake_seconds}s ago`
                + (stale ? ` (stale > ${data.stale_after_seconds}s)` : ''));
        }
        const rx = fmtBytes(data.rx_bytes);
        const tx = fmtBytes(data.tx_bytes);
        if (rx || tx) {
            lines.push(`Transfer: ↓ ${rx || '0 B'}  ↑ ${tx || '0 B'}`);
        }
        if (data.transmission_bind_address) {
            const mismatch = data.transmission_bound === false;
            lines.push(`Transmission bind: ${data.transmission_bind_address}`
                + (mismatch ? ' (not the tunnel IP)' : ''));
        }
        if (data.host_bare_ipv6) {
            lines.push(`Transmission bind (v6): ${data.transmission_bind_address6 || 'unset'}`
                + (data.reason === 'ipv6_leak' ? ' (host has bare IPv6 — leak)' : ''));
        }
        if (data.live_bind_addrs && data.live_bind_addrs.length) {
            lines.push(`Live sockets (:${data.peer_port}): ${data.live_bind_addrs.join(', ')}`);
        }
        if (data.route_egress_dev && data.route_egress_dev !== data.interface) {
            lines.push(`Egress dev: ${data.route_egress_dev} (not the tunnel)`);
        }
        const rec = data.recovery;
        if (rec && rec.enabled && rec.last_attempt_at) {
            const recAgo = fmtAgo(rec.last_attempt_at);
            let recLine = `Auto-recovery: attempt ${rec.attempts}/${rec.max_attempts}`
                + (recAgo ? ` ${recAgo}` : '')
                + (rec.last_result ? ` — ${rec.last_result}` : '');
            if (rec.gave_up) recLine += ' (gave up; manual fix needed)';
            lines.push(recLine);
        }
        const ago = fmtAgo(data.checked_at);
        if (ago) lines.push(`Checked ${ago}${data.cached ? ' (cached)' : ''}`);
        indicator.title = lines.join('\n');
    }

    let pollBusy = false;
    async function poll() {
        if (pollBusy) return;
        pollBusy = true;
        try {
            const res = await fetch('/api/tunnel-status', { cache: 'no-store' });
            if (res.status === 401) {
                // Session expired — the previous (possibly green) state is now
                // unverifiable. Fail closed to "?" rather than freezing a
                // stale all-clear on screen.
                apply({ ok: false, error: 'session expired — sign in again' });
                return;
            }
            if (!res.ok) {
                apply({ ok: false, error: 'HTTP ' + res.status });
                return;
            }
            apply(await res.json());
        } catch (err) {
            apply({ ok: false, error: err.message });
        } finally {
            pollBusy = false;
        }
    }

    // Pause polling while the tab is hidden — a background tab can't show
    // the indicator anyway, and each uncached server-side refresh spawns a
    // `wg` subprocess on the Pi.
    let timer = null;
    function start() {
        if (timer) return;
        poll();
        timer = setInterval(poll, POLL_MS);
    }
    function stop() {
        if (timer) {
            clearInterval(timer);
            timer = null;
        }
    }
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) stop();
        else start();
    });
    start();
})();
