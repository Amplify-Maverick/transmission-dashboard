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
 *   Charts.bars(el, items, opts)         vertical bars (histograms / daily)
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
    const padL = 58, padR = 14, padT = 12, padB = 22;
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
    const sx = t => xMax === xMin ? padL
      : padL + (t - xMin) / (xMax - xMin) * (W - padL - padR);
    const sy = v => H - padB - (v / vMax) * (H - padT - padB);

    const gridN = 3;
    let grid = '';
    for (let i = 0; i <= gridN; i++) {
      const gv = vMax * i / gridN;
      const y = sy(gv).toFixed(1);
      grid += `<line class="chart-grid" x1="${padL}" x2="${W - padR}" y1="${y}" y2="${y}"/>`;
      grid += `<text class="chart-axis" x="${padL - 8}" y="${(+y + 3.5).toFixed(1)}" text-anchor="end">${esc(fmtY(gv))}</text>`;
    }
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

  window.Charts = { sparkline, areaLines, barsH, bars, donut };
})();
