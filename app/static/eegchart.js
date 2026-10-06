/* Easy-to-read brainwave graph.
 *   Top panel    "Calm-focus meter": theta minus beta, relative to this session's own average.
 *   Bottom panel the five bands, each shown as above/below its own usual level, so they share one scale.
 * Phases (between your markers) are shaded, numbered dots are the top "windows", hover or touch for words.
 * All text from the data goes through textContent.
 */
(function (root) {
  'use strict';
  const NS = 'http://www.w3.org/2000/svg';
  const BAND_INFO = {
    Delta: { hz: '1-4 Hz', slot: 1, what: 'Slowest waves' },
    Theta: { hz: '4-8 Hz', slot: 2, what: 'Slow, dreamy waves' },
    Alpha: { hz: '8-13 Hz', slot: 3, what: 'Relaxed, idle waves' },
    Beta: { hz: '13-30 Hz', slot: 4, what: 'Busy, alert waves' },
    Gamma: { hz: '30-44 Hz', slot: 5, what: 'Fastest waves' }
  };
  const BAND_ORDER = ['Delta', 'Theta', 'Alpha', 'Beta', 'Gamma'];
  const DEFAULT_ON = { Delta: false, Theta: true, Alpha: true, Beta: true, Gamma: false };

  const el = (tag, attrs, text) => {
    const n = document.createElementNS(NS, tag);
    for (const k in (attrs || {})) n.setAttribute(k, attrs[k]);
    if (text != null) n.textContent = text;
    return n;
  };
  const h = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  const fmt = s => { s = Math.max(0, Math.round(s)); const hh = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), ss = s % 60;
    return (hh ? hh + ':' + String(m).padStart(2, '0') : String(m).padStart(2, '0')) + ':' + String(ss).padStart(2, '0'); };
  const sgn = v => (v >= 0 ? '+' : '−') + Math.abs(v).toFixed(1);

  function zscore(arr) {
    const v = arr.filter(x => x != null);
    if (v.length < 2) return arr.map(() => 0);
    const m = v.reduce((a, b) => a + b, 0) / v.length;
    const sd = Math.sqrt(v.reduce((a, b) => a + (b - m) * (b - m), 0) / v.length) || 1;
    return arr.map(x => (x == null ? null : (x - m) / sd));
  }

  function meterWords(z) {
    if (z == null) return 'no data';
    if (z >= 1) return 'Clearly more relaxed and inward than usual';
    if (z >= 0.4) return 'Somewhat more relaxed than usual';
    if (z > -0.4) return 'Close to your usual';
    if (z > -1) return 'Somewhat more alert than usual';
    return 'Clearly more alert and busy than usual';
  }

  function phasesOf(series) {
    const ev = series.events || [], dur = series.t[series.t.length - 1] || 0, out = [];
    const first = ev.length ? ev[0].t : dur;
    if (first > 2 || !ev.length) out.push({ start: 0, end: first || dur, label: ev.length ? 'before first marker' : 'whole recording' });
    ev.forEach((e, i) => out.push({ start: e.t, end: i + 1 < ev.length ? ev[i + 1].t : dur, label: e.label }));
    return out.filter(p => p.end > p.start);
  }

  function mount(host, series, opts) {
    opts = opts || {};
    host.textContent = '';
    if (!series || !series.t || series.t.length < 3) { host.appendChild(h('p', 'eegc-note', 'Not enough data to draw a graph.')); return null; }
    const T = series.t, n = T.length, dur = T[n - 1] || 1;
    const meter = series.rv_z;
    const bz = {}; BAND_ORDER.forEach(b => { if (series.bands[b]) bz[b] = zscore(series.bands[b]); });
    const on = Object.assign({}, DEFAULT_ON);
    const phases = phasesOf(series);
    // a gap is a jump of more than 5x the typical spacing: break the line there instead of joining across it
    const dts = []; for (let i = 1; i < n; i++) dts.push(T[i] - T[i - 1]); dts.sort((a, b) => a - b);
    const gapAt = (dts[Math.floor(dts.length / 2)] || 1) * 5;

    const root_ = h('div', 'eegc');
    const chips = h('div', 'eegc-chips'); chips.setAttribute('role', 'group'); chips.setAttribute('aria-label', 'Show or hide brainwave bands');
    const wrap = h('div'); wrap.style.position = 'relative';
    const tip = h('div', 'eegc-tip'); tip.setAttribute('aria-hidden', 'true');
    const tools = h('div', 'eegc-tools');
    const tblBtn = h('button', null, 'Show data table'); tblBtn.type = 'button';
    const tblHost = h('div', 'eegc-tbl'); tblHost.hidden = true;
    root_.append(chips, wrap, tools, tblHost);
    wrap.appendChild(tip); tools.append(h('span', 'eegc-note', 'Hover, touch or use the arrow keys to read values.'), tblBtn);
    host.appendChild(root_);

    BAND_ORDER.forEach(b => {
      if (!bz[b]) return;
      const info = BAND_INFO[b], c = h('button', 'eegc-chip'); c.type = 'button';
      c.style.color = `var(--s${info.slot})`; c.setAttribute('aria-pressed', String(on[b]));
      c.title = `${b} ${info.hz}: ${info.what}`;
      const sw = h('span', 'sw'); const name = h('span'); name.style.color = 'var(--ink)'; name.textContent = b;
      const hz = h('small', null, info.hz);
      c.append(sw, name, hz);
      c.addEventListener('click', () => { on[b] = !on[b]; c.setAttribute('aria-pressed', String(on[b])); draw(); });
      chips.appendChild(c);
    });

    let svg = null, cur = null;

    function pathFor(vals, X, Y) {
      let d = '', pen = false;
      for (let i = 0; i < n; i++) {
        const v = vals[i];
        if (v == null) { pen = false; continue; }
        if (i > 0 && T[i] - T[i - 1] > gapAt) pen = false;
        d += (pen ? 'L' : 'M') + X(T[i]).toFixed(1) + ' ' + Y(v).toFixed(1);
        pen = true;
      }
      return d;
    }

    function draw() {
      if (svg) svg.remove();
      const W = Math.max(300, Math.floor(wrap.clientWidth || host.clientWidth || 640));
      const small = W < 520;
      const L = small ? 38 : 50, R = 12, top = 46, mh = small ? 130 : 150, gap = 40, bh = small ? 170 : 200, bot = 26;
      const H = top + mh + gap + bh + bot;
      svg = el('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img', tabindex: '0',
        'aria-label': 'Brainwave graph with the calm-focus meter on top and the brainwave bands below. Use the arrow keys to move through time.' });
      wrap.insertBefore(svg, tip);
      const X = t => L + (t / dur) * (W - L - R);
      const mTop = top, mBot = top + mh, bTop = mBot + gap, bBot = bTop + bh;

      const mv = meter.filter(v => v != null);
      const mAbs = Math.max(2, Math.ceil(Math.max(...mv.map(Math.abs)) * 2) / 2);
      const YM = v => mTop + (1 - (v + mAbs) / (2 * mAbs)) * mh;
      const bAbs = 3, YB = v => bTop + (1 - (Math.max(-bAbs, Math.min(bAbs, v)) + bAbs) / (2 * bAbs)) * bh;

      // clip so shaded phases and lines stay inside their panel
      const defs = el('defs');
      const mkClip = (id, x, y, w, hh) => { const c = el('clipPath', { id }); c.appendChild(el('rect', { x, y, width: w, height: hh })); defs.appendChild(c); };
      const uid = 'c' + Math.random().toString(36).slice(2, 7);
      mkClip(uid + 'm', L, mTop, W - L - R, mh); mkClip(uid + 'b', L, bTop, W - L - R, bh);
      mkClip(uid + 'up', L, mTop, W - L - R, YM(0) - mTop); mkClip(uid + 'dn', L, YM(0), W - L - R, mBot - YM(0));
      mkClip(uid + 'ph', L, top - 18, W - L - R, 18);
      svg.appendChild(defs);

      // phases: tinted bands behind both panels, label at the top
      phases.forEach((p, i) => {
        const x0 = X(p.start), x1 = X(p.end);
        const fill = i % 2 === 0 ? 'var(--phase-a)' : 'var(--phase-b)';
        svg.appendChild(el('rect', { x: x0, y: mTop, width: x1 - x0, height: mh, fill }));
        svg.appendChild(el('rect', { x: x0, y: bTop, width: x1 - x0, height: bh, fill }));
        const room = Math.floor((x1 - x0 - 8) / 5.6);           // each label must fit inside its own phase
        if (room >= 3) {
          const lab = p.label.length > room ? p.label.slice(0, Math.max(1, room - 1)) + '\u2026' : p.label;
          const t = el('text', { x: x0 + 4, y: top - 8 }, lab);
          t.setAttribute('class', 'hint');
          svg.appendChild(t);
        }
      });

      // panel titles
      svg.appendChild(el('text', { x: L, y: 16, class: 'ttl' }, 'Calm-focus meter'));
      svg.appendChild(el('text', { x: L + 138, y: 16, class: 'hint' }, small ? 'vs session average' : 'compared with your session average'));

      // meter grid, zero line, fills, line
      [-mAbs, 0, mAbs].forEach(v => {
        if (v !== 0) svg.appendChild(el('line', { x1: L, x2: W - R, y1: YM(v), y2: YM(v), class: 'grid' }));
        svg.appendChild(el('text', { x: L - 6, y: YM(v) + 4, 'text-anchor': 'end' }, v === 0 ? 'avg' : sgn(v)));
      });
      svg.appendChild(el('text', { x: L + 4, y: mTop + 12, class: 'hint' }, '▲ relaxed, inward'));
      svg.appendChild(el('text', { x: L + 4, y: mBot - 4, class: 'hint' }, '▼ alert, busy'));
      const mPath = pathFor(meter, X, YM);
      const area = (() => { // close each drawn segment down to the zero line for the soft fill
        let d = '', pen = false, startT = 0, lastT = 0;
        for (let i = 0; i < n; i++) {
          const v = meter[i], brk = v == null || (i > 0 && T[i] - T[i - 1] > gapAt);
          if (brk && pen) { d += `L${X(lastT).toFixed(1)} ${YM(0)}L${X(startT).toFixed(1)} ${YM(0)}Z`; pen = false; }
          if (v == null) continue;
          if (!pen) { d += `M${X(T[i]).toFixed(1)} ${YM(0)}`; startT = T[i]; pen = true; }
          d += `L${X(T[i]).toFixed(1)} ${YM(v).toFixed(1)}`; lastT = T[i];
        }
        if (pen) d += `L${X(lastT).toFixed(1)} ${YM(0)}L${X(startT).toFixed(1)} ${YM(0)}Z`;
        return d;
      })();
      const gUp = el('g', { 'clip-path': `url(#${uid}up)` }), gDn = el('g', { 'clip-path': `url(#${uid}dn)` });
      gUp.appendChild(el('path', { d: area, fill: 'var(--pos)', opacity: 0.28 }));
      gDn.appendChild(el('path', { d: area, fill: 'var(--neg)', opacity: 0.28 }));
      svg.append(gUp, gDn);
      svg.appendChild(el('line', { x1: L, x2: W - R, y1: YM(0), y2: YM(0), class: 'zero' }));
      const gm = el('g', { 'clip-path': `url(#${uid}m)` }); gm.appendChild(el('path', { d: mPath, class: 'meter-line' })); svg.appendChild(gm);

      // bands panel
      svg.appendChild(el('text', { x: L, y: bTop - 12, class: 'ttl' }, 'Brainwave bands'));
      svg.appendChild(el('text', { x: L + 122, y: bTop - 12, class: 'hint' }, small ? "vs each band's usual" : "each band compared with its own usual level"));
      [-2, 0, 2].forEach(v => {
        svg.appendChild(el('line', { x1: L, x2: W - R, y1: YB(v), y2: YB(v), class: v === 0 ? 'zero' : 'grid' }));
        svg.appendChild(el('text', { x: L - 6, y: YB(v) + 4, 'text-anchor': 'end' }, v === 0 ? 'usual' : sgn(v)));
      });
      svg.appendChild(el('text', { x: L + 4, y: bTop + 12, class: 'hint' }, '▲ stronger than usual'));
      svg.appendChild(el('text', { x: L + 4, y: bBot - 4, class: 'hint' }, '▼ weaker than usual'));
      const gb = el('g', { 'clip-path': `url(#${uid}b)` });
      BAND_ORDER.forEach(b => {
        if (!bz[b] || !on[b]) return;
        gb.appendChild(el('path', { d: pathFor(bz[b], X, YB), class: 'line', stroke: `var(--s${BAND_INFO[b].slot})` }));
      });
      svg.appendChild(gb);

      // markers
      (series.events || []).forEach(e => {
        const x = X(e.t);
        svg.appendChild(el('line', { x1: x, x2: x, y1: mTop, y2: mBot, class: 'mark' }));
        svg.appendChild(el('line', { x1: x, x2: x, y1: bTop, y2: bBot, class: 'mark' }));
      });

      // windows: numbered dots on the meter
      (series.windows || []).forEach(w => {
        const g = el('g', { class: 'win' });
        const y = Math.max(mTop + 9, Math.min(mBot - 9, YM(w.rv_z)));
        g.appendChild(el('circle', { cx: X(w.t), cy: y, r: 9 }));
        g.appendChild(el('text', { x: X(w.t), y: y + 4 }, String(w.n)));
        svg.appendChild(g);
      });

      // time axis under the bands
      const steps = [5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];
      const step = steps.find(s => dur / s <= 8) || 3600;
      for (let t = 0; t <= dur + 0.01; t += step) {
        svg.appendChild(el('line', { x1: X(t), x2: X(t), y1: bBot, y2: bBot + 4, class: 'grid' }));
        svg.appendChild(el('text', { x: X(t), y: bBot + 18, 'text-anchor': 'middle' }, fmt(t)));
      }

      // hover layer
      const cross = el('line', { y1: mTop, y2: bBot, class: 'cross', visibility: 'hidden' });
      const dotM = el('circle', { r: 4, fill: 'var(--ink)', class: 'dotring', visibility: 'hidden' });
      const dots = {}; BAND_ORDER.forEach(b => { dots[b] = el('circle', { r: 4, class: 'dotring', fill: `var(--s${BAND_INFO[b].slot})`, visibility: 'hidden' }); });
      svg.append(cross, dotM, ...Object.values(dots));
      const hit = el('rect', { x: L, y: mTop, width: W - L - R, height: bBot - mTop, fill: 'transparent' });
      svg.appendChild(hit);

      function show(i) {
        i = Math.max(0, Math.min(n - 1, i)); cur = i;
        const x = X(T[i]);
        cross.setAttribute('x1', x); cross.setAttribute('x2', x); cross.setAttribute('visibility', 'visible');
        if (meter[i] != null) { dotM.setAttribute('cx', x); dotM.setAttribute('cy', YM(meter[i])); dotM.setAttribute('visibility', 'visible'); } else dotM.setAttribute('visibility', 'hidden');
        BAND_ORDER.forEach(b => {
          const d = dots[b];
          if (bz[b] && on[b] && bz[b][i] != null) { d.setAttribute('cx', x); d.setAttribute('cy', YB(bz[b][i])); d.setAttribute('visibility', 'visible'); } else d.setAttribute('visibility', 'hidden');
        });
        const ph = phases.filter(p => T[i] >= p.start).pop();
        tip.textContent = '';
        tip.appendChild(h('div', 'h', fmt(T[i]) + (ph ? ' · ' + ph.label : '')));
        const r0 = h('div', 'r'); const k0 = h('span', 'k'); k0.style.borderColor = 'var(--ink)';
        const b0 = h('b', null, meter[i] == null ? '–' : sgn(meter[i])); r0.append(k0, b0, h('span', 'n', meterWords(meter[i])));
        tip.appendChild(r0);
        BAND_ORDER.forEach(b => {
          if (!bz[b] || !on[b] || bz[b][i] == null) return;
          const r = h('div', 'r'), k = h('span', 'k'); k.style.borderColor = `var(--s${BAND_INFO[b].slot})`;
          const v = bz[b][i];
          r.append(k, h('b', null, sgn(v)), h('span', 'n', b + (v > 0.4 ? ' above usual' : v < -0.4 ? ' below usual' : ' near usual')));
          tip.appendChild(r);
        });
        tip.style.display = 'block';
        const tw = tip.offsetWidth, left = x + 14 + tw > W ? x - tw - 14 : x + 14;
        tip.style.left = Math.max(0, left) + 'px'; tip.style.top = (mTop + 6) + 'px';
      }
      const idxAt = clientX => {
        const r = svg.getBoundingClientRect(), x = (clientX - r.left) * (W / r.width), t = Math.max(0, Math.min(dur, (x - L) / (W - L - R) * dur));
        let lo = 0, hi = n - 1; while (hi - lo > 1) { const m = (lo + hi) >> 1; if (T[m] < t) lo = m; else hi = m; }
        return Math.abs(T[lo] - t) <= Math.abs(T[hi] - t) ? lo : hi;
      };
      hit.addEventListener('pointermove', e => show(idxAt(e.clientX)));
      hit.addEventListener('pointerdown', e => show(idxAt(e.clientX)));
      svg.addEventListener('pointerleave', () => { if (document.activeElement !== svg) hideTip(); });
      svg.addEventListener('keydown', e => {
        const big = Math.max(1, Math.round(n / 20));
        if (e.key === 'ArrowRight') { show((cur == null ? 0 : cur) + (e.shiftKey ? big : 1)); e.preventDefault(); }
        else if (e.key === 'ArrowLeft') { show((cur == null ? 0 : cur) - (e.shiftKey ? big : 1)); e.preventDefault(); }
        else if (e.key === 'Home') { show(0); e.preventDefault(); } else if (e.key === 'End') { show(n - 1); e.preventDefault(); }
        else if (e.key === 'Escape') hideTip();
      });
      svg.addEventListener('blur', hideTip);
      function hideTip() { tip.style.display = 'none'; cross.setAttribute('visibility', 'hidden'); dotM.setAttribute('visibility', 'hidden'); Object.values(dots).forEach(d => d.setAttribute('visibility', 'hidden')); }
      if (cur != null && document.activeElement === svg) show(cur);
    }

    // --- table view (phase averages) so nothing depends on hovering ---
    function buildTable() {
      tblHost.textContent = '';
      const t = h('table'), thead = h('thead'), hr = h('tr');
      ['Phase', 'Starts', 'Length', 'Meter'].concat(BAND_ORDER.filter(b => bz[b])).forEach(c => hr.appendChild(h('th', null, c)));
      thead.appendChild(hr); t.appendChild(thead);
      const tb = h('tbody');
      phases.forEach(p => {
        const idx = []; for (let i = 0; i < n; i++) if (T[i] >= p.start && T[i] < p.end + 1e-6) idx.push(i);
        const avg = a => { const v = idx.map(i => a[i]).filter(x => x != null); return v.length ? sgn(v.reduce((x, y) => x + y, 0) / v.length) : '–'; };
        const r = h('tr');
        [p.label, fmt(p.start), fmt(p.end - p.start), avg(meter)].concat(BAND_ORDER.filter(b => bz[b]).map(b => avg(bz[b]))).forEach(c => r.appendChild(h('td', null, c)));
        tb.appendChild(r);
      });
      t.appendChild(tb); tblHost.appendChild(t);
      tblHost.appendChild(h('p', 'eegc-note', 'Values are averages in units of how far above (+) or below (−) that line\'s own usual level the session was. The meter is theta minus beta.'));
    }
    buildTable();
    tblBtn.addEventListener('click', () => { tblHost.hidden = !tblHost.hidden; tblBtn.textContent = tblHost.hidden ? 'Show data table' : 'Hide data table'; });

    if (series.flags && series.flags.length) {
      const ul = h('ul', 'eegc-flags');
      series.flags.forEach(f => ul.appendChild(h('li', null, f)));
      root_.insertBefore(ul, tools);
    }

    draw();
    let raf = 0, lastW = wrap.clientWidth;
    if (root.ResizeObserver) new ResizeObserver(() => { if (Math.abs(wrap.clientWidth - lastW) < 2) return; lastW = wrap.clientWidth; cancelAnimationFrame(raf); raf = requestAnimationFrame(draw); }).observe(wrap);
    return { redraw: draw };
  }

  async function mountFromUrl(host, url, pick) {
    const r = await fetch(url, { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const j = await r.json();
    const s = pick ? pick(j) : j;
    return { data: j, chart: s ? mount(host, s) : null };
  }

  root.EEGChart = { mount, mountFromUrl, BAND_INFO };
})(window);
