/* Muse headband capture over Web Bluetooth.
 *
 * Streams the four EEG channels, computes band powers once a second and records them in the same
 * column layout as a Muse Monitor CSV (log10 band power per sensor, contact quality, marker rows),
 * so the server can analyze it exactly like an uploaded session.
 *
 * Needs Chrome (Android or desktop) on an HTTPS page or localhost. Based on the public Muse BLE protocol;
 * only tested here against a simulated headband, so treat real-hardware results as experimental.
 */
(function (root) {
  'use strict';

  const SERVICE = 0xfe8d;
  const CHAR_CONTROL = '273e0001-4c4d-454d-96be-f03bac821358';
  const CHAR_TELEMETRY = '273e000b-4c4d-454d-96be-f03bac821358';
  const EEG_CHARS = [
    '273e0003-4c4d-454d-96be-f03bac821358', // TP9
    '273e0004-4c4d-454d-96be-f03bac821358', // AF7
    '273e0005-4c4d-454d-96be-f03bac821358', // AF8
    '273e0006-4c4d-454d-96be-f03bac821358'  // TP10
  ];
  const CH = ['TP9', 'AF7', 'AF8', 'TP10'];
  const BANDS = [
    { name: 'Delta', lo: 1, hi: 4 }, { name: 'Theta', lo: 4, hi: 8 }, { name: 'Alpha', lo: 8, hi: 13 },
    { name: 'Beta', lo: 13, hi: 30 }, { name: 'Gamma', lo: 30, hi: 44 }
  ];
  const FS = 256, N = 512, RING = FS * 20;
  const HANN = new Float64Array(N).map((_, i) => 0.5 - 0.5 * Math.cos(2 * Math.PI * i / (N - 1)));
  const HANN_U = HANN.reduce((a, w) => a + w * w, 0);
  const STALE_MS = 2500;

  // ---------- protocol + signal processing (also exposed for tests) ----------
  function encodeCommand(cmd) {
    const b = new TextEncoder().encode(cmd);
    const out = new Uint8Array(b.length + 2);
    out[0] = b.length + 1; out.set(b, 1); out[out.length - 1] = 0x0a;
    return out;
  }

  function decodeEEG(dv) { // uint16 sequence + 12 x 12-bit samples, 0.48828125 uV per step
    const seq = dv.getUint16(0), s = new Array(12);
    for (let i = 0; i < 12; i++) {
      const b = 2 + ((i * 12) >> 3);
      const v = (i % 2 === 0) ? ((dv.getUint8(b) << 4) | (dv.getUint8(b + 1) >> 4))
                              : (((dv.getUint8(b) & 0x0f) << 8) | dv.getUint8(b + 1));
      s[i] = (v - 2048) * 0.48828125;
    }
    return { seq, samples: s };
  }

  function fftPower(re) { // in-place radix-2; returns |X_k|^2 for k = 0..n/2
    const n = re.length, im = new Float64Array(n);
    for (let i = 1, j = 0; i < n; i++) {
      let bit = n >> 1;
      for (; j & bit; bit >>= 1) j ^= bit;
      j ^= bit;
      if (i < j) { const t = re[i]; re[i] = re[j]; re[j] = t; }
    }
    for (let len = 2; len <= n; len <<= 1) {
      const ang = -2 * Math.PI / len, wr = Math.cos(ang), wi = Math.sin(ang);
      for (let i = 0; i < n; i += len) {
        let cr = 1, ci = 0;
        for (let k = 0; k < len / 2; k++) {
          const a = i + k, b = i + k + len / 2;
          const xr = re[b] * cr - im[b] * ci, xi = re[b] * ci + im[b] * cr;
          re[b] = re[a] - xr; im[b] = im[a] - xi; re[a] += xr; im[a] += xi;
          const ncr = cr * wr - ci * wi; ci = cr * wi + ci * wr; cr = ncr;
        }
      }
    }
    const p = new Float64Array(n / 2 + 1);
    for (let i = 0; i <= n / 2; i++) p[i] = re[i] * re[i] + im[i] * im[i];
    return p;
  }

  // Band power in uV^2 (area under the one-sided Welch-style PSD within the band).
  function bandPowers(win) {
    let mean = 0; for (let i = 0; i < N; i++) mean += win[i]; mean /= N;
    const x = new Float64Array(N);
    for (let i = 0; i < N; i++) x[i] = (win[i] - mean) * HANN[i];
    const p = fftPower(x), df = FS / N;
    return BANDS.map(b => {
      let s = 0;
      for (let k = Math.ceil(b.lo / df); k <= Math.floor(b.hi / df); k++) s += 2 * p[k] / (FS * HANN_U) * df;
      return s;
    });
  }
  const log10 = v => Math.log10(Math.max(v, 1e-9));

  // ---------- state ----------
  const listeners = [];
  const st = {
    connected: false, reconnecting: false, recording: false, deviceName: '', battery: null, rows: 0, elapsed: 0,
    quality: ['none', 'none', 'none', 'none'], rel: [0, 0, 0, 0, 0], dropouts: 0, error: ''
  };
  // Tell the server what happened (counted and logged) so a failed connection can be diagnosed from Loki/Prometheus.
  function report(event, detail) {
    try {
      fetch('/api/client-event', { method: 'POST', keepalive: true, headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ event, detail: String(detail || '').slice(0, 200) }) }).catch(() => {});
    } catch (e) { /* telemetry must never break capture */ }
  }
  const emit = () => listeners.forEach(fn => { try { fn(st); } catch (e) { /* UI errors must not stop capture */ } });

  const ring = CH.map(() => new Float64Array(RING));
  let wpos = [0, 0, 0, 0], hp = CH.map(() => ({ px: 0, py: 0, init: false }));
  let lastSeq = [null, null, null, null], seqBase = [0, 0, 0, 0], lastPkt = [0, 0, 0, 0];
  let device = null, ctrl = null, timer = null, wake = null;
  let rows = [], markers = [], recStart = 0;

  function onEEG(ch, ev) {
    const dv = ev.target.value;
    if (!dv || dv.byteLength < 20) return;
    const { seq, samples } = decodeEEG(dv);
    if (lastSeq[ch] !== null && seq < lastSeq[ch] - 30000) seqBase[ch] += 65536;
    lastSeq[ch] = seq;
    lastPkt[ch] = performance.now();
    const h = hp[ch];
    for (const x of samples) {   // one-pole high-pass (~0.2 Hz) removes slow electrode drift
      if (!h.init) { h.px = x; h.py = 0; h.init = true; }
      const y = 0.995 * (h.py + x - h.px);
      h.px = x; h.py = y;
      ring[ch][wpos[ch] % RING] = y; wpos[ch]++;
    }
  }

  function onTelemetry(ev) {
    const dv = ev.target.value;
    if (dv && dv.byteLength >= 4) { st.battery = lastBattery = Math.round(dv.getUint16(2) / 512); emit(); }
  }

  function window512(ch) {
    const w = new Float64Array(N), end = wpos[ch];
    for (let i = 0; i < N; i++) w[i] = ring[ch][(end - N + i) % RING];
    return w;
  }

  function channelQuality(ch) {
    if (wpos[ch] < FS || performance.now() - lastPkt[ch] > STALE_MS) return 'none';
    let s = 0, s2 = 0;
    for (let i = 0; i < FS; i++) { const v = ring[ch][(wpos[ch] - 1 - i) % RING]; s += v; s2 += v * v; }
    const rms = Math.sqrt(Math.max(0, s2 / FS - (s / FS) * (s / FS)));
    if (rms < 1) return 'flat';
    if (rms > 150) return 'noisy';
    if (rms > 60) return 'fair';
    return 'good';
  }
  const HSI = { good: 1, fair: 2, noisy: 4, flat: 4, none: 4 };

  function tick() {
    const now = Date.now();
    st.quality = CH.map((_, c) => channelQuality(c));
    const fresh = CH.every((_, c) => wpos[c] >= N && performance.now() - lastPkt[c] < STALE_MS);
    if (fresh) {
      const perCh = CH.map((_, c) => bandPowers(window512(c)));        // [ch][band] linear uV^2
      const tot = BANDS.map((_, b) => perCh.reduce((a, p) => a + p[b], 0) / 4);
      const sum = tot.reduce((a, v) => a + v, 0) || 1;
      st.rel = tot.map(v => v / sum);
      if (st.recording) {
        const vals = [];
        for (let b = 0; b < BANDS.length; b++) for (let c = 0; c < 4; c++) vals.push(log10(perCh[c][b]));
        const off = st.quality.every(q => q === 'flat' || q === 'none');
        rows.push({ t: now, v: vals, hsi: st.quality.map(q => HSI[q]), on: off ? 0 : 1 });
      }
    } else if (st.recording && st.connected) {
      st.dropouts++;
    }
    if (st.recording) { st.rows = rows.length; st.elapsed = (now - recStart) / 1000; }
    emit();
  }

  // ---------- connection ----------
  function supported() {
    if (!root.isSecureContext) return { ok: false, reason: 'insecure' };
    if (!root.navigator || !navigator.bluetooth) return { ok: false, reason: 'unsupported' };
    return { ok: true };
  }

  async function requestWake() { try { if ('wakeLock' in navigator) wake = await navigator.wakeLock.request('screen'); } catch (e) { /* optional */ } }
  function releaseWake() { try { if (wake) { wake.release(); wake = null; } } catch (e) { /* optional */ } }

  // Timing knobs (milliseconds); tests shorten them.
  const T = { settle: 300, retryDelay: 900, reconnectDelay: 1200, reconnectTries: 6 };
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  let wantConnected = false, establishing = false, reconnecting = false, connGen = 0, connectedAt = 0, lastBattery = null;

  // Open the GATT link, subscribe to the streams and start them. Used for the first connect and for every reconnect.
  async function establish() {
    establishing = true;
    const gen = ++connGen;            // listeners from an earlier attempt see a stale generation and stay silent
    try {
      // The Muse sometimes drops the link right after connecting (or is held by another app): retry a few times.
      let svc = null;
      for (let attempt = 0; attempt < 3 && !svc; attempt++) {
        try {
          const server = await device.gatt.connect();
          await sleep(T.settle);
          svc = await server.getPrimaryService(SERVICE);
        } catch (e) {
          try { if (device.gatt.connected) device.gatt.disconnect(); } catch (_) { /* ignore */ }
          if (attempt === 2) throw e;
          st.error = 'Connecting… retry ' + (attempt + 2) + ' of 3'; emit();
          report('bt_connect_retry', (e && e.message) || e);
          await sleep(T.retryDelay * (attempt + 1));
        }
      }
      ctrl = await svc.getCharacteristic(CHAR_CONTROL);
      try { await ctrl.startNotifications(); } catch (e) { /* some firmware needs it, some does not */ }
      try {
        const tel = await svc.getCharacteristic(CHAR_TELEMETRY);
        tel.addEventListener('characteristicvaluechanged', ev => { if (gen === connGen) onTelemetry(ev); });
        await tel.startNotifications();
      } catch (e) { /* battery readout is optional */ }
      // fresh buffers: a reconnect restarts the sequence numbers
      wpos = [0, 0, 0, 0]; lastSeq = [null, null, null, null]; seqBase = [0, 0, 0, 0];
      hp = CH.map(() => ({ px: 0, py: 0, init: false }));
      for (let i = 0; i < 4; i++) {
        const c = await svc.getCharacteristic(EEG_CHARS[i]);
        c.addEventListener('characteristicvaluechanged', ev => { if (gen === connGen) onEEG(i, ev); });
        await c.startNotifications();
      }
      const send = async cmd => {
        const data = encodeCommand(cmd);
        if (ctrl.writeValueWithoutResponse) await ctrl.writeValueWithoutResponse(data); else await ctrl.writeValue(data);
        await sleep(60);
      };
      await send('h'); await send('p21'); await send('s'); await send('d');   // halt, preset, stream, resume
      st.connected = true; st.error = ''; connectedAt = Date.now();
      if (!timer) timer = setInterval(tick, 1000);
      requestWake(); emit();
    } finally {
      establishing = false;
    }
  }

  async function pick() {
    let inPicker = true;
    try {
      const d = await navigator.bluetooth.requestDevice({ filters: [{ namePrefix: 'Muse' }], optionalServices: [SERVICE] });
      inPicker = false;
      d.addEventListener('gattserverdisconnected', onDisconnected);   // same function each time: never added twice
      st.deviceName = d.name || 'Muse'; emit();
      return d;
    } catch (e) {
      if (inPicker && e && e.name === 'NotFoundError') e.cancelled = true;   // the person closed the picker
      throw e;
    }
  }

  function explain(m) {
    return /GATT Server is disconnected|Cannot retrieve services|Connection attempt failed|Unsupported device/i.test(m)
      ? 'The headband dropped the connection. Close Muse Monitor or the Muse app (the headband only allows one connection), switch the Muse off and on, forget it in your device’s Bluetooth settings if it is paired there, then press Connect again.'
      : m;
  }

  async function connect() {
    if (reconnecting) return;                                 // the automatic reconnect is already working on it
    st.error = '';
    const sup = supported();
    if (!sup.ok) { st.error = sup.reason; emit(); throw new Error(sup.reason); }
    try {
      wantConnected = true;
      let pickedNow = false;
      if (!device) { device = await pick(); pickedNow = true; }
      try {
        await establish();
      } catch (e) {
        if (pickedNow) throw e;
        device = await pick();                                // a known device that fails: offer the picker once
        await establish();
      }
      report('bt_connected', st.deviceName);
      if (st.recording) mark('reconnected');
    } catch (e) {
      st.connected = false;
      if (e && e.cancelled) { wantConnected = false; device = null; }
      else {
        const m = (e && e.message) ? e.message : String(e);
        report('bt_connect_failed', m);
        st.error = explain(m);
      }
      try { if (device && device.gatt && device.gatt.connected) device.gatt.disconnect(); } catch (_) { /* ignore */ }
      emit();
      throw e;
    }
  }

  function onDisconnected() {
    if (establishing) return;                                  // our own cleanup between connection attempts
    const wasUp = st.connected;
    const upFor = connectedAt ? Math.round((Date.now() - connectedAt) / 1000) : 0;
    st.connected = false; st.battery = null; releaseWake();
    if (!wasUp) { emit(); return; }
    report('bt_disconnected', (st.recording ? 'recording' : 'idle') + ', up ' + upFor + 's, battery ' +
      (lastBattery == null ? '?' : lastBattery + '%') + ', samples ' + rows.length);
    if (!wantConnected) { emit(); return; }                    // the person pressed Disconnect
    st.error = 'The headband dropped the link. Reconnecting…'; emit();
    autoReconnect();
  }

  // After an unexpected drop, reconnect to the same headband without the device picker.
  async function autoReconnect() {
    if (reconnecting) return;
    reconnecting = true; st.reconnecting = true; emit();
    try {
      for (let i = 0; i < T.reconnectTries && wantConnected && !st.connected; i++) {
        await sleep(T.reconnectDelay * (i + 1));
        if (!wantConnected || st.connected) break;
        try {
          await establish();
          st.error = '';
          report('bt_reconnected', 'attempt ' + (i + 1));
          if (st.recording) mark('reconnected');
          return;
        } catch (e) {
          report('bt_reconnect_failed', 'attempt ' + (i + 1) + ': ' + ((e && e.message) || e));
          try { if (device && device.gatt.connected) device.gatt.disconnect(); } catch (_) { /* ignore */ }
        }
      }
      if (!st.connected) {
        st.error = 'Could not reconnect automatically. Check the headband is on and charged, then press Connect Muse. ' +
          (st.recording ? 'Your recording so far is kept.' : '');
      }
    } finally {
      reconnecting = false; st.reconnecting = false; emit();
    }
  }

  async function disconnect() {
    wantConnected = false;
    try { if (ctrl) { const d = encodeCommand('h'); await ctrl.writeValue(d); } } catch (e) { /* ignore */ }
    try { if (device && device.gatt.connected) device.gatt.disconnect(); } catch (e) { /* ignore */ }
    st.connected = false; emit();
  }

  // ---------- recording ----------
  function start() {
    rows = []; markers = []; recStart = Date.now();
    st.recording = true; st.rows = 0; st.elapsed = 0; st.dropouts = 0; st.error = '';
    mark('capture started'); report('capture_started', ''); emit();
  }
  function stop() {
    if (st.recording) report('capture_stopped', 'rows=' + rows.length + ' dropouts=' + st.dropouts);
    st.recording = false; emit();
  }
  function mark(label) {
    if (!st.recording) return false;
    markers.push({ t: Date.now(), label: String(label).replace(/[\r\n",]+/g, ' ').trim().slice(0, 60) || 'marker' });
    return true;
  }

  const pad = (n, w = 2) => String(n).padStart(w, '0');
  function stamp(ms) {
    const d = new Date(ms);
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`;
  }

  function csv() {
    const head = ['TimeStamp'];
    for (const b of BANDS) for (const c of CH) head.push(`${b.name}_${c}`);
    for (const c of CH) head.push(`HSI_${c}`);
    head.push('HeadBandOn', 'Elements');
    const lines = [head.join(',')];
    const all = [...rows.map(r => ({ t: r.t, r })), ...markers.map(m => ({ t: m.t, m }))].sort((a, b) => a.t - b.t);
    const blanks = ','.repeat(head.length - 2);
    for (const e of all) {
      if (e.r) lines.push([stamp(e.t), ...e.r.v.map(v => v.toFixed(4)), ...e.r.hsi, e.r.on, ''].join(','));
      else lines.push(stamp(e.t) + blanks + ',' + `/Marker/${e.m.label}`);
    }
    return lines.join('\n') + '\n';
  }

  root.MuseCapture = {
    supported, connect, disconnect, start, stop, mark, csv, report,
    state: st, onChange: fn => listeners.push(fn), BANDS, CH,
    hasData: () => rows.length >= 5,
    _timing: T,
    _internals: { decodeEEG, bandPowers, encodeCommand, log10, N, FS }
  };
})(window);
