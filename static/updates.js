(function () {
    // No shared base template, so the badge element doesn't exist in the
    // markup — inject it into the topbar actions on whatever page loaded us.
    const actions = document.querySelector('.topbar-actions');
    if (!actions) return;

    // A non-navigating indicator (like the tunnel one) — the action it hints
    // at is "run ./update.sh on the server", not a click, so the detail lives
    // in the tooltip rather than a link.
    const badge = document.createElement('span');
    badge.id = 'update-indicator';
    badge.className = 'update-badge';
    badge.hidden = true;
    badge.innerHTML =
        '<span class="update-dot"></span><span class="update-label"></span>';
    // Sit the badge just before the "Live" indicator when present, else at
    // the end — keeps it left of the logout link on every page.
    const live = actions.querySelector('.live');
    if (live) actions.insertBefore(badge, live);
    else actions.appendChild(badge);

    const label = badge.querySelector('.update-label');
    // Poll every 5 minutes. The server caches for UPDATE_CHECK_CACHE_TTL
    // (15 min default) and only fetches once per window, so this is cheap.
    const POLL_MS = 5 * 60 * 1000;

    function hide() {
        badge.hidden = true;
    }

    function apply(data) {
        if (!data || data.ok === false || data.enabled === false) {
            hide();
            return;
        }
        const behind = Number(data.behind) || 0;
        if (behind <= 0) {
            // Up to date — show a calm green dot + label rather than hiding, so
            // the indicator always confirms the checkout's state at a glance.
            // When the last check was offline we can't be sure, so fall back to
            // the yellow "stale" treatment with a caveat instead of green.
            label.textContent = 'Up to date';
            if (data.stale) {
                badge.dataset.state = 'stale';
                badge.title = 'Up to date as of the last check '
                    + '(offline — could be out of date)';
            } else {
                badge.dataset.state = 'uptodate';
                badge.title = 'Dashboard is up to date with upstream';
            }
            badge.hidden = false;
            return;
        }
        label.textContent = behind === 1 ? '1 update' : `${behind} updates`;

        const lines = [];
        lines.push(behind === 1
            ? '1 new commit is available upstream'
            : `${behind} new commits are available upstream`);
        if (data.upstream_subject) {
            const sha = data.upstream_sha ? `${data.upstream_sha} ` : '';
            lines.push(`Latest: ${sha}${data.upstream_subject}`);
        }
        if (data.stale) {
            lines.push('(offline — count may be out of date)');
        }
        lines.push('Run ./update.sh on the server to apply');
        badge.title = lines.join('\n');
        badge.dataset.state = data.stale ? 'stale' : 'available';
        badge.hidden = false;
    }

    let pollBusy = false;
    async function poll() {
        if (pollBusy) return;
        pollBusy = true;
        try {
            const res = await fetch('/api/update-status', { cache: 'no-store' });
            if (res.status === 401) return;
            if (!res.ok) { hide(); return; }
            apply(await res.json());
        } catch (err) {
            hide();
        } finally {
            pollBusy = false;
        }
    }

    // Pause while the tab is hidden, matching tunnel.js — a background tab
    // can't show the badge and each uncached refresh runs `git fetch`.
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
