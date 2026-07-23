(function () {
    // Compact topbar chip mirroring the Tracker IP leak test on the Stats
    // page. The full test (run button, live progress, config) lives there;
    // this is a glanceable summary of the *last* verdict — manual or from the
    // periodic scheduler — so a leak is visible from the torrents view without
    // navigating away. Clicking the chip jumps to the Stats page to run it.
    const indicator = document.getElementById('tracker-test-indicator');
    if (!indicator) return;
    const dot = indicator.querySelector('.tunnel-dot');
    const label = indicator.querySelector('.tunnel-label');
    // Slow when idle (catches scheduled runs), fast while a run is in flight.
    const IDLE_MS = 30000;
    const RUN_MS = 3000;

    function fmtAgo(iso) {
        if (!iso) return null;
        const ts = Date.parse(iso);
        if (Number.isNaN(ts)) return null;
        const secs = Math.max(0, Math.floor((Date.now() - ts) / 1000));
        if (secs < 60) return `${secs}s ago`;
        if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
        const h = Math.floor(secs / 3600);
        return `${h}h ${Math.floor((secs % 3600) / 60)}m ago`;
    }

    function set(state, text, title) {
        indicator.dataset.state = state;
        dot.dataset.state = state;
        label.textContent = text;
        indicator.title = title;
    }

    function applyRunning(d) {
        let note;
        if (d.seen_ips && d.seen_ips.length) note = `tracker saw ${d.seen_ips.join(', ')}, comparing…`;
        else if (d.tracker_message) note = `tracker said: ${d.tracker_message}`;
        else if (d.announced) note = 'announce sent, waiting for the reply…';
        else note = 'announcing to tracker…';
        set('warn', 'Testing…', `Tracker IP test running (${d.elapsed_seconds}s)\n${note}`);
    }

    function applyResult(r) {
        if (!r) {
            set('unknown', 'Test —', 'Tracker IP test: not run yet.\nClick to run it on the Stats page.');
            return;
        }
        const ago = fmtAgo(r.finished_at);
        // Show the age on the chip itself, not just the tooltip — a passing
        // result is only reassuring if you can see it's recent.
        const tag = ago ? ` · ${ago}` : '';
        const when = ago ? `\nChecked ${ago}` : '';
        if (r.verdict === 'pass') {
            set('ok', `Tracker OK${tag}`,
                `Tracker IP test PASSED — tracker saw ${(r.seen_ips || []).join(', ')}, `
                + `matching the tunnel exit.${when}`);
        } else if (r.verdict === 'leak') {
            set('down', `IP leak${tag}`,
                `Tracker IP test FAILED — real IP exposed to trackers:\n`
                + `${(r.problems || []).join('; ')}${when}`);
        } else if (r.verdict === 'cancelled') {
            set('unknown', `Test —${tag}`, `Tracker IP test was cancelled.${when}`);
        } else {
            set('error', `Test ?${tag}`,
                `Tracker IP test inconclusive — ${(r.problems || []).join('; ')}${when}`);
        }
    }

    let pollBusy = false;
    let running = false;
    async function poll() {
        if (pollBusy) return;
        pollBusy = true;
        try {
            const res = await fetch('/api/tracker-test/status', { cache: 'no-store' });
            if (res.status === 401) {
                running = false;
                set('unknown', 'Test ?', 'Tracker IP test unavailable — session expired, sign in again.');
                reschedule();
                return;
            }
            if (!res.ok) { reschedule(); return; }
            const d = await res.json();
            running = !!d.running;
            if (running) applyRunning(d);
            else applyResult(d.result);
            reschedule();
        } catch (err) {
            reschedule();
        } finally {
            pollBusy = false;
        }
    }

    let timer = null;
    function reschedule() {
        if (timer) clearTimeout(timer);
        if (document.hidden) { timer = null; return; }
        timer = setTimeout(poll, running ? RUN_MS : IDLE_MS);
    }

    document.addEventListener('visibilitychange', () => {
        if (document.hidden) { if (timer) { clearTimeout(timer); timer = null; } }
        else poll();
    });

    // Only surface the chip when the test is enabled — otherwise the feature is
    // off and an idle "Test —" would just be noise.
    (async function init() {
        try {
            const res = await fetch('/api/tracker-test/config', { cache: 'no-store' });
            if (!res.ok) return;
            const cfg = await res.json();
            if (!cfg.enabled) return;
            indicator.hidden = false;
            poll();
        } catch (err) { /* stays hidden */ }
    })();
})();
