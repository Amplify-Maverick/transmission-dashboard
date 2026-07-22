/*
 * charts.js — tiny dependency-free inline-SVG charts for the System page.
 *
 * No build step, no external library: every chart is an SVG string rendered
 * into a container element, colored from the dashboard's CSS design tokens.
 * Charts use a fixed viewBox coordinate space and scale to their container
 * width via CSS (width:100%), so they stay crisp and responsive on mobile.
 *
 * Public API (all on the global `Charts`):
 *   Charts.sparkline(el, points, opts)   line + soft fill, no axes
 *   Charts.areaLines(el, series, opts)   multi-series time chart w/ hover
 *   Charts.barsH(el, items, opts)        horizontal ranked bars
 *   Charts.bars(el, items, opts)         vertical bars (histograms)
 *   Charts.barsTime(el, items, opts)     daily bars w/ y axis + hover readout
 *   Charts.donut(el, segments, opts)     ring for categorical mix
 *
 * `points` is an array of {t, v} (t = epoch ms or any monotonic x, v = value).
 * Colors are passed as CSS custom-property names ('--blue') and resolved once.
 */
(function () {
  'use strict';

  const NS = 'http://www.w3.org/2000/svg';
  const VBW = 640; // viewBox width — charts scale to container width from here

  const _varCache = {};
  function cssVar(name) {
    if (name && name[0] !== '-') return name; // already a literal color
    if (_varCache[name]) return _varCache[name];
    const v = getComputedStyle(document.documentElement)
      .getPropertyValue(name).trim() || '#8b949e';
    _varCache[name] = v;
    return v;
  }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  const reduceMotion = window.matchMedia
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // "Nice" upper bound so the top gridline lands on a round number.
  function niceMax(v) {
    if (!isFinite(v) || v <= 0) return 1;
    const exp = Math.floor(Math.log10(v));
    const base = Math.pow(10, exp);
    const f = v / base;
    const step = f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10;
    return step * base;
  }

  function clearEl(el) { while (el.firstChild) el.removeChild(el.firstChild); }

  // ---- shared y-axis furniture for the axis-bearing charts ----
  // Size the left gutter to the widest y label instead of assuming one fits in
  // a fixed 58px — "8.54 MB/s" doesn't, and the overflow is silently clipped at
  // the SVG edge rather than pushing the plot over. .chart-axis is 10px
  // monospace, so ~6px per character.
  function yGutter(vMax, gridN, fmtY) {
    const labels = [];
    for (let i = 0; i <= gridN; i++) labels.push(String(fmtY(vMax * i / gridN)));
    const widest = labels.reduce((a, s) => Math.max(a, s.length), 0);
    return { labels, padL: Math.min(96, Math.max(34, Math.ceil(widest * 6.1) + 12)) };
  }

  function yGrid(gut, vMax, gridN, sy, padL, xRight) {
    let g = '';
    for (let i = 0; i <= gridN; i++) {
      const y = sy(vMax * i / gridN).toFixed(1);
      g += `<line class="chart-grid" x1="${padL}" x2="${xRight}" y1="${y}" y2="${y}"/>`;
      g += `<text class="chart-axis" x="${padL - 8}" y="${(+y + 3.5).toFixed(1)}" ` +
        `text-anchor="end">${esc(gut.labels[i])}</text>`;
    }
    return g;
  }

  // Bar with a flat baseline and rounded top corners. `rx` on a <rect> rounds
  // all four, which detaches the bar from the axis; only the data end rounds.
  function barPath(x, y, w, h, r) {
    r = Math.min(r, w / 2, h);
    const b = y + h;
    if (r <= 0) return `M${x} ${y}H${(x + w).toFixed(1)}V${b.toFixed(1)}H${x}Z`;
    return `M${x} ${b.toFixed(1)}V${(y + r).toFixed(1)}` +
      `Q${x} ${y.toFixed(1)} ${(x + r).toFixed(1)} ${y.toFixed(1)}` +
      `H${(x + w - r).toFixed(1)}` +
      `Q${(x + w).toFixed(1)} ${y.toFixed(1)} ${(x + w).toFixed(1)} ${(y + r).toFixed(1)}` +
      `V${b.toFixed(1)}Z`;
  }

  // ---- sparkline: single series, soft area fill, no axes ----
  function sparkline(el, points, opts) {
    opts = opts || {};
    const H = opts.height || 44;
    const color = cssVar(opts.color || '--blue');
    const pad = 3;
    clearEl(el);
    const pts = (points || []).filter(p => p && p.v != null && isFinite(p.v));
    if (pts.length < 2) { el.dataset.empty = '1'; return; }
    delete el.dataset.empty;
    const xs = pts.map(p => p.t);
    const vs = pts.map(p => p.v);
    const xMin = Math.min(...xs), xMax = Math.max(...xs);
    const vMax = Math.max(niceMax(Math.max(...vs)), 1);
    const sx = t => xMax === xMin ? VBW - pad
      : pad + (t - xMin) / (xMax - xMin) * (VBW - 2 * pad);
    const sy = v => H - pad - (v / vMax) * (H - 2 * pad);
    let d = '';
    pts.forEach((p, i) => { d += (i ? 'L' : 'M') + sx(p.t).toFixed(1) + ' ' + sy(p.v).toFixed(1); });
    const area = d + `L${sx(xMax).toFixed(1)} ${H - pad}L${sx(xMin).toFixed(1)} ${H - pad}Z`;
    const gid = 'sg' + Math.random().toString(36).slice(2, 8);
    el.innerHTML =
      `<svg viewBox="0 0 ${VBW} ${H}" preserveAspectRatio="none" class="chart-svg spark" role="img">` +
      `<defs><linearGradient id="${gid}" x1="0" x2="0" y1="0" y2="1">` +
      `<stop offset="0" stop-color="${color}" stop-opacity="0.28"/>` +
      `<stop offset="1" stop-color="${color}" stop-opacity="0"/></linearGradient></defs>` +
      `<path d="${area}" fill="url(#${gid})"/>` +
      `<path d="${d}" fill="none" stroke="${color}" stroke-width="2" ` +
      `vector-effect="non-scaling-stroke" stroke-linejoin="round" stroke-linecap="round"/>` +
      `</svg>`;
  }

  // ---- areaLines: multi-series time chart with axes + hover crosshair ----
  // series: [{ name, color:'--blue', points:[{t,v}] }]
  function areaLines(el, series, opts) {
    opts = opts || {};
    const H = opts.height || 200;
    // Render in the element's actual pixel space (1:1) rather than a fixed
    // viewBox stretched with preserveAspectRatio="none" — stretching distorts
    // and clips the axis text. Falls back to VBW when the element isn't laid
    // out yet (e.g. a hidden tab); the next poll re-renders at real width.
    const W = Math.max(320, Math.round(el.clientWidth) || VBW);
    const padR = 14, padT = 12, padB = 22;
    const fmtY = opts.fmtY || (v => String(Math.round(v)));
    const fmtX = opts.fmtX || (t => '');
    const fmtTip = opts.fmtTip || fmtY;
    clearEl(el);

    const active = (series || []).map(s => ({
      name: s.name,
      color: cssVar(s.color),
      points: (s.points || []).filter(p => p && p.v != null && isFinite(p.v)),
    })).filter(s => s.points.length >= 1);
    const haveData = active.some(s => s.points.length >= 2);
    if (!haveData) {
      el.innerHTML = `<div class="chart-empty">Collecting data…</div>`;
      return;
    }

    let xMin = Infinity, xMax = -Infinity, vMax = 0;
    active.forEach(s => s.points.forEach(p => {
      if (p.t < xMin) xMin = p.t; if (p.t > xMax) xMax = p.t;
      if (p.v > vMax) vMax = p.v;
    }));
    vMax = Math.max(niceMax(vMax), opts.minYMax || 1);

    const gridN = 3;
    const gut = yGutter(vMax, gridN, fmtY);
    const padL = gut.padL;

    const sx = t => xMax === xMin ? padL
      : padL + (t - xMin) / (xMax - xMin) * (W - padL - padR);
    const sy = v => H - padB - (v / vMax) * (H - padT - padB);

    const grid = yGrid(gut, vMax, gridN, sy, padL, W - padR);
    // x labels: first, middle, last
    let xlab = '';
    if (fmtX) {
      [xMin, (xMin + xMax) / 2, xMax].forEach((t, i) => {
        const lx = sx(t);
        const anchor = i === 0 ? 'start' : i === 2 ? 'end' : 'middle';
        const txt = fmtX(t);
        if (txt) xlab += `<text class="chart-axis" x="${lx.toFixed(1)}" y="${H - 6}" text-anchor="${anchor}">${esc(txt)}</text>`;
      });
    }

    // Area fills read well for two or three series but turn to mud once
    // several overlap, so callers plotting many series pass fill:false and
    // get bare lines.
    const fill = opts.fill !== false;
    let paths = '';
    const defs = [];
    active.forEach((s, si) => {
      let d = '';
      s.points.forEach((p, i) => { d += (i ? 'L' : 'M') + sx(p.t).toFixed(1) + ' ' + sy(p.v).toFixed(1); });
      if (fill) {
        const gid = 'ag' + si + Math.random().toString(36).slice(2, 6);
        defs.push(`<linearGradient id="${gid}" x1="0" x2="0" y1="0" y2="1">` +
          `<stop offset="0" stop-color="${s.color}" stop-opacity="0.22"/>` +
          `<stop offset="1" stop-color="${s.color}" stop-opacity="0"/></linearGradient>`);
        const lastX = sx(s.points[s.points.length - 1].t).toFixed(1);
        const area = d + `L${lastX} ${sy(0).toFixed(1)}L${sx(s.points[0].t).toFixed(1)} ${sy(0).toFixed(1)}Z`;
        paths += `<path d="${area}" fill="url(#${gid})"/>`;
      }
      paths += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="2" ` +
        `vector-effect="non-scaling-stroke" stroke-linejoin="round" stroke-linecap="round"/>`;
    });

    const svg =
      `<svg viewBox="0 0 ${W} ${H}" class="chart-svg" role="img" ` +
      `aria-label="${esc(active.map(s => s.name).join(', '))} over time">` +
      `<defs>${defs.join('')}</defs>${grid}${paths}${xlab}` +
      `<line class="chart-cross" x1="0" x2="0" y1="${padT}" y2="${H - padB}" style="display:none"/>` +
      `</svg>`;

    // legend (>=2 series) — identity is never color-alone
    let legend = '';
    if (active.length >= 2) {
      legend = `<div class="chart-legend">` + active.map(s =>
        `<span class="chart-legend-item"><span class="chart-swatch" style="background:${s.color}"></span>${esc(s.name)}</span>`
      ).join('') + `</div>`;
    }
    el.innerHTML = `<div class="chart-plot">${svg}<div class="chart-tip" hidden></div></div>${legend}`;

    // ---- hover crosshair + tooltip ----
    const svgEl = el.querySelector('svg');
    const cross = el.querySelector('.chart-cross');
    const tip = el.querySelector('.chart-tip');
    const plot = el.querySelector('.chart-plot');
    function nearestIndex(pts, t) {
      let lo = 0, hi = pts.length - 1;
      while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (pts[mid].t < t) lo = mid + 1; else hi = mid;
      }
      if (lo > 0 && Math.abs(pts[lo - 1].t - t) < Math.abs(pts[lo].t - t)) lo--;
      return lo;
    }
    function onMove(ev) {
      const rect = svgEl.getBoundingClientRect();
      const clientX = (ev.touches ? ev.touches[0].clientX : ev.clientX);
      const rel = (clientX - rect.left) / rect.width;      // 0..1 across svg
      const vbx = rel * W;
      const frac = Math.max(0, Math.min(1, (vbx - padL) / (W - padL - padR)));
      const t = xMin + frac * (xMax - xMin);
      cross.setAttribute('x1', vbx.toFixed(1));
      cross.setAttribute('x2', vbx.toFixed(1));
      cross.style.display = '';
      let rows = '';
      active.forEach(s => {
        const idx = nearestIndex(s.points, t);
        const p = s.points[idx];
        if (!p) return;
        rows += `<div class="chart-tip-row"><span class="chart-swatch" style="background:${s.color}"></span>` +
          `<span class="chart-tip-name">${esc(s.name)}</span>` +
          `<span class="chart-tip-val">${esc(fmtTip(p.v))}</span></div>`;
      });
      const near = nearestIndex(active[0].points, t);
      const label = fmtX ? fmtX(active[0].points[near].t) : '';
      tip.innerHTML = (label ? `<div class="chart-tip-x">${esc(label)}</div>` : '') + rows;
      tip.hidden = false;
      const pr = plot.getBoundingClientRect();
      let left = clientX - pr.left + 10;
      if (left + 140 > pr.width) left = clientX - pr.left - 150;
      tip.style.left = Math.max(0, left) + 'px';
    }
    function onLeave() { cross.style.display = 'none'; tip.hidden = true; }
    plot.addEventListener('pointermove', onMove);
    plot.addEventListener('pointerleave', onLeave);
    plot.addEventListener('touchmove', onMove, { passive: true });
    plot.addEventListener('touchend', onLeave);
  }

  // ---- barsH: horizontal ranked bars with value labels ----
  // items: [{ label, value, color? }]
  function barsH(el, items, opts) {
    opts = opts || {};
    const fmtV = opts.fmtValue || (v => String(v));
    const color = cssVar(opts.color || '--blue');
    clearEl(el);
    const rows = (items || []).filter(d => d && isFinite(d.value));
    if (!rows.length) { el.innerHTML = `<div class="chart-empty">No data</div>`; return; }
    const max = Math.max(...rows.map(d => d.value), 1);
    el.innerHTML = `<div class="barh-list">` + rows.map(d => {
      const pct = Math.max(1, (d.value / max) * 100);
      const c = d.color ? cssVar(d.color) : color;
      return `<div class="barh-row" title="${esc(d.label)} — ${esc(fmtV(d.value))}">` +
        `<span class="barh-label">${esc(d.label)}</span>` +
        `<span class="barh-track"><span class="barh-fill${reduceMotion ? ' no-anim' : ''}" style="width:${pct.toFixed(1)}%;background:${c}"></span></span>` +
        `<span class="barh-value">${esc(fmtV(d.value))}</span></div>`;
    }).join('') + `</div>`;
  }

  // ---- bars: vertical bars (histograms, daily totals) ----
  // items: [{ label, value }]
  function bars(el, items, opts) {
    opts = opts || {};
    const fmtV = opts.fmtValue || (v => String(v));
    const color = cssVar(opts.color || '--blue');
    clearEl(el);
    const rows = (items || []).filter(d => d && isFinite(d.value));
    if (!rows.length) { el.innerHTML = `<div class="chart-empty">No data</div>`; return; }
    const max = Math.max(...rows.map(d => d.value), 1);
    // Thin x labels so a long series (e.g. 30 daily bars) doesn't collide —
    // every bar keeps its full label as a hover tooltip regardless.
    const stride = Math.max(1, Math.ceil(rows.length / 8));
    el.innerHTML = `<div class="barv-list">` + rows.map((d, i) => {
      const h = d.value > 0 ? Math.max(2, (d.value / max) * 100) : 0;
      const lbl = (i % stride === 0) ? esc(d.label) : '';
      return `<div class="barv-col" title="${esc(d.label)} — ${esc(fmtV(d.value))}">` +
        `<span class="barv-track"><span class="barv-fill${reduceMotion ? ' no-anim' : ''}" style="height:${h.toFixed(1)}%;background:${color}"></span></span>` +
        `<span class="barv-label">${lbl}</span></div>`;
    }).join('') + `</div>`;
  }

  // ---- barsTime: daily bars on a real value axis, with hover ----
  // items: [{ label, value }] — one entry per period, already gap-filled by the
  // caller so even spacing means even time. Unlike bars() this carries a y axis
  // and a tooltip, because a plain silhouette answers "which day was biggest"
  // but not "was that 900 MB or 90 GB".
  function barsTime(el, items, opts) {
    opts = opts || {};
    const H = opts.height || 190;
    const color = cssVar(opts.color || '--blue');
    const fmtV = opts.fmtValue || (v => String(v));
    const fmtX = opts.fmtX || (d => d.label);
    const padR = 10, padT = 20, padB = 22;
    clearEl(el);

    const rows = (items || []).filter(d => d && isFinite(d.value));
    const total = rows.reduce((a, d) => a + d.value, 0);
    if (!rows.length || total <= 0) {
      el.innerHTML = `<div class="chart-empty">${esc(opts.emptyText || 'No data')}</div>`;
      return;
    }
    // Match areaLines: render 1:1 in the element's pixel space, falling back to
    // VBW while the tab is still hidden — the next poll re-renders at real width.
    const W = Math.max(320, Math.round(el.clientWidth) || VBW);

    const gridN = 3;
    const peak = rows.reduce((a, d) => d.value > a.value ? d : a, rows[0]);
    // niceMax rounds base-10 on the raw number, which is meaningless once the
    // axis is labelled in binary units — a 61 GB peak becomes a "93.1 GB" top.
    // Callers with a unit pass their own rounder.
    const vMax = Math.max(
      opts.niceMax ? opts.niceMax(peak.value, gridN) : niceMax(peak.value),
      opts.minYMax || 1);
    const gut = yGutter(vMax, gridN, fmtV);
    const padL = gut.padL;
    const sy = v => H - padB - (v / vMax) * (H - padT - padB);
    const grid = yGrid(gut, vMax, gridN, sy, padL, W - padR);

    const slot = (W - padL - padR) / rows.length;
    const gap = slot > 6 ? 2 : 1;            // 2px surface gap between bars
    const bw = Math.max(1, slot - gap);
    const base = sy(0);

    let marks = '';
    rows.forEach((d, i) => {
      if (d.value <= 0) return;              // an idle day is a gap, not a stub
      const y = Math.min(sy(d.value), base - 2);   // keep tiny days visible
      marks += `<path class="barT" data-i="${i}" d="${barPath(padL + i * slot + gap / 2, y, bw, base - y, 4)}" fill="${color}"/>`;
    });

    // Direct-label the peak only: one anchored number means the chart reads
    // without hover, which is the only mode iOS Safari actually has.
    const pi = rows.indexOf(peak);
    const ptxt = fmtV(peak.value);
    const phalf = ptxt.length * 3;
    const px = Math.max(padL + phalf,
      Math.min(W - padR - phalf, padL + pi * slot + slot / 2));
    const peakLabel = `<text class="chart-peak" x="${px.toFixed(1)}" ` +
      `y="${(sy(peak.value) - 6).toFixed(1)}" text-anchor="middle">${esc(ptxt)}</text>`;

    // Thin x labels so 30 dates don't collide, each sitting under its own bar.
    // Budget by pixels, not row count: the same stride that's airy at 1200px
    // overlaps at phone width.
    const TICK_W = 52;                                  // "07-14" at 10px mono + air
    const avail = W - padL - padR;
    const maxTicks = Math.max(2, Math.min(7, Math.floor(avail / TICK_W)));
    const stride = Math.max(1, Math.ceil(rows.length / maxTicks));
    const last = rows.length - 1;
    const cxOf = i => padL + i * slot + slot / 2;
    let xlab = '';
    rows.forEach((d, i) => {
      if (i !== last && i % stride !== 0) return;
      // The last date always shows, so drop any tick that would crowd it.
      if (i !== last && cxOf(last) - cxOf(i) < TICK_W) return;
      const txt = fmtX(d, i);
      if (!txt) return;
      const half = txt.length * 3;
      const cx = Math.max(padL + half, Math.min(W - padR - half, cxOf(i)));
      xlab += `<text class="chart-axis" x="${cx.toFixed(1)}" y="${H - 6}" text-anchor="middle">${esc(txt)}</text>`;
    });

    const svg =
      `<svg viewBox="0 0 ${W} ${H}" class="chart-svg" role="img" ` +
      `aria-label="${esc(opts.label || 'Daily totals')}: peak ${esc(fmtV(peak.value))} on ${esc(peak.label)}">` +
      `${grid}<rect class="chart-band" x="0" y="${padT}" width="0" height="${H - padT - padB}" style="display:none"/>` +
      `${marks}${peakLabel}${xlab}</svg>`;
    el.innerHTML = `<div class="chart-plot">${svg}<div class="chart-tip" hidden></div></div>`;

    // ---- hover / touch readout ----
    // A full-height band per slot, not the bar itself: a 3px bar is an
    // impossible hit target, and idle days need a readout too.
    const svgEl = el.querySelector('svg');
    const band = el.querySelector('.chart-band');
    const tip = el.querySelector('.chart-tip');
    const plot = el.querySelector('.chart-plot');
    let lastI = -1;
    function onMove(ev) {
      const rect = svgEl.getBoundingClientRect();
      const clientX = (ev.touches ? ev.touches[0].clientX : ev.clientX);
      const vbx = (clientX - rect.left) / rect.width * W;
      const i = Math.max(0, Math.min(rows.length - 1, Math.floor((vbx - padL) / slot)));
      if (i !== lastI) {
        const prev = el.querySelector('.barT.is-hot');
        if (prev) prev.classList.remove('is-hot');
        const hot = el.querySelector(`.barT[data-i="${i}"]`);
        if (hot) hot.classList.add('is-hot');
        band.setAttribute('x', (padL + i * slot).toFixed(1));
        band.setAttribute('width', slot.toFixed(1));
        band.style.display = '';
        const d = rows[i];
        tip.innerHTML = `<div class="chart-tip-x">${esc(d.label)}</div>` +
          `<div class="chart-tip-row"><span class="chart-swatch" style="background:${color}"></span>` +
          `<span class="chart-tip-val">${esc(fmtV(d.value))}</span></div>` +
          (d.sub ? `<div class="chart-tip-sub">${esc(d.sub)}</div>` : '');
        tip.hidden = false;
        lastI = i;
      }
      const pr = plot.getBoundingClientRect();
      let left = clientX - pr.left + 10;
      if (left + 140 > pr.width) left = clientX - pr.left - 150;
      tip.style.left = Math.max(0, left) + 'px';
    }
    function onLeave() {
      const prev = el.querySelector('.barT.is-hot');
      if (prev) prev.classList.remove('is-hot');
      band.style.display = 'none';
      tip.hidden = true;
      lastI = -1;
    }
    plot.addEventListener('pointermove', onMove);
    plot.addEventListener('pointerleave', onLeave);
    plot.addEventListener('touchmove', onMove, { passive: true });
    plot.addEventListener('touchend', onLeave);
  }

  // ---- donut: categorical mix ----
  // segments: [{ label, value, color:'--green' }]
  function donut(el, segments, opts) {
    opts = opts || {};
    const size = opts.size || 132, sw = opts.stroke || 16;
    const r = (size - sw) / 2, cx = size / 2, cy = size / 2, C = 2 * Math.PI * r;
    clearEl(el);
    const segs = (segments || []).filter(s => s && s.value > 0);
    const total = segs.reduce((a, s) => a + s.value, 0);
    const fmtV = opts.fmtValue || (v => String(v));
    if (!total) { el.innerHTML = `<div class="chart-empty">No data</div>`; return; }
    let off = 0, arcs = '';
    segs.forEach(s => {
      const frac = s.value / total;
      const len = frac * C;
      const c = cssVar(s.color);
      // 2px surface gap between segments
      const gap = segs.length > 1 ? 2 : 0;
      arcs += `<circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${c}" ` +
        `stroke-width="${sw}" stroke-dasharray="${Math.max(0, len - gap).toFixed(2)} ${(C - len + gap).toFixed(2)}" ` +
        `stroke-dashoffset="${(-off).toFixed(2)}" transform="rotate(-90 ${cx} ${cy})">` +
        `<title>${esc(s.label)} — ${esc(fmtV(s.value))}</title></circle>`;
      off += len;
    });
    const center = opts.centerLabel != null
      ? `<text class="donut-center" x="${cx}" y="${cy + 1}" text-anchor="middle" dominant-baseline="middle">${esc(opts.centerLabel)}</text>` +
        (opts.centerSub ? `<text class="donut-sub" x="${cx}" y="${cy + 16}" text-anchor="middle">${esc(opts.centerSub)}</text>` : '')
      : '';
    const legend = `<div class="chart-legend donut-legend">` + segs.map(s =>
      `<span class="chart-legend-item"><span class="chart-swatch" style="background:${cssVar(s.color)}"></span>` +
      `${esc(s.label)} <span class="chart-legend-val">${esc(fmtV(s.value))}</span></span>`
    ).join('') + `</div>`;
    el.innerHTML = `<div class="donut-wrap"><svg viewBox="0 0 ${size} ${size}" class="chart-svg donut" role="img" ` +
      `aria-label="${esc(segs.map(s => s.label + ' ' + fmtV(s.value)).join(', '))}">${arcs}${center}</svg>${legend}</div>`;
  }

  window.Charts = { sparkline, areaLines, barsH, bars, barsTime, donut };
})();
