(function () {
    const indicator = document.getElementById('tunnel-indicator');
    if (!indicator) return;
    const dot = indicator.querySelector('.tunnel-dot');
    const label = indicator.querySelector('.tunnel-label');
    const POLL_MS = 15000;

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
        label.textContent = status === 'up' ? 'Tunnel'
                          : status === 'down' ? 'Tunnel down'
                          : status === 'error' ? 'Tunnel ?'
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
        if (data.last_handshake_seconds != null) {
            lines.push(`Last handshake: ${data.last_handshake_seconds}s ago`);
        }
        if (data.transmission_bind_address) {
            lines.push(`Transmission bind: ${data.transmission_bind_address}`);
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
            if (res.status === 401) return;
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
