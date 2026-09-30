/* ═══════════════════════════════════════════════════════════════
   ROBOO HUD — canvas renderers (one requestAnimationFrame loop)
   Everything that moves here is driven by real state: the assistant's
   voice (Web Audio analyser), the mic level, the machine's load.
   ═══════════════════════════════════════════════════════════════ */
const HUD = (() => {
  const TAU = Math.PI * 2;
  // ?soft=1 renders canvases on the CPU - only for headless screenshot previews.
  const SOFT = new URLSearchParams(location.search).has("soft");
  const S = {                    // live state, written by app.js
    state: "IDLE", nick: "ROBOO",
    amp: 0, mic: 0, freq: null,  // freq: Uint8Array from the analyser while speaking
    stats: { cpu: 0, ram: 0, disk: 0, battery: null, cores: [], up: 0, down: 0 },
  };
  let T = { rgb: "25,240,255", acc: "255,138,31", ok: "60,255,170", bad: "255,58,90" };

  function refreshTheme() {
    const cs = getComputedStyle(document.documentElement);
    const g = (n, d) => (cs.getPropertyValue(n).trim() || d).replace(/\s+/g, "");
    T = { rgb: g("--rgb", T.rgb), acc: g("--acc", T.acc), ok: g("--ok", T.ok), bad: g("--bad", T.bad) };
  }
  const C = (rgb, a) => `rgba(${rgb},${a})`;
  const rnd = (a, b) => a + Math.random() * (b - a);

  // DPR-aware sizing, checked every frame (cheap) so layout changes just work.
  function fit(cv) {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = cv.clientWidth, h = cv.clientHeight;
    if (!w || !h) return null;
    if (cv._w !== w || cv._h !== h || cv._dpr !== dpr) {
      cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
      cv._w = w; cv._h = h; cv._dpr = dpr;
    }
    const ctx = cv.getContext("2d", SOFT ? { willReadFrequently: true } : undefined);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    return { ctx, w, h };
  }

  // ── smoothed drive values ──────────────────────────────────────────
  let energy = 0, speed = 1, stateRGB = T.rgb, lastT = performance.now(), phase = 0;
  const SPEEDS = { IDLE: 1, STANDBY: 1, LISTENING: 1.5, RECOGNISING: 3, THINKING: 3.6, PROCESSING: 4.2, SPEAKING: 1.9 };

  function stateColour() {
    const s = S.state;
    if (s === "LISTENING") return T.ok;
    if (s === "THINKING" || s === "PROCESSING" || s === "RECOGNISING") return T.acc;
    return T.rgb;
  }

  function spectrum(n) {
    // n values 0..1: real FFT while speaking, mic-driven noise while listening, idle breath otherwise
    const out = new Array(n);
    const f = S.freq;
    for (let i = 0; i < n; i++) {
      let v;
      if (f && S.amp > 0.01) {
        const k = Math.floor(Math.pow(i / n, 1.4) * (f.length * 0.8));
        v = f[k] / 255;
      } else if (S.mic > 0.02) {
        v = S.mic * (0.4 + 0.6 * Math.abs(Math.sin(i * 0.9 + phase * 9 + Math.sin(i * 0.37) * 3)));
      } else {
        v = 0.06 + 0.05 * Math.sin(i * 0.45 + phase * 2.2);
      }
      out[i] = v;
    }
    return out;
  }

  // ── background: perspective floor, drifting motes, hex haze ────────
  const motes = Array.from({ length: 70 }, () => ({ x: Math.random(), y: Math.random(), z: rnd(.2, 1), v: rnd(.00005, .0003) }));
  function drawBG(cv, t) {
    const f = fit(cv); if (!f) return;
    const { ctx, w, h } = f;
    const g = ctx.createRadialGradient(w / 2, h * .45, 0, w / 2, h * .45, Math.max(w, h) * .7);
    g.addColorStop(0, C(T.rgb, .09)); g.addColorStop(.5, C(T.rgb, .025)); g.addColorStop(1, "rgba(0,0,0,0)");
    ctx.fillStyle = g; ctx.fillRect(0, 0, w, h);

    // floor grid
    const hy = h * .62, vx = w / 2;
    ctx.lineWidth = 1;
    for (let i = -24; i <= 24; i++) {
      ctx.strokeStyle = C(T.rgb, .07 - Math.abs(i) * .0022);
      ctx.beginPath(); ctx.moveTo(vx + i * 14, hy); ctx.lineTo(vx + i * w * .14, h); ctx.stroke();
    }
    const off = (t * .00004) % 1;
    for (let k = 0; k < 16; k++) {
      const p = Math.pow((k + off) / 16, 2.2), y = hy + p * (h - hy);
      ctx.strokeStyle = C(T.rgb, .015 + p * .08);
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
    }
    // motes
    for (const m of motes) {
      m.y -= m.v * 16; if (m.y < -0.02) { m.y = 1.02; m.x = Math.random(); }
      ctx.fillStyle = C(T.rgb, .15 + .5 * m.z * (0.5 + 0.5 * Math.sin(t * .002 + m.x * 40)));
      ctx.fillRect(m.x * w, m.y * h, 1.6 * m.z, 1.6 * m.z);
    }
  }

  // ── the reactor ────────────────────────────────────────────────────
  const SPH = (() => {  // fibonacci sphere
    const n = 720, pts = [], ga = Math.PI * (3 - Math.sqrt(5));
    for (let i = 0; i < n; i++) {
      const y = 1 - (i / (n - 1)) * 2, r = Math.sqrt(1 - y * y), th = ga * i;
      pts.push([Math.cos(th) * r, y, Math.sin(th) * r]);
    }
    return pts;
  })();
  const SEG = Array.from({ length: 30 }, (_, i) => ({ len: rnd(.4, 1), acc: i % 7 === 3 }));
  const waves = [];
  let lastPeak = 0, readout = [], readoutT = 0;

  function drawReactor(cv, t) {
    const f = fit(cv); if (!f) return;
    const { ctx, w, h } = f;
    const cx = w / 2, cy = h / 2, R = Math.min(w * .5, h * .5) * .9;
    const sc = stateRGB, sp = speed, e = energy;
    const rot = phase;

    // glow body
    let g = ctx.createRadialGradient(cx, cy, 0, cx, cy, R * 1.05);
    g.addColorStop(0, C(sc, .16 + e * .25)); g.addColorStop(.4, C(sc, .05 + e * .06)); g.addColorStop(1, "rgba(0,0,0,0)");
    ctx.fillStyle = g; ctx.beginPath(); ctx.arc(cx, cy, R * 1.05, 0, TAU); ctx.fill();

    // crosshair to edges
    ctx.strokeStyle = C(T.rgb, .14); ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, cy); ctx.lineTo(cx - R * 1.08, cy); ctx.moveTo(cx + R * 1.08, cy); ctx.lineTo(w, cy);
    ctx.moveTo(cx, 0); ctx.lineTo(cx, cy - R * 1.08); ctx.moveTo(cx, cy + R * 1.08); ctx.lineTo(cx, h);
    ctx.stroke();
    for (let k = 1; k < 7; k++) {         // ticks on the crosshair
      const d = R * 1.08 + k * 16;
      ctx.fillStyle = C(T.rgb, .35 - k * .04);
      ctx.fillRect(cx - d, cy - 3, 1, 6); ctx.fillRect(cx + d, cy - 3, 1, 6);
    }

    ctx.save(); ctx.translate(cx, cy);
    ctx.globalCompositeOperation = "lighter";

    // outer brackets
    ctx.lineWidth = 2.5; ctx.strokeStyle = C(T.rgb, .8);
    for (let k = 0; k < 4; k++) {
      const a0 = k * TAU / 4 + Math.PI / 4 - .38;
      ctx.beginPath(); ctx.arc(0, 0, R * 1.02, a0, a0 + .76); ctx.stroke();
      for (const a of [a0, a0 + .76]) {
        ctx.beginPath(); ctx.moveTo(Math.cos(a) * R * 1.02, Math.sin(a) * R * 1.02);
        ctx.lineTo(Math.cos(a) * R * .97, Math.sin(a) * R * .97); ctx.stroke();
      }
    }
    // accent notches on the cardinal points
    ctx.fillStyle = C(T.acc, .95);
    for (let k = 0; k < 4; k++) {
      ctx.save(); ctx.rotate(k * TAU / 4);
      ctx.beginPath(); ctx.moveTo(R * 1.045, 0); ctx.lineTo(R * 1.09, -5); ctx.lineTo(R * 1.09, 5); ctx.fill();
      ctx.restore();
    }

    // degree ring
    ctx.save(); ctx.rotate(rot * .05);
    for (let i = 0; i < 180; i++) {
      const a = i * TAU / 180, long = i % 15 === 0, mid = i % 5 === 0;
      const r0 = R * (long ? .915 : mid ? .935 : .95);
      ctx.strokeStyle = C(T.rgb, long ? .75 : mid ? .4 : .2); ctx.lineWidth = long ? 1.6 : 1;
      ctx.beginPath(); ctx.moveTo(Math.cos(a) * r0, Math.sin(a) * r0); ctx.lineTo(Math.cos(a) * R * .965, Math.sin(a) * R * .965); ctx.stroke();
    }
    ctx.font = `${Math.max(8, R * .03)}px "Share Tech Mono", monospace`;
    ctx.fillStyle = C(T.rgb, .55); ctx.textAlign = "center"; ctx.textBaseline = "middle";
    for (let i = 0; i < 12; i++) {
      const a = i * TAU / 12;
      ctx.save(); ctx.rotate(a); ctx.translate(0, -R * .885);
      ctx.fillText(String(i * 30).padStart(3, "0"), 0, 0); ctx.restore();
    }
    ctx.restore();

    // thick segmented ring
    ctx.save(); ctx.rotate(-rot * .18);
    ctx.lineWidth = R * .034;
    const step = TAU / SEG.length;
    SEG.forEach((s, i) => {
      ctx.strokeStyle = s.acc ? C(T.acc, .75) : C(sc, .35 + .35 * (0.5 + 0.5 * Math.sin(phase * 2 + i)) * (0.4 + e));
      ctx.beginPath(); ctx.arc(0, 0, R * .835, i * step, i * step + step * s.len * .82); ctx.stroke();
    });
    ctx.restore();

    // dashed ring + orbiting triangles
    ctx.save(); ctx.rotate(rot * .3);
    ctx.setLineDash([2, 7]); ctx.strokeStyle = C(T.rgb, .45); ctx.lineWidth = 1.2;
    ctx.beginPath(); ctx.arc(0, 0, R * .78, 0, TAU); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = C(T.acc, .9);
    for (let k = 0; k < 3; k++) {
      ctx.save(); ctx.rotate(k * TAU / 3); ctx.translate(R * .78, 0);
      ctx.beginPath(); ctx.moveTo(-7, 0); ctx.lineTo(4, -5); ctx.lineTo(4, 5); ctx.fill(); ctx.restore();
    }
    ctx.restore();

    // fast arcs (these are what visibly "think")
    for (const [rr, n, span, dir, lw, a] of [[.735, 3, .7, 1, 3, .8], [.705, 2, 1.4, -1, 1.5, .5], [.665, 5, .25, 1, 2, .6]]) {
      ctx.save(); ctx.rotate(rot * dir * (.7 + rr) * 1.2);
      ctx.lineWidth = lw; ctx.strokeStyle = C(sc, a);
      for (let k = 0; k < n; k++) { ctx.beginPath(); ctx.arc(0, 0, R * rr, k * TAU / n, k * TAU / n + span); ctx.stroke(); }
      ctx.restore();
    }

    // radar sweep
    if (ctx.createConicGradient) {
      const cg = ctx.createConicGradient(rot * .9, 0, 0);
      cg.addColorStop(0, C(sc, .22)); cg.addColorStop(.08, C(sc, 0)); cg.addColorStop(1, C(sc, 0));
      ctx.fillStyle = cg; ctx.beginPath(); ctx.arc(0, 0, R * .64, 0, TAU); ctx.fill();
    }

    // voice spectrum ring
    const N = 128, sv = spectrum(N), r0 = R * .5;
    for (let i = 0; i < N; i++) {
      const a = i * TAU / N - Math.PI / 2, v = sv[i], len = R * (.012 + v * .17);
      const c = Math.cos(a), s = Math.sin(a);
      ctx.strokeStyle = C(v > .55 ? T.acc : sc, .35 + v * .65); ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(c * r0, s * r0); ctx.lineTo(c * (r0 + len), s * (r0 + len)); ctx.stroke();
    }

    // gear ring
    ctx.save(); ctx.rotate(-rot * .45);
    ctx.strokeStyle = C(T.rgb, .55); ctx.lineWidth = 1.2;
    ctx.beginPath(); ctx.arc(0, 0, R * .47, 0, TAU); ctx.stroke();
    ctx.fillStyle = C(T.rgb, .55);
    for (let i = 0; i < 36; i++) { ctx.save(); ctx.rotate(i * TAU / 36); ctx.fillRect(R * .44, -1.5, R * .025, 3); ctx.restore(); }
    ctx.restore();

    // orbit electrons
    for (let k = 0; k < 3; k++) {
      const tilt = k * TAU / 3 + rot * .05, rx = R * .43, ry = R * .13;
      ctx.save(); ctx.rotate(tilt);
      ctx.strokeStyle = C(T.rgb, .16); ctx.lineWidth = 1;
      ctx.beginPath(); ctx.ellipse(0, 0, rx, ry, 0, 0, TAU); ctx.stroke();
      const a = phase * (1.3 + k * .4) * (k % 2 ? -1 : 1);
      const px = Math.cos(a) * rx, py = Math.sin(a) * ry;
      const eg = ctx.createRadialGradient(px, py, 0, px, py, 9);
      eg.addColorStop(0, C("255,255,255", .95)); eg.addColorStop(.3, C(sc, .8)); eg.addColorStop(1, C(sc, 0));
      ctx.fillStyle = eg; ctx.beginPath(); ctx.arc(px, py, 9, 0, TAU); ctx.fill();
      ctx.restore();
    }

    // particle sphere
    const Rs = R * .33 * (1 + e * .22);
    const ay = phase * .35, ax = .42 + Math.sin(phase * .2) * .15;
    const cya = Math.cos(ay), sya = Math.sin(ay), cxa = Math.cos(ax), sxa = Math.sin(ax);
    for (let i = 0; i < SPH.length; i++) {
      let [x, y, z] = SPH[i];
      const wob = 1 + e * .18 * Math.sin(i * 7.3 + phase * 7);
      let x1 = x * cya + z * sya, z1 = -x * sya + z * cya;
      let y1 = y * cxa - z1 * sxa, z2 = y * sxa + z1 * cxa;
      const d = (z2 + 1.6) / 2.6;
      const px = x1 * Rs * wob, py = y1 * Rs * wob, sz = .7 + d * 1.8;
      ctx.fillStyle = C(z2 > .55 && i % 9 === 0 ? T.acc : sc, .1 + d * .75);
      ctx.fillRect(px - sz / 2, py - sz / 2, sz, sz);
    }

    // core
    g = ctx.createRadialGradient(0, 0, 0, 0, 0, R * .24);
    g.addColorStop(0, C("255,255,255", .55 + e * .45)); g.addColorStop(.25, C(sc, .45 + e * .3)); g.addColorStop(1, C(sc, 0));
    ctx.fillStyle = g; ctx.beginPath(); ctx.arc(0, 0, R * .24, 0, TAU); ctx.fill();

    // shockwaves on voice peaks
    if (S.amp > .42 && t - lastPeak > 260) { waves.push({ r: R * .34, a: .8 }); lastPeak = t; }
    for (let i = waves.length - 1; i >= 0; i--) {
      const wv = waves[i]; wv.r += R * .012; wv.a *= .955;
      ctx.strokeStyle = C(sc, wv.a); ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(0, 0, wv.r, 0, TAU); ctx.stroke();
      if (wv.a < .03 || wv.r > R) waves.splice(i, 1);
    }

    ctx.globalCompositeOperation = "source-over";
    // name
    const fs = Math.max(14, R * .085);
    ctx.font = `900 ${fs}px Orbitron, sans-serif`;
    if ("letterSpacing" in ctx) ctx.letterSpacing = `${fs * .28}px`;
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillStyle = C(sc, .35); ctx.fillText(S.nick, 2, 2);
    ctx.fillStyle = "#fff"; ctx.fillText(S.nick, 0, 0);
    if ("letterSpacing" in ctx) ctx.letterSpacing = "0px";
    ctx.restore();

    // side readouts
    if (w > R * 2.9) {
      if (t - readoutT > 450) {
        readoutT = t;
        readout = [
          ["AZ", (rot * 57.3 % 360).toFixed(2)], ["EL", (22 + Math.sin(phase) * 9).toFixed(2)],
          ["SYNC", (97 + Math.random() * 3).toFixed(1) + "%"], ["FLUX", (e * 100).toFixed(1)],
          ["CPU", (S.stats.cpu || 0).toFixed(0) + "%"], ["MEM", (S.stats.ram || 0).toFixed(0) + "%"],
        ];
      }
      ctx.font = `11px "Share Tech Mono", monospace`;
      readout.forEach(([k, v], i) => {
        const left = i < 3, y = cy - R * .55 + (i % 3) * 22 + (left ? 0 : R * .75);
        const x = left ? cx - R * 1.28 : cx + R * 1.28;
        ctx.textAlign = left ? "left" : "right";
        ctx.fillStyle = C(T.rgb, .45); ctx.fillText(k, x, y);
        ctx.fillStyle = C("235,255,255", .9); ctx.fillText(v, left ? x + 44 : x - 44, y);
        ctx.fillStyle = C(T.rgb, .3); ctx.fillRect(left ? x : x - 110, y + 7, 110, 1);
      });
    }
  }

  // ── gauges ─────────────────────────────────────────────────────────
  function drawGauge(cv, val) {
    const f = fit(cv); if (!f) return;
    const { ctx, w, h } = f;
    cv._v = (cv._v || 0) + ((val ?? 0) - (cv._v || 0)) * .08;
    const v = cv._v, cx = w / 2, cy = h / 2, r = Math.min(w, h) * .4;
    const a0 = Math.PI * .75, span = Math.PI * 1.5, hot = v > 85;
    ctx.lineWidth = r * .16;
    ctx.strokeStyle = C(T.rgb, .12); ctx.beginPath(); ctx.arc(cx, cy, r, a0, a0 + span); ctx.stroke();
    ctx.strokeStyle = C(hot ? T.bad : T.rgb, .95);
    ctx.beginPath(); ctx.arc(cx, cy, r, a0, a0 + span * Math.min(1, v / 100)); ctx.stroke();
    ctx.lineWidth = 1;
    for (let i = 0; i <= 20; i++) {
      const a = a0 + span * i / 20, r1 = r * 1.22, r2 = r * (i % 5 ? 1.16 : 1.1);
      ctx.strokeStyle = C(T.rgb, i / 20 <= v / 100 ? .7 : .2);
      ctx.beginPath(); ctx.moveTo(cx + Math.cos(a) * r1, cy + Math.sin(a) * r1); ctx.lineTo(cx + Math.cos(a) * r2, cy + Math.sin(a) * r2); ctx.stroke();
    }
    ctx.save(); ctx.translate(cx, cy); ctx.rotate(phase * .6);
    ctx.strokeStyle = C(T.rgb, .35); ctx.setLineDash([2, 4]);
    ctx.beginPath(); ctx.arc(0, 0, r * .66, 0, TAU); ctx.stroke(); ctx.restore(); ctx.setLineDash([]);
    ctx.fillStyle = "#eaffff"; ctx.font = `600 ${r * .5}px Orbitron, sans-serif`;
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText(val == null ? "--" : Math.round(v), cx, cy + 1);
  }

  // ── cores ──────────────────────────────────────────────────────────
  function drawCores(cv) {
    const f = fit(cv); if (!f) return;
    const { ctx, w, h } = f;
    const cores = S.stats.cores && S.stats.cores.length ? S.stats.cores : new Array(8).fill(0);
    cv._v = cv._v && cv._v.length === cores.length ? cv._v : cores.map(() => 0);
    const n = cores.length, bw = w / n, segs = 12, sh = (h - 14) / segs;
    cores.forEach((c, i) => {
      cv._v[i] += (c - cv._v[i]) * .1;
      const lit = Math.round(cv._v[i] / 100 * segs);
      for (let s = 0; s < segs; s++) {
        const on = s < lit;
        ctx.fillStyle = on ? C(s > segs * .8 ? T.acc : T.rgb, .35 + s / segs * .6) : C(T.rgb, .07);
        ctx.fillRect(i * bw + 2, h - 14 - (s + 1) * sh + 1, bw - 4, sh - 2);
      }
      ctx.fillStyle = C(T.rgb, .45); ctx.font = `9px "Share Tech Mono"`; ctx.textAlign = "center";
      ctx.fillText(String(i).padStart(2, "0"), i * bw + bw / 2, h - 3);
    });
  }

  // ── network graph ──────────────────────────────────────────────────
  const netHist = { up: new Array(60).fill(0), down: new Array(60).fill(0) };
  function pushNet(up, down) { netHist.up.push(up); netHist.up.shift(); netHist.down.push(down); netHist.down.shift(); }
  function drawNet(cv) {
    const f = fit(cv); if (!f) return;
    const { ctx, w, h } = f;
    ctx.strokeStyle = C(T.rgb, .08); ctx.lineWidth = 1;
    for (let i = 1; i < 4; i++) { ctx.beginPath(); ctx.moveTo(0, h * i / 4); ctx.lineTo(w, h * i / 4); ctx.stroke(); }
    for (let i = 1; i < 10; i++) { ctx.beginPath(); ctx.moveTo(w * i / 10, 0); ctx.lineTo(w * i / 10, h); ctx.stroke(); }
    const max = Math.max(50e3, ...netHist.up, ...netHist.down) * 1.15;
    const path = (arr) => { ctx.beginPath(); arr.forEach((v, i) => { const x = i / (arr.length - 1) * w, y = h - v / max * h; i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); }); };
    path(netHist.down); ctx.lineTo(w, h); ctx.lineTo(0, h); ctx.closePath();
    const g = ctx.createLinearGradient(0, 0, 0, h); g.addColorStop(0, C(T.rgb, .45)); g.addColorStop(1, C(T.rgb, 0));
    ctx.fillStyle = g; ctx.fill();
    path(netHist.down); ctx.strokeStyle = C(T.rgb, .95); ctx.lineWidth = 1.5; ctx.stroke();
    path(netHist.up); ctx.strokeStyle = C(T.acc, .9); ctx.lineWidth = 1.2; ctx.stroke();
    const sx = (phase * 40) % w; ctx.fillStyle = C(T.rgb, .25); ctx.fillRect(sx, 0, 1, h);
  }

  // ── globe ──────────────────────────────────────────────────────────
  const LAND = (() => {
    const pts = [];
    for (let lat = -80; lat <= 80; lat += 3.2) {
      const step = 3.2 / Math.max(.25, Math.cos(lat * Math.PI / 180));
      for (let lon = -180; lon < 180; lon += step) {
        const la = lat * Math.PI / 180, lo = lon * Math.PI / 180;
        const n = Math.sin(lo * 2 + 1) * Math.cos(la * 2.2) + .7 * Math.sin(lo * 3.3 + la * 2.7 + 2.1)
          + .45 * Math.sin(lo * 7.1 - la * 5.3) + .25 * Math.cos(lo * 11 + la * 9);
        if (n > .55) pts.push([Math.cos(la) * Math.cos(lo), Math.sin(la), Math.cos(la) * Math.sin(lo)]);
      }
    }
    return pts;
  })();
  const pings = [];
  function drawGlobe(cv, t) {
    const f = fit(cv); if (!f) return;
    const { ctx, w, h } = f;
    const cx = w / 2, cy = h / 2, r = Math.min(w, h) * .4, ay = phase * .25, tilt = .35;
    const ca = Math.cos(ay), sa = Math.sin(ay), ct = Math.cos(tilt), st = Math.sin(tilt);
    const proj = ([x, y, z]) => { const x1 = x * ca + z * sa, z1 = -x * sa + z * ca; return [x1, y * ct - z1 * st, y * st + z1 * ct]; };
    let g = ctx.createRadialGradient(cx, cy, r * .2, cx, cy, r * 1.25);
    g.addColorStop(0, C(T.rgb, .12)); g.addColorStop(.8, C(T.rgb, .05)); g.addColorStop(1, C(T.rgb, 0));
    ctx.fillStyle = g; ctx.beginPath(); ctx.arc(cx, cy, r * 1.25, 0, TAU); ctx.fill();
    ctx.strokeStyle = C(T.rgb, .5); ctx.lineWidth = 1.2; ctx.beginPath(); ctx.arc(cx, cy, r, 0, TAU); ctx.stroke();
    // meridians
    ctx.strokeStyle = C(T.rgb, .1); ctx.lineWidth = 1;
    for (let m = 0; m < 6; m++) {
      const a = m * Math.PI / 6 + ay; ctx.beginPath(); ctx.ellipse(cx, cy, Math.abs(Math.cos(a)) * r, r, 0, 0, TAU); ctx.stroke();
    }
    for (let p = -2; p <= 2; p++) { const y = p * r / 3 * ct; ctx.beginPath(); ctx.ellipse(cx, cy + y, Math.sqrt(1 - (p / 3) ** 2) * r, r * .1 * Math.sqrt(1 - (p / 3) ** 2) + .1, 0, 0, TAU); ctx.stroke(); }
    // land
    for (const pt of LAND) {
      const [x, y, z] = proj(pt);
      if (z < -0.05) continue;
      ctx.fillStyle = C(T.rgb, .25 + z * .7);
      ctx.fillRect(cx + x * r - .9, cy - y * r - .9, 1.8, 1.8);
    }
    // pings + arcs
    if (Math.random() < .03 && pings.length < 6) pings.push({ p: LAND[Math.floor(Math.random() * LAND.length)], q: LAND[Math.floor(Math.random() * LAND.length)], t: 0 });
    for (let i = pings.length - 1; i >= 0; i--) {
      const pg = pings[i]; pg.t += .008;
      const a = proj(pg.p), b = proj(pg.q);
      if (a[2] > 0) {
        ctx.strokeStyle = C(T.acc, Math.max(0, 1 - pg.t)); ctx.lineWidth = 1.2;
        ctx.beginPath(); ctx.arc(cx + a[0] * r, cy - a[1] * r, 2 + pg.t * 14, 0, TAU); ctx.stroke();
        ctx.fillStyle = C(T.acc, 1); ctx.fillRect(cx + a[0] * r - 2, cy - a[1] * r - 2, 4, 4);
      }
      if (a[2] > 0 && b[2] > 0) {
        const mx = (a[0] + b[0]) / 2 * 1.5, my = (a[1] + b[1]) / 2 * 1.5, k = Math.min(1, pg.t * 2.5);
        ctx.strokeStyle = C(T.acc, .6 * (1 - pg.t)); ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(cx + a[0] * r, cy - a[1] * r);
        const qx = cx + mx * r, qy = cy - my * r, bx = cx + b[0] * r, by = cy - b[1] * r;
        const sx = cx + a[0] * r, sy = cy - a[1] * r;
        const ex = (1 - k) * ((1 - k) * sx + k * qx) + k * ((1 - k) * qx + k * bx);
        const ey = (1 - k) * ((1 - k) * sy + k * qy) + k * ((1 - k) * qy + k * by);
        ctx.quadraticCurveTo((1 - k) * sx + k * qx, (1 - k) * sy + k * qy, ex, ey); ctx.stroke();
      }
      if (pg.t >= 1) pings.splice(i, 1);
    }
    // orbit + satellite
    ctx.save(); ctx.translate(cx, cy); ctx.rotate(-.35);
    ctx.strokeStyle = C(T.rgb, .35); ctx.setLineDash([3, 5]);
    ctx.beginPath(); ctx.ellipse(0, 0, r * 1.35, r * .38, 0, 0, TAU); ctx.stroke(); ctx.setLineDash([]);
    const sa2 = phase * .9; ctx.fillStyle = "#fff";
    ctx.beginPath(); ctx.arc(Math.cos(sa2) * r * 1.35, Math.sin(sa2) * r * .38, 2.6, 0, TAU); ctx.fill();
    ctx.restore();
    ctx.font = `10px "Share Tech Mono"`; ctx.fillStyle = C(T.rgb, .6); ctx.textAlign = "left";
    ctx.fillText(`LAT ${(Math.sin(phase * .3) * 60).toFixed(2)}`, 6, 14);
    ctx.fillText(`LON ${((ay * 57.3) % 360 - 180).toFixed(2)}`, 6, 27);
    ctx.textAlign = "right"; ctx.fillText(`NODES ${LAND.length}`, w - 6, h - 8);
  }

  // ── radar ──────────────────────────────────────────────────────────
  const blips = [];
  function drawRadar(cv) {
    const f = fit(cv); if (!f) return;
    const { ctx, w, h } = f;
    const cx = w / 2, cy = h / 2, r = Math.min(w, h) * .44, a = phase * 1.4;
    ctx.strokeStyle = C(T.rgb, .25); ctx.lineWidth = 1;
    for (let i = 1; i <= 4; i++) { ctx.beginPath(); ctx.arc(cx, cy, r * i / 4, 0, TAU); ctx.stroke(); }
    ctx.beginPath(); ctx.moveTo(cx - r, cy); ctx.lineTo(cx + r, cy); ctx.moveTo(cx, cy - r); ctx.lineTo(cx, cy + r); ctx.stroke();
    if (ctx.createConicGradient) {
      const cg = ctx.createConicGradient(a - .9, cx, cy);
      cg.addColorStop(0, C(T.rgb, 0)); cg.addColorStop(.143, C(T.rgb, .45)); cg.addColorStop(.144, C(T.rgb, 0)); cg.addColorStop(1, C(T.rgb, 0));
      ctx.fillStyle = cg; ctx.beginPath(); ctx.arc(cx, cy, r, 0, TAU); ctx.fill();
    }
    if (Math.random() < .02) blips.push({ a: a % TAU, d: rnd(.2, .95), l: 1 });
    for (let i = blips.length - 1; i >= 0; i--) {
      const b = blips[i]; b.l -= .006;
      ctx.fillStyle = C(T.acc, b.l); ctx.beginPath(); ctx.arc(cx + Math.cos(b.a) * r * b.d, cy + Math.sin(b.a) * r * b.d, 3, 0, TAU); ctx.fill();
      if (b.l <= 0) blips.splice(i, 1);
    }
    ctx.font = `10px "Share Tech Mono"`; ctx.fillStyle = C(T.rgb, .55); ctx.textAlign = "center";
    ctx.fillText("AWAITING INTEL", cx, h - 6);
  }

  // ── footer waveform ────────────────────────────────────────────────
  function drawWave(cv) {
    const f = fit(cv); if (!f) return;
    const { ctx, w, h } = f;
    const n = Math.floor(w / 6), sv = spectrum(n), mid = h / 2;
    for (let i = 0; i < n; i++) {
      const d = Math.abs(i - n / 2) / (n / 2), v = sv[Math.floor(d * n * .9)] * (1 - d * .55);
      const bh = Math.max(1.5, v * h * .9);
      ctx.fillStyle = C(v > .6 ? T.acc : stateRGB, .3 + v * .7);
      ctx.fillRect(i * 6, mid - bh / 2, 3.5, bh);
    }
    ctx.fillStyle = C(T.rgb, .25); ctx.fillRect(0, mid, w, 1);
  }

  // ── boot / setup backdrops ─────────────────────────────────────────
  function drawRings(cv, t, grow) {
    const f = fit(cv); if (!f) return;
    const { ctx, w, h } = f;
    const cx = w / 2, cy = h / 2, R = Math.min(w, h) * .46 * (grow ?? 1);
    ctx.save(); ctx.translate(cx, cy); ctx.globalCompositeOperation = "lighter";
    for (let k = 0; k < 7; k++) {
      const rr = R * (.5 + k * .09), n = 2 + (k % 3), span = .4 + (k % 4) * .35;
      ctx.save(); ctx.rotate(phase * (k % 2 ? -1 : 1) * (.15 + k * .06));
      ctx.strokeStyle = C(k % 3 === 1 ? T.acc : T.rgb, .12 + (k % 3) * .1); ctx.lineWidth = 1 + (k % 3);
      for (let i = 0; i < n; i++) { ctx.beginPath(); ctx.arc(0, 0, rr, i * TAU / n, i * TAU / n + span); ctx.stroke(); }
      ctx.restore();
    }
    ctx.setLineDash([2, 8]); ctx.strokeStyle = C(T.rgb, .2);
    ctx.beginPath(); ctx.arc(0, 0, R * 1.12, 0, TAU); ctx.stroke(); ctx.setLineDash([]);
    ctx.restore();
  }

  // ── main loop ──────────────────────────────────────────────────────
  const targets = [];   // [canvas, fn] pairs registered by app.js
  function loop(t) {
    const dt = Math.min(.05, (t - lastT) / 1000); lastT = t;
    const want = Math.max(S.amp, S.mic * .8);
    energy += (want - energy) * (want > energy ? .35 : .08);
    speed += ((SPEEDS[S.state] || 1) - speed) * .04;
    phase += dt * speed;
    stateRGB = stateColour();
    for (const [cv, fn] of targets) {
      if (cv.offsetParent === null && cv.id !== "bg") continue;   // hidden
      try { fn(cv, t); } catch (e) { /* never let one panel stop the HUD */ }
    }
    requestAnimationFrame(loop);
  }

  function start() {
    refreshTheme();
    const $ = (id) => document.getElementById(id);
    targets.push([$("bg"), drawBG], [$("reactor"), drawReactor], [$("cores"), drawCores],
      [$("net"), drawNet], [$("globe"), drawGlobe], [$("wave"), drawWave], [$("radar"), drawRadar],
      [$("bootRings"), (c, t) => drawRings(c, t, 1)], [$("setupRings"), (c, t) => drawRings(c, t, 1.25)]);
    document.querySelectorAll("canvas[data-g]").forEach((cv) => targets.push([cv, () => drawGauge(cv, S.stats[cv.dataset.g])]));
    requestAnimationFrame(loop);
  }

  function attachRadar() {
    const cv = document.getElementById("radar");
    if (cv && !targets.some(([c]) => c === cv)) targets.push([cv, drawRadar]);
  }

  return { S, start, refreshTheme, pushNet, attachRadar };
})();
