/*
 * Rips tab — drive an optical drive through HandBrakeCLI.
 *
 * Drive cards are built once and then patched in place on each poll, the same
 * way the torrent cards work: a full re-render on every tick would fight the
 * user for the title radio, the name field and the track checkboxes.
 */
(function () {
    const drivesEl = document.getElementById('rip-drives');
    if (!drivesEl) return;

    const template = document.getElementById('rip-drive-template');
    const errorEl = document.getElementById('rip-error');
    const mainEl = document.getElementById('rip-main');
    const unavailableEl = document.getElementById('rip-unavailable');
    const noDrivesEl = document.getElementById('rip-no-drives');
    const dirWarningEl = document.getElementById('rip-dir-warning');
    const historyEl = document.getElementById('rip-history');
    const historyEmptyEl = document.getElementById('rip-history-empty');

    // Poll fast while something is encoding, slowly when idle.
    const POLL_ACTIVE_MS = 2000;
    const POLL_IDLE_MS = 6000;

    let presetGroups = [];
    let settings = null;
    // device -> { scanSignature, titles, mainIndex, selectedTitle, nameDirty }
    const ui = new Map();
    let pollTimer = null;
    let historyDirty = true;

    // ---------- helpers ----------

    function toast(message, type = 'info') {
        const container = document.getElementById('toasts');
        const el = document.createElement('div');
        el.className = `toast toast-${type}`;
        el.textContent = message;
        container.appendChild(el);
        requestAnimationFrame(() => el.classList.add('toast-show'));
        setTimeout(() => {
            el.classList.remove('toast-show');
            el.classList.add('toast-hide');
            setTimeout(() => el.remove(), 300);
        }, 2700);
    }

    function showError(el, msg) {
        if (!el) return;
        el.textContent = msg;
        el.hidden = !msg;
    }

    function fmtBytes(n) {
        if (n === null || n === undefined) return '—';
        const units = ['B', 'KB', 'MB', 'GB', 'TB'];
        let v = Number(n);
        let i = 0;
        while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
        return i === 0 ? `${Math.round(v)} B` : `${v.toFixed(1)} ${units[i]}`;
    }

    function fmtDuration(seconds) {
        if (seconds === null || seconds === undefined) return '—';
        const s = Math.max(0, Math.round(seconds));
        const h = Math.floor(s / 3600);
        const m = Math.floor((s % 3600) / 60);
        const sec = s % 60;
        if (h) return `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`;
        return `${m}:${String(sec).padStart(2, '0')}`;
    }

    function fmtEta(seconds) {
        if (!seconds && seconds !== 0) return '—';
        return `${fmtDuration(seconds)} left`;
    }

    function fmtWhen(iso) {
        if (!iso) return '—';
        const d = new Date(iso);
        if (isNaN(d)) return iso;
        return d.toLocaleString();
    }

    // Mirrors handbrake.sanitize_filename so the preview matches the file the
    // server will actually create.
    function sanitizeName(name) {
        return (name || '')
            .trim()
            .replace(/_/g, ' ')
            .replace(/[<>:"/\\|?*\x00-\x1f]/g, '')
            .replace(/\s+/g, ' ')
            .trim()
            .replace(/^[. ]+|[. ]+$/g, '')
            .slice(0, 150);
    }

    async function api(path, options) {
        const res = await fetch(path, options);
        let data = {};
        try { data = await res.json(); } catch (e) { /* non-JSON error page */ }
        if (!res.ok || data.ok === false) {
            throw new Error(data.error || `${res.status} ${res.statusText}`);
        }
        return data;
    }

    function postJSON(path, body) {
        return api(path, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body || {}),
        });
    }

    function field(root, name) {
        return root.querySelector(`[data-field="${name}"]`);
    }

    // ---------- presets ----------

    function fillPresetSelect(select, selected) {
        select.textContent = '';
        presetGroups.forEach(group => {
            const og = document.createElement('optgroup');
            og.label = group.group;
            group.presets.forEach(p => {
                const opt = document.createElement('option');
                opt.value = p.name;
                opt.textContent = p.name;
                opt.dataset.description = p.description || '';
                og.appendChild(opt);
            });
            select.appendChild(og);
        });
        if (!presetGroups.length && selected) {
            // HandBrake couldn't be queried — keep whatever is configured so
            // the form still submits something valid.
            const opt = document.createElement('option');
            opt.value = selected;
            opt.textContent = selected;
            select.appendChild(opt);
        }
        if (selected) select.value = selected;
        if (!select.value && select.options.length) select.selectedIndex = 0;
    }

    function presetDescription(select) {
        const opt = select.selectedOptions[0];
        return opt ? (opt.dataset.description || '') : '';
    }

    // ---------- drive cards ----------

    function cardFor(device) {
        let card = drivesEl.querySelector(`.rip-drive[data-device="${CSS.escape(device)}"]`);
        if (card) return card;
        card = template.content.firstElementChild.cloneNode(true);
        card.dataset.device = device;
        field(card, 'device').textContent = device;
        wireCard(card, device);
        drivesEl.appendChild(card);
        return card;
    }

    function wireCard(card, device) {
        card.querySelector('[data-action="scan"]').addEventListener('click', async (e) => {
            const btn = e.currentTarget;
            btn.disabled = true;
            showError(errorEl, '');
            try {
                await postJSON('/api/rip/scan', { device });
                // Drop any previous result so the picker redraws for this disc.
                ui.delete(device);
                field(card, 'setup').hidden = true;
                field(card, 'disc-state').textContent = 'Scanning disc…';
                schedule(POLL_ACTIVE_MS);
            } catch (err) {
                showError(errorEl, err.message);
                toast(err.message, 'error');
            } finally {
                btn.disabled = false;
            }
        });

        card.querySelector('[data-action="eject"]').addEventListener('click', async (e) => {
            const btn = e.currentTarget;
            btn.disabled = true;
            showError(errorEl, '');
            try {
                await postJSON('/api/rip/eject', { device });
                ui.delete(device);
                field(card, 'setup').hidden = true;
                toast('Disc ejected');
                refresh();
            } catch (err) {
                showError(errorEl, err.message);
                toast(err.message, 'error');
            } finally {
                btn.disabled = false;
            }
        });

        card.querySelector('[data-action="cancel"]').addEventListener('click', async (e) => {
            const btn = e.currentTarget;
            btn.disabled = true;
            try {
                await postJSON('/api/rip/stop', { device });
                toast('Cancelling rip…');
            } catch (err) {
                toast(err.message, 'error');
            } finally {
                btn.disabled = false;
                refresh();
            }
        });

        card.querySelector('[data-action="start"]').addEventListener('click', () => startRip(card, device));

        const nameInput = field(card, 'name');
        nameInput.addEventListener('input', () => {
            // Once the user edits the name, stop overwriting it from the disc
            // label on subsequent polls.
            const state = ui.get(device);
            if (state) state.nameDirty = true;
            updateNamePreview(card);
        });
        field(card, 'container').addEventListener('change', () => updateNamePreview(card));
        field(card, 'preset').addEventListener('change', () => {
            field(card, 'preset-hint').textContent = presetDescription(field(card, 'preset'));
        });
    }

    function updateNamePreview(card) {
        const name = sanitizeName(field(card, 'name').value) || 'disc';
        const ext = field(card, 'container').value === 'mp4' ? '.mp4' : '.mkv';
        field(card, 'name-preview').textContent = name + ext;
    }

    function describeDisc(drive) {
        if (!drive.has_disc) return 'No disc in the drive.';
        if (drive.media_state && drive.media_state !== 'complete') {
            return `Reading disc (${drive.media_state})…`;
        }
        const label = drive.disc_label || 'unlabelled disc';
        return `Disc loaded: ${label}`;
    }

    function updateDrive(card, drive) {
        const device = drive.device;
        field(card, 'model').textContent = drive.model || device;

        const bus = field(card, 'bus');
        bus.textContent = (drive.bus || '').toUpperCase();
        bus.hidden = !drive.bus;
        const media = field(card, 'media-type');
        media.textContent = (drive.media_type || '').toUpperCase();
        media.hidden = !drive.media_type;

        const job = drive.job || {};
        const scan = drive.scan || {};
        const busy = !!job.active;

        card.querySelector('[data-action="scan"]').disabled =
            busy || !drive.has_disc || scan.status === 'scanning';
        card.querySelector('[data-action="eject"]').disabled = busy || !drive.has_disc;

        // ---- disc / status line ----
        let state = describeDisc(drive);
        if (scan.status === 'scanning') {
            state = 'Scanning disc — this takes up to a minute…';
        } else if (scan.status === 'error') {
            state = `Scan failed: ${scan.error}`;
        } else if (scan.status === 'done' && scan.stale) {
            state = `${describeDisc(drive)} — the disc changed since the last scan, scan again.`;
        }
        if (!drive.readable) {
            state += ' (the dashboard cannot read this device — check that its user is in the "cdrom" group)';
        }
        field(card, 'disc-state').textContent = state;

        // ---- live encode ----
        const jobEl = field(card, 'job');
        jobEl.hidden = !busy && !isRecentTerminal(job);
        if (!jobEl.hidden) {
            const pct = job.percent === null || job.percent === undefined ? 0 : job.percent;
            field(card, 'job-fill').style.width = `${Math.min(100, Math.max(0, pct))}%`;
            field(card, 'job-pct').textContent = `${pct.toFixed ? pct.toFixed(1) : pct}%`;
            field(card, 'job-phase').textContent = jobPhase(job);
            field(card, 'job-fps').textContent = job.fps ? `${job.fps} fps` : '—';
            field(card, 'job-eta').textContent = busy ? fmtEta(job.eta_seconds) : '—';
            field(card, 'job-status').textContent = jobStatusLine(job);
            card.querySelector('[data-action="cancel"]').hidden = !busy;
        }

        // ---- title picker ----
        const signature = `${scan.status}|${scan.at}|${drive.disc_label}`;
        const state0 = ui.get(device);
        if (scan.status === 'done' && !scan.stale && !busy) {
            if (!state0 || state0.scanSignature !== signature) {
                loadTitles(card, device, signature, drive);
            } else {
                field(card, 'setup').hidden = false;
            }
        } else {
            field(card, 'setup').hidden = true;
        }
    }

    function isRecentTerminal(job) {
        return ['done', 'error', 'cancelled', 'interrupted'].includes(job.status);
    }

    // How long a finished rip's card stays up after the disc is gone.
    const TERMINAL_LINGER_SEC = 120;

    function hasSomethingToShow(drive) {
        if (drive.has_disc) return true;
        const job = drive.job || {};
        if (job.active) return true;
        if (drive.scan && drive.scan.status === 'scanning') return true;
        // "Eject when done" pops the disc the moment a rip finishes; keep the
        // result on screen briefly rather than having the card vanish at 100%.
        if (isRecentTerminal(job) && job.finished_at) {
            const age = (Date.now() - new Date(job.finished_at).getTime()) / 1000;
            if (age >= 0 && age < TERMINAL_LINGER_SEC) return true;
        }
        return false;
    }

    function driveLabel(drive) {
        return `${drive.model || drive.device} (${drive.device})`;
    }

    function jobPhase(job) {
        if (job.status === 'done') return 'finished';
        if (job.status === 'error') return 'failed';
        if (job.status === 'cancelled') return 'cancelled';
        if (job.status === 'interrupted') return 'interrupted';
        if (job.phase === 'working') {
            return job.pass_count > 1 ? `encoding (pass ${job.pass}/${job.pass_count})` : 'encoding';
        }
        return job.phase || 'starting';
    }

    function jobStatusLine(job) {
        const name = job.output_name || '—';
        if (job.status === 'done') {
            return `${name} — ${fmtBytes(job.output_bytes)}`;
        }
        if (job.status === 'error') return `${name} — ${job.error_message || 'failed'}`;
        if (job.status === 'cancelled') return `${name} — cancelled`;
        if (job.status === 'interrupted') return `${name} — ${job.error_message || 'interrupted'}`;
        const bits = [name];
        if (job.title_seconds) bits.push(`title ${job.title_index} · ${fmtDuration(job.title_seconds)}`);
        if (job.preset) bits.push(job.preset);
        return bits.join(' · ');
    }

    async function loadTitles(card, device, signature, drive) {
        let data;
        try {
            data = await api(`/api/rip/titles?device=${encodeURIComponent(device)}`);
        } catch (err) {
            showError(errorEl, err.message);
            return;
        }
        if (data.status !== 'done') return;

        const state = {
            scanSignature: signature,
            titles: data.titles || [],
            mainIndex: data.main_index,
            selectedTitle: data.main_index,
            nameDirty: false,
        };
        ui.set(device, state);

        renderTitles(card, device, state);

        // Prefill from the disc label; the scan's own title name is a decent
        // fallback when the drive reported no label.
        const suggested = drive.disc_label || data.disc_name || 'disc';
        field(card, 'name').value = sanitizeName(suggested);
        fillPresetSelect(field(card, 'preset'), settings && settings.preset);
        field(card, 'preset-hint').textContent = presetDescription(field(card, 'preset'));
        field(card, 'container').value = (settings && settings.container) || 'mkv';
        updateNamePreview(card);
        renderTracks(card, state);
        field(card, 'setup').hidden = false;
    }

    function renderTitles(card, device, state) {
        const tbody = field(card, 'titles');
        tbody.textContent = '';
        state.titles.forEach(t => {
            const tr = document.createElement('tr');
            if (t.index === state.mainIndex) tr.classList.add('rip-title-main');

            const radioCell = document.createElement('td');
            const radio = document.createElement('input');
            radio.type = 'radio';
            radio.name = `title-${device}`;
            radio.value = t.index;
            radio.checked = t.index === state.selectedTitle;
            radio.addEventListener('change', () => {
                state.selectedTitle = t.index;
                renderTracks(card, state);
            });
            radioCell.appendChild(radio);
            tr.appendChild(radioCell);

            const cells = [
                t.index === state.mainIndex ? `${t.index} (main feature)` : String(t.index),
                t.duration,
                String(t.chapters),
                t.width && t.height
                    ? `${t.width}×${t.height}${t.interlaced ? 'i' : ''}${t.fps ? ` @ ${t.fps}` : ''}`
                    : '—',
                `${t.audio.length} audio, ${t.subtitles.length} sub`,
            ];
            cells.forEach(text => {
                const td = document.createElement('td');
                td.textContent = text;
                tr.appendChild(td);
            });
            // Clicking anywhere on the row selects it — the radio alone is a
            // tiny target on a phone.
            tr.addEventListener('click', (e) => {
                if (e.target !== radio) { radio.checked = true; radio.dispatchEvent(new Event('change')); }
            });
            tbody.appendChild(tr);
        });
    }

    function selectedTitle(state) {
        return state.titles.find(t => t.index === state.selectedTitle) || null;
    }

    function renderTracks(card, state) {
        const title = selectedTitle(state);
        const audioEl = field(card, 'audio');
        const subsEl = field(card, 'subtitles');
        audioEl.textContent = '';
        subsEl.textContent = '';
        if (!title) return;

        // Default to the first non-commentary track — a director's commentary
        // is almost never what you want as the main audio. Falls back to the
        // first track when every track is flagged as commentary.
        let defaultAudio = title.audio.findIndex(a => !a.commentary);
        if (defaultAudio < 0) defaultAudio = 0;
        title.audio.forEach((a, i) => {
            audioEl.appendChild(trackRow('audio', a.index, a.description, i === defaultAudio));
        });
        title.subtitles.forEach(s => {
            const label = s.language + (s.forced ? ' (forced)' : '');
            subsEl.appendChild(trackRow('sub', s.index, label, false));
        });
        if (!title.subtitles.length) {
            const p = document.createElement('p');
            p.className = 'field-hint';
            p.textContent = 'This title has no subtitle tracks.';
            subsEl.appendChild(p);
        }
    }

    function trackRow(kind, index, label, checked) {
        const wrap = document.createElement('label');
        wrap.className = 'rip-check';
        const box = document.createElement('input');
        box.type = 'checkbox';
        box.value = index;
        box.checked = checked;
        box.dataset.kind = kind;
        const span = document.createElement('span');
        span.textContent = label;
        wrap.appendChild(box);
        wrap.appendChild(span);
        return wrap;
    }

    function checkedValues(root, kind) {
        return Array.from(root.querySelectorAll(`input[data-kind="${kind}"]:checked`))
            .map(el => Number(el.value));
    }

    async function startRip(card, device) {
        const state = ui.get(device);
        if (!state) return;
        const title = selectedTitle(state);
        if (!title) {
            toast('Pick a title first', 'error');
            return;
        }
        const btn = card.querySelector('[data-action="start"]');
        btn.disabled = true;
        showError(errorEl, '');
        const body = {
            device,
            title: title.index,
            name: field(card, 'name').value,
            preset: field(card, 'preset').value,
            container: field(card, 'container').value,
            audio: checkedValues(field(card, 'audio'), 'audio'),
            subtitles: checkedValues(field(card, 'subtitles'), 'sub'),
            burn_subtitle: field(card, 'burn').checked,
            chapters: field(card, 'chapters').checked,
        };
        try {
            const res = await postJSON('/api/rip/start', body);
            toast(`Ripping to ${res.output_name}`);
            field(card, 'setup').hidden = true;
            historyDirty = true;
            schedule(POLL_ACTIVE_MS);
            refresh();
        } catch (err) {
            showError(errorEl, err.message);
            toast(err.message, 'error');
        } finally {
            btn.disabled = false;
        }
    }

    // ---------- settings form ----------

    function fillSettingsForm(s) {
        document.getElementById('rip-dir-input').value = s.output_dir || '';
        fillPresetSelect(document.getElementById('rip-default-preset'), s.preset);
        document.getElementById('rip-default-container').value = s.container || 'mkv';
        document.getElementById('rip-nice').value = s.nice;
        document.getElementById('rip-min-title').value = s.min_title_seconds;
        document.getElementById('rip-max-concurrent').value = s.max_concurrent;
        document.getElementById('rip-eject-done').checked = !!s.eject_when_done;
    }

    document.getElementById('rip-settings-save').addEventListener('click', async (e) => {
        const btn = e.currentTarget;
        btn.disabled = true;
        showError(document.getElementById('rip-settings-error'), '');
        try {
            const data = await postJSON('/api/rip/config', {
                output_dir: document.getElementById('rip-dir-input').value,
                preset: document.getElementById('rip-default-preset').value,
                container: document.getElementById('rip-default-container').value,
                nice: Number(document.getElementById('rip-nice').value),
                min_title_seconds: Number(document.getElementById('rip-min-title').value),
                max_concurrent: Number(document.getElementById('rip-max-concurrent').value),
                eject_when_done: document.getElementById('rip-eject-done').checked,
            });
            settings = data.settings;
            fillSettingsForm(settings);
            toast('Rip settings saved');
            refresh();
        } catch (err) {
            showError(document.getElementById('rip-settings-error'), err.message);
        } finally {
            btn.disabled = false;
        }
    });

    document.getElementById('rip-settings-reset').addEventListener('click', () => {
        if (settings) fillSettingsForm(settings);
        showError(document.getElementById('rip-settings-error'), '');
    });

    // ---------- history ----------

    async function loadHistory() {
        let data;
        try {
            data = await api('/api/rip/history');
        } catch (err) {
            return;
        }
        const rips = data.rips || [];
        historyEl.textContent = '';
        historyEmptyEl.hidden = rips.length > 0;
        rips.forEach(r => {
            const row = document.createElement('div');
            row.className = 'rip-history-row';
            row.dataset.status = r.status;
            if (r.output_path && !r.exists) row.classList.add('rip-history-gone');

            const name = document.createElement('div');
            name.className = 'rip-history-name';
            name.textContent = r.output_name || r.disc_label || '—';

            const meta = document.createElement('div');
            meta.className = 'rip-history-meta';
            const bits = [fmtWhen(r.finished_at)];
            if (r.status === 'done') {
                bits.push(fmtBytes(r.output_bytes));
                if (r.duration_seconds) bits.push(fmtDuration(r.duration_seconds));
            } else {
                bits.push(r.error_message || r.status);
            }
            if (r.preset) bits.push(r.preset);
            meta.textContent = bits.join(' · ');

            const badge = document.createElement('span');
            badge.className = 'rip-badge';
            badge.textContent = r.status;

            row.appendChild(name);
            row.appendChild(meta);
            row.appendChild(badge);
            historyEl.appendChild(row);
        });
    }

    // ---------- ripped files (right column) ----------

    const filesEl = document.getElementById('rip-files');
    const filesEmptyEl = document.getElementById('rip-files-empty');
    const filesErrorEl = document.getElementById('rip-files-error');
    const filesHintEl = document.getElementById('rip-files-hint');
    const fileTemplate = document.getElementById('rip-file-template');

    let mediaInfo = { configured: false, folders: [] };
    // path -> folder the user picked, so a poll doesn't reset the dropdown.
    const folderChoice = new Map();

    function fileCardFor(path) {
        let card = filesEl.querySelector(`.rip-file[data-path="${CSS.escape(path)}"]`);
        if (card) return card;
        card = fileTemplate.content.firstElementChild.cloneNode(true);
        card.dataset.path = path;
        wireFileCard(card, path);
        filesEl.appendChild(card);
        return card;
    }

    function wireFileCard(card, path) {
        field(card, 'folder').addEventListener('change', (e) => {
            folderChoice.set(path, e.currentTarget.value);
        });
        card.querySelector('[data-action="copy"]').addEventListener('click', async (e) => {
            const btn = e.currentTarget;
            btn.disabled = true;
            showError(filesErrorEl, '');
            try {
                const res = await postJSON('/api/rip/files/copy', {
                    path,
                    folder: field(card, 'folder').value,
                });
                toast(`Copying to ${res.folder}`);
                refreshFiles();
            } catch (err) {
                showError(filesErrorEl, err.message);
                toast(err.message, 'error');
            } finally {
                btn.disabled = false;
            }
        });
        card.querySelector('[data-action="stop"]').addEventListener('click', async (e) => {
            const btn = e.currentTarget;
            btn.disabled = true;
            try {
                await postJSON('/api/rip/files/copy/stop', { path });
                toast('Stopping copy…');
            } catch (err) {
                toast(err.message, 'error');
            } finally {
                btn.disabled = false;
                refreshFiles();
            }
        });

        // Deleting is irreversible, so it takes two clicks rather than a
        // modal — the second click within a few seconds does it.
        const removeBtn = card.querySelector('[data-action="remove"]');
        removeBtn.addEventListener('click', async () => {
            if (removeBtn.dataset.armed !== 'yes') {
                armRemove(removeBtn);
                return;
            }
            disarmRemove(removeBtn);
            removeBtn.disabled = true;
            showError(filesErrorEl, '');
            try {
                const res = await postJSON('/api/rip/files/delete', { path });
                toast(`Removed — ${fmtBytes(res.freed)} freed`);
                card.remove();
                refreshFiles();
            } catch (err) {
                showError(filesErrorEl, err.message);
                toast(err.message, 'error');
                removeBtn.disabled = false;
            }
        });
    }

    const REMOVE_ARM_MS = 4000;

    function armRemove(btn) {
        btn.dataset.armed = 'yes';
        btn.dataset.label = btn.textContent;
        btn.textContent = 'Really remove?';
        btn.classList.add('btn-danger');
        btn._armTimer = setTimeout(() => disarmRemove(btn), REMOVE_ARM_MS);
    }

    function disarmRemove(btn) {
        if (btn._armTimer) clearTimeout(btn._armTimer);
        if (btn.dataset.armed !== 'yes') return;
        delete btn.dataset.armed;
        btn.textContent = btn.dataset.label || 'Remove';
        btn.classList.remove('btn-danger');
    }

    function fillFolderSelect(select, path) {
        const wanted = folderChoice.get(path) || select.value;
        const current = Array.from(select.options).map(o => o.value).join('|');
        if (current !== mediaInfo.folders.join('|')) {
            select.textContent = '';
            mediaInfo.folders.forEach(name => {
                const opt = document.createElement('option');
                opt.value = name;
                opt.textContent = name;
                select.appendChild(opt);
            });
        }
        if (wanted && mediaInfo.folders.includes(wanted)) select.value = wanted;
    }

    // Copy states that shouldn't offer another Copy click.
    const COPY_BUSY = 'copying';

    function updateFileCard(card, f) {
        const copy = f.copy || {};
        const busy = !!copy.active || copy.status === COPY_BUSY;
        // Still being written by HandBrake — not a finished file yet.
        const ripping = f.ripping || null;

        field(card, 'name').textContent = f.name;
        field(card, 'size').textContent = fmtBytes(f.size);
        field(card, 'when').textContent = fmtWhen(f.modified);
        // "ripped <timestamp>" is meaningless while it's still being written;
        // the pill and progress bar carry the state instead.
        field(card, 'when').closest('.meta-item').hidden = !!ripping;

        setMeta(card, 'runtime', f.duration_seconds ? fmtDuration(f.duration_seconds) : null);
        setMeta(card, 'disc', f.disc_label);
        setMeta(card, 'preset', f.preset);
        setMeta(card, 'copyrate', busy ? copy.rate : null);
        setMeta(card, 'copyeta', busy && copy.eta_seconds != null
            ? fmtDuration(copy.eta_seconds) : null);

        const pill = field(card, 'pill');
        const label = ripping ? ripPill(ripping) : copyPill(copy, busy);
        pill.hidden = !label;
        if (label) {
            pill.textContent = label.text;
            // data-status on the card drives the accent bar, pill colour and
            // progress-fill colour through the shared torrent-card rules.
            card.dataset.status = label.state;
        } else {
            delete card.dataset.status;
        }

        const bar = field(card, 'progress');
        bar.hidden = !busy && !ripping;
        if (!bar.hidden) {
            const raw = ripping ? ripping.percent : copy.percent;
            field(card, 'fill').style.width = `${Math.min(100, Math.max(0, raw || 0))}%`;
        }

        const errBox = field(card, 'error-box');
        const failed = !ripping && ['error', 'interrupted'].includes(copy.status)
            && copy.error_message;
        errBox.hidden = !failed;
        if (failed) field(card, 'error-msg').textContent = copy.error_message;

        const select = field(card, 'folder');
        const copyBtn = card.querySelector('[data-action="copy"]');
        const stopBtn = card.querySelector('[data-action="stop"]');
        const removeBtn = card.querySelector('[data-action="remove"]');
        fillFolderSelect(select, f.path);
        select.disabled = busy || ripping || !mediaInfo.configured;
        copyBtn.hidden = busy;
        // Copying a file mid-encode ships a truncated movie — and a two-pass
        // encode rewrites it completely on pass 2, so even a 99% file is junk.
        copyBtn.disabled = !!ripping || !mediaInfo.configured || !mediaInfo.folders.length;
        copyBtn.title = ripping ? 'Still ripping — wait for the encode to finish' : '';
        // A file already on the server can still be re-sent (e.g. after
        // changing library folders) — rsync skips it if it's identical.
        copyBtn.textContent = copy.status === 'done' ? 'Copy again' : 'Copy';
        stopBtn.hidden = !busy;
        removeBtn.hidden = busy || !!ripping;
        if (removeBtn.hidden) disarmRemove(removeBtn);
    }

    function ripPill(ripping) {
        const p = ripping.pass_count > 1 && ripping.pass
            ? ` (pass ${ripping.pass}/${ripping.pass_count})`
            : '';
        return { text: `ripping${p}`, state: 'checking' };
    }

    function setMeta(card, name, value) {
        const el = card.querySelector(`[data-meta="${name}"]`);
        if (!el) return;
        el.hidden = !value;
        if (value) field(el, name === 'copyrate' ? 'rate'
            : name === 'copyeta' ? 'eta' : name).textContent = value;
    }

    // States map onto the torrent-card status vocabulary so the colours match
    // the Torrents page: blue in flight, green complete, grey stopped.
    function copyPill(copy, busy) {
        if (busy) return { text: 'copying', state: 'downloading' };
        switch (copy.status) {
            case 'done': return { text: 'on media server', state: 'seeding' };
            case 'error': return { text: 'copy failed', state: 'failed' };
            case 'cancelled': return { text: 'copy cancelled', state: 'paused' };
            case 'interrupted': return { text: 'copy interrupted', state: 'failed' };
            default: return null;
        }
    }

    async function refreshFiles() {
        let data;
        try {
            data = await api('/api/rip/files');
        } catch (err) {
            showError(filesErrorEl, err.message);
            return;
        }
        mediaInfo = data.media || { configured: false, folders: [] };
        if (!mediaInfo.configured) {
            filesHintEl.textContent = 'Everything in the output directory. Configure a '
                + 'media server in Settings to copy files to it.';
        } else {
            filesHintEl.textContent = `Everything in the output directory. Copy to `
                + `${mediaInfo.host} over SSH.`;
        }

        const files = data.files || [];
        filesEmptyEl.hidden = files.length > 0;
        const seen = new Set();
        files.forEach(f => {
            seen.add(f.path);
            updateFileCard(fileCardFor(f.path), f);
        });
        filesEl.querySelectorAll('.rip-file').forEach(card => {
            if (!seen.has(card.dataset.path)) card.remove();
        });
        // Keep the newest first, matching the API's order.
        files.forEach(f => filesEl.appendChild(fileCardFor(f.path)));

        return files.some(f => (f.copy || {}).active);
    }

    // ---------- polling ----------

    function schedule(ms) {
        if (pollTimer) clearTimeout(pollTimer);
        pollTimer = setTimeout(refresh, ms);
    }

    async function refresh() {
        let data;
        try {
            data = await api('/api/rip/overview');
        } catch (err) {
            showError(errorEl, err.message);
            schedule(POLL_IDLE_MS);
            return;
        }
        showError(errorEl, '');

        unavailableEl.hidden = data.available;
        mainEl.hidden = !data.available;
        if (!data.available) {
            document.getElementById('rip-cli-path').textContent = data.cli || 'HandBrakeCLI';
            schedule(POLL_IDLE_MS * 2);
            return;
        }

        if (!settings) {
            settings = data.settings;
            fillSettingsForm(settings);
        }
        document.getElementById('rip-output-dir').textContent = data.settings.output_dir;
        document.getElementById('rip-output-free').textContent = fmtBytes(data.output_dir_free);

        if (!data.output_dir_writable && !data.output_dir_creatable) {
            showError(dirWarningEl,
                `The output directory ${data.settings.output_dir} is not writable by the `
                + 'dashboard — rips will fail until it is fixed or changed below.');
        } else {
            showError(dirWarningEl, '');
        }

        const drives = data.drives || [];
        // An empty drive is just noise on this page — a machine can have two
        // (an internal bay plus a USB one) and only the loaded one is useful.
        const visible = drives.filter(hasSomethingToShow);
        noDrivesEl.hidden = visible.length > 0;
        if (!visible.length) {
            noDrivesEl.textContent = drives.length
                ? `No disc loaded. Insert one into ${drives.map(driveLabel).join(' or ')}.`
                : 'No optical drives found. Only /dev/sr* devices are detected — '
                  + 'if you just plugged in a USB drive, reload the page.';
        }
        const seen = new Set();
        visible.forEach(d => {
            seen.add(d.device);
            updateDrive(cardFor(d.device), d);
        });
        // A USB drive can be unplugged mid-session.
        drivesEl.querySelectorAll('.rip-drive').forEach(card => {
            if (!seen.has(card.dataset.device)) card.remove();
        });

        // The file list carries live copy progress, so it rides the same tick.
        const copying = await refreshFiles();

        const anyActive = drives.some(d => d.job && d.job.active)
            || drives.some(d => d.scan && d.scan.status === 'scanning');
        // Refresh the history list once each time everything goes quiet.
        if (!anyActive && historyDirty) {
            historyDirty = false;
            loadHistory();
        }
        if (anyActive) historyDirty = true;

        schedule(anyActive || copying ? POLL_ACTIVE_MS : POLL_IDLE_MS);
    }

    async function init() {
        try {
            const data = await api('/api/rip/presets');
            presetGroups = data.groups || [];
        } catch (err) {
            presetGroups = [];
        }
        await refresh();
        await loadHistory();
    }

    // Don't poll a backgrounded tab — an encode runs for an hour and nobody
    // is watching a hidden page.
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) {
            if (pollTimer) clearTimeout(pollTimer);
            pollTimer = null;
        } else {
            refresh();
        }
    });

    init();
})();
