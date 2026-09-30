/* ═══════════════════════════════════════════════════════════════
   NEXUS — app logic: boot, bridge to Python, chat, voice, setup
   ═══════════════════════════════════════════════════════════════ */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const THEMES = { cyan: "#19f0ff", amber: "#ffba28", azure: "#46a0ff", emerald: "#28ff96", crimson: "#ff3c5a", violet: "#b978ff" };

// Mock backend so the page can be previewed in a normal browser (?mock=1).
const MOCK = {
  _cfg: { nickname: "", user_name: "", provider: "", model: "", base_url: "", voice: "en-IN-PrabhatNeural", language: "en-IN", theme: "cyan", voice_enabled: true, auto_listen: false },
  async ui_ready() { return this.get_state(); },
  async get_state() {
    return { configured: !!this._cfg.nickname, config: { ...this._cfg, has_key: false },
      presets: { anthropic: { label: "Anthropic Claude", base: "https://api.anthropic.com", model: "claude-opus-5-5" }, gemini: { label: "Google Gemini", base: "", model: "gemini-flash-latest" }, openai: { label: "OpenAI", base: "https://api.openai.com/v1", model: "gpt-4o-mini" }, ollama: { label: "Ollama (local)", base: "http://localhost:11434/v1", model: "llama3.2" }, custom: { label: "Custom (OpenAI-compatible)", base: "", model: "" } },
      voices: [["en-IN-PrabhatNeural", "Prabhat - English (India), male"]], languages: [["en-IN", "English (India)"]],
      memory: [{ fact: "Likes lofi music while coding", at: "2026-09-30" }], mic: { ok: true, device: "Mock mic" }, tools: [] };
  },
  async detect_provider(k) { return k.startsWith("sk-ant-") ? "anthropic" : k.startsWith("AIza") ? "gemini" : k.startsWith("sk-") ? "openai" : ""; },
  async fetch_models() { return { ok: true, models: ["model-a", "model-b"] }; },
  async save_setup(v) { await sleep(1200); Object.assign(this._cfg, v); return { ok: true, state: await this.get_state() }; },
  async save_prefs(v) { Object.assign(this._cfg, v); return this.get_state(); },
  async send_text(t) {
    NX.on({ type: "chat", role: "user", text: t }); NX.on({ type: "state", state: "THINKING" });
    await sleep(500); NX.on({ type: "task", id: "a1", name: "web_search", status: "running", detail: "query=" + t });
    await sleep(900); NX.on({ type: "task", id: "a1", name: "web_search", status: "done", detail: "6 results", ms: 812 });
    NX.on({ type: "intel", kind: "weather", title: "Delhi", data: { city: "Delhi", temp_c: 31, feels_c: 34, desc: "Haze", humidity: 48, wind_kmph: 9, forecast: [{ date: "Wed", max: 33, min: 24 }, { date: "Thu", max: 32, min: 23 }, { date: "Fri", max: 34, min: 25 }] } });
    NX.on({ type: "chat", role: "assistant", text: "Mock mode: this is how replies appear, typed out live in the comms log." });
    NX.on({ type: "speak", audio: null, text: "Mock mode reply." });
  },
  async mic_press() { return { ok: true, armed: true }; },
  async speech_done() { NX.on({ type: "state", state: "IDLE" }); },
  async confirm_reply() {}, async clear_chat() {}, async delete_memory() { return []; }, async greet() {},
  async stats() {
    const r = () => Math.random() * 100;
    return { cpu: 20 + Math.random() * 40, cores: Array.from({ length: 12 }, r), ram: 63, ram_used: 10.1, ram_total: 16, disk: 71, up: Math.random() * 90e3, down: Math.random() * 600e3, procs: 312, battery: 84, plugged: true, uptime: 49000 };
  },
  async win_minimize() {}, async win_maximize() {}, async win_close() {},
};

const NX = {
  api: null, st: null, typingQ: [], lastUser: "", confirmId: null, audio: null,

  // ── python -> page ──────────────────────────────────────────────
  on(ev) {
    switch (ev.type) {
      case "state": this.setState(ev.state); break;
      case "chat": this.addMsg(ev.role, ev.text, ev.source); break;
      case "task": this.task(ev); break;
      case "intel": this.intel(ev); break;
      case "speak": this.speak(ev.audio, ev.text); break;
      case "mic": HUD.S.mic = ev.level; break;
      case "mic_status": this.micStatus(ev.ok, ev.device); break;
      case "confirm": this.showConfirm(ev); break;
      case "confirm_close": if (this.confirmId === ev.id) $("confirm").classList.remove("show"); break;
      case "reminder": this.toast("REMINDER", ev.message); break;
      case "memory": if (this.st) { this.st.memory = ev.facts; this.renderMemory(); } break;
    }
  },

  setState(s) {
    HUD.S.state = s;
    const el = $("stateText");
    const label = { IDLE: "STANDBY", RECOGNISING: "DECODING SPEECH" }[s] || s;
    el.textContent = label; el.dataset.s = s;
    const busy = ["THINKING", "PROCESSING", "RECOGNISING"].includes(s);
    $("micBtn").classList.toggle("live", s === "LISTENING");
    $("micBtn").classList.toggle("busy", busy);
    $("micLabel").textContent = s === "LISTENING" ? "LISTENING... SPEAK NOW"
      : s === "RECOGNISING" ? "DECODING..." : busy ? "WORKING..."
      : s === "SPEAKING" ? "TAP TO INTERRUPT" : "TAP TO SPEAK";
  },

  // ── chat ────────────────────────────────────────────────────────
  addMsg(role, text, source) {
    const chat = $("chat");
    const m = document.createElement("div");
    m.className = `msg ${role}`;
    const who = role === "user" ? (this.st?.config.user_name || "YOU") : (this.st?.config.nickname || "NEXUS");
    const time = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    m.innerHTML = `<div class="meta">${esc(who.toUpperCase())} // ${time}${source === "voice" ? " // VOICE" : ""}</div><div class="body"></div>`;
    chat.appendChild(m);
    const body = m.querySelector(".body");
    if (role === "assistant") {
      body.classList.add("typing");
      let i = 0;
      const step = () => {
        i = Math.min(text.length, i + Math.max(1, Math.ceil(text.length / 90)));
        body.textContent = text.slice(0, i); chat.scrollTop = chat.scrollHeight;
        if (i < text.length) requestAnimationFrame(step); else body.classList.remove("typing");
      };
      step();
    } else { body.textContent = text; }
    while (chat.children.length > 80) chat.firstChild.remove();
    chat.scrollTop = chat.scrollHeight;
  },

  task(ev) {
    const box = $("tasks");
    box.querySelector(".empty")?.remove();
    let el = ev.id && box.querySelector(`[data-id="${ev.id}"]`);
    if (!el) {
      el = document.createElement("div"); el.className = "task"; if (ev.id) el.dataset.id = ev.id;
      el.innerHTML = `<i class="ic"></i><span class="nm"></span><span class="ms"></span><span class="dt"></span>`;
      box.prepend(el);
    }
    el.className = `task ${ev.status === "running" || ev.status === "scheduled" ? "" : ev.status}`;
    el.querySelector(".nm").textContent = ev.name.replace(/_/g, " ");
    el.querySelector(".dt").textContent = ev.detail || "";
    el.querySelector(".ms").textContent = ev.ms != null ? `${ev.ms}ms` : ev.status === "scheduled" ? "SCHED" : "...";
    while (box.children.length > 25) box.lastChild.remove();
  },

  intel(ev) {
    const box = $("intelCards");
    $("radar").style.display = "none";
    let html = `<div class="icard"><h4>${esc(ev.kind)} // ${esc(ev.title)}</h4>`;
    if (ev.kind === "search") {
      html += (ev.items || []).map((x) => `<div class="res" data-url="${esc(x.url)}"><b>${esc(x.title)}</b><span>${esc(x.snippet)}</span></div>`).join("") || "<div class='empty'>NO RESULTS</div>";
    } else if (ev.kind === "weather") {
      const d = ev.data;
      html += `<div class="wx"><div class="t">${esc(d.temp_c)}°</div><div class="d">${esc(d.desc)}<small>FEELS ${esc(d.feels_c)}° · HUM ${esc(d.humidity)}% · WIND ${esc(d.wind_kmph)} KM/H</small></div></div>
        <div class="fc">${(d.forecast || []).map((f) => `<div>${esc(String(f.date).slice(5) || f.date)}<br>${esc(f.max)}° / ${esc(f.min)}°</div>`).join("")}</div>`;
    } else if (ev.kind === "video") {
      html += `<div class="res" data-url="${esc(ev.url)}"><img src="https://i.ytimg.com/vi/${esc(ev.id)}/hqdefault.jpg" alt=""><b>▶ ${esc(ev.title)}</b></div>`;
    } else if (ev.kind === "image") {
      html += `<img src="data:image/jpeg;base64,${ev.b64}" alt="">`;
    }
    box.innerHTML = html + "</div>";
    box.querySelectorAll("[data-url]").forEach((el) => el.onclick = () => this.api.send_text(`Open this link: ${el.dataset.url}`));
  },

  // ── voice out ───────────────────────────────────────────────────
  ensureAudio() {
    if (this.actx) return;
    const AC = window.AudioContext || window.webkitAudioContext;
    this.actx = new AC();
    this.analyser = this.actx.createAnalyser();
    this.analyser.fftSize = 256; this.analyser.smoothingTimeConstant = .72;
    this.analyser.connect(this.actx.destination);
    this.freq = new Uint8Array(this.analyser.frequencyBinCount);
    const tick = () => {
      if (this.playing) {
        this.analyser.getByteFrequencyData(this.freq);
        let s = 0; for (let i = 2; i < 60; i++) s += this.freq[i];
        HUD.S.amp = Math.min(1, s / 58 / 170); HUD.S.freq = this.freq;
      } else if (this.fakeSpeak) {
        HUD.S.amp = .25 + .35 * Math.abs(Math.sin(performance.now() / 90)); HUD.S.freq = null;
      } else { HUD.S.amp *= .85; HUD.S.freq = null; }
      requestAnimationFrame(tick);
    };
    tick();
  },

  speak(b64, text) {
    this.ensureAudio();
    if (this.actx.state === "suspended") this.actx.resume();
    this.stopSpeech();
    this.subtitle(text);
    const done = () => { this.playing = false; this.fakeSpeak = false; this.api.speech_done(); $("subtitle").textContent = ""; };
    if (b64) {
      const a = new Audio("data:audio/mpeg;base64," + b64);
      this.audio = a;
      try { this.actx.createMediaElementSource(a).connect(this.analyser); } catch (e) { /* plays without viz */ }
      a.onended = done; a.onerror = done;
      this.playing = true; this.setState("SPEAKING");
      a.play().catch(() => { this.playing = false; this.browserSpeak(text, done); });
    } else { this.browserSpeak(text, done); }
  },

  browserSpeak(text, done) {
    if (!("speechSynthesis" in window) || !text) return done();
    const u = new SpeechSynthesisUtterance(text);
    u.onend = done; u.onerror = done;
    this.fakeSpeak = true; this.setState("SPEAKING");
    speechSynthesis.speak(u);
  },

  stopSpeech() {
    if (this.audio) { this.audio.onended = null; this.audio.pause(); this.audio = null; }
    if ("speechSynthesis" in window) speechSynthesis.cancel();
    this.playing = false; this.fakeSpeak = false;
  },

  subtitle(text) {
    const el = $("subtitle"), words = String(text || "").split(/\s+/);
    clearInterval(this._subT);
    let i = 0; el.textContent = "";
    this._subT = setInterval(() => {
      i++; el.textContent = words.slice(Math.max(0, i - 14), i).join(" ");
      if (i >= words.length) clearInterval(this._subT);
    }, 260);
  },

  // ── voice in ────────────────────────────────────────────────────
  async micPress() {
    this.ensureAudio();
    if (HUD.S.state === "SPEAKING") { this.stopSpeech(); this.api.speech_done(); }
    const r = await this.api.mic_press();
    if (!r.ok) this.toast("MICROPHONE", r.error || "Not available");
  },
  micStatus(ok, device) {
    $("chipMic").classList.toggle("on", !!ok);
    $("chipMic").title = device || "No microphone";
    if (!ok) this.toast("MICROPHONE", "No working microphone found - typing still works.");
  },

  send() {
    const t = $("cmd").value.trim(); if (!t) return;
    this.ensureAudio();
    this.stopSpeech();
    $("cmd").value = "";
    this.api.send_text(t);
  },

  // ── confirm / memory / toasts ───────────────────────────────────
  showConfirm(ev) {
    this.confirmId = ev.id;
    $("cTitle").textContent = ev.title; $("cDetail").textContent = ev.detail;
    $("confirm").classList.add("show");
  },
  answer(ok) { $("confirm").classList.remove("show"); this.api.confirm_reply(this.confirmId, ok); },
  openMemory() { this.renderMemory(); $("memory").classList.add("show"); },
  closeMemory() { $("memory").classList.remove("show"); },
  renderMemory() {
    const list = this.st?.memory || [];
    $("memList").innerHTML = list.length ? list.map((m, i) => `<div class="m" style="animation-delay:${i * 30}ms"><p>${esc(m.fact)}</p><small>${esc(m.at)}</small><button onclick="NX.delMem(${i})">✕</button></div>`).join("")
      : `<div class="empty">MEMORY BANK EMPTY — tell your assistant about yourself</div>`;
  },
  async delMem(i) { this.st.memory = await this.api.delete_memory(i); this.renderMemory(); },
  toast(title, msg) {
    const t = document.createElement("div"); t.className = "toast";
    t.innerHTML = `<b>${esc(title)}</b>${esc(msg)}`; $("toasts").appendChild(t);
    setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 600); }, 6000);
  },
  clearChat() { $("chat").innerHTML = ""; $("tasks").innerHTML = `<div class="empty">NO ACTIVE TASKS</div>`; $("intelCards").innerHTML = ""; $("radar").style.display = ""; this.api.clear_chat(); },
  win(a) { this.api["win_" + a]?.(); },

  // ── status / chrome ─────────────────────────────────────────────
  applyState(st) {
    this.st = st;
    const c = st.config;
    document.documentElement.dataset.theme = c.theme || "cyan";
    HUD.refreshTheme();
    const nick = (c.nickname || "NEXUS").toUpperCase();
    HUD.S.nick = nick; document.title = nick;
    const bn = $("brandName"); bn.textContent = nick; bn.dataset.text = nick;
    const p = st.presets[c.provider];
    $("brandModel").textContent = st.configured ? `${(p?.label || c.provider).toUpperCase()} · ${c.model}` : "NOT LINKED";
    $("chipAI").classList.toggle("on", st.configured);
    $("chipAI").title = st.configured ? `${p?.label} / ${c.model}` : "No AI connected";
    $("chipMic").classList.toggle("on", !!st.mic?.ok);
    this.ticker();
  },

  ticker() {
    const c = this.st?.config || {};
    const items = [`${(c.nickname || "NEXUS").toUpperCase()} ONLINE`, `PROVIDER ${String(c.provider || "none").toUpperCase()}`, `MODEL ${c.model || "-"}`,
      `VOICE ${c.voice || "-"}`, `SKILLS ${this.st?.tools?.length || 0}`, "CTRL+SPACE = TALK", "ENTER = EXECUTE", "ESC = STOP SPEAKING",
      ...Array.from({ length: 4 }, () => "0x" + Math.floor(Math.random() * 0xffffff).toString(16).toUpperCase().padStart(6, "0"))];
    const s = items.join("   ◆   ") + "   ◆   ";
    $("ticker").textContent = s + s;
  },

  async pollStats() {
    try {
      const s = await this.api.stats();
      HUD.S.stats = s; HUD.pushNet(s.up, s.down);
      const kb = (b) => b > 1e6 ? (b / 1e6).toFixed(1) + " MB/s" : (b / 1e3).toFixed(0) + " KB/s";
      $("rUpS").textContent = kb(s.up); $("rDown").textContent = kb(s.down);
      $("rRam").textContent = `${s.ram_used} / ${s.ram_total} GB`; $("rProcs").textContent = s.procs;
      const u = s.uptime; $("rUp").textContent = `${Math.floor(u / 3600)}h ${String(Math.floor(u % 3600 / 60)).padStart(2, "0")}m`;
    } catch (e) { /* ignore */ }
    $("chipNet").classList.toggle("on", navigator.onLine);
  },

  clock() {
    const d = new Date();
    $("clock").textContent = d.toLocaleTimeString([], { hour12: false });
    $("date").textContent = d.toLocaleDateString([], { weekday: "short", day: "2-digit", month: "short", year: "numeric" }).toUpperCase();
  },

  glitchLoop() {
    document.querySelectorAll(".glitch").forEach((el) => { el.classList.add("g"); setTimeout(() => el.classList.remove("g"), 360); });
    setTimeout(() => this.glitchLoop(), 3500 + Math.random() * 4000);
  },

  // ── boot ────────────────────────────────────────────────────────
  async boot() {
    const lines = ["BIOS HANDSHAKE ........................ OK", "LOADING NEURAL CORE KERNEL .............. OK",
      "MOUNTING MEMORY BANKS ................... OK", "CALIBRATING AUDIO ARRAYS ................ OK",
      "SYNCING HOLOGRAPHIC RENDERER ............ OK", "ESTABLISHING AI UPLINK .................. OK",
      "LOADING SKILL MATRIX .................... OK", "SECURITY PROTOCOLS ARMED ................ OK", "ALL SYSTEMS NOMINAL."];
    const log = $("bootLog");
    for (let i = 0; i < lines.length; i++) {
      const [a, b] = lines[i].split(/(OK)$/);
      log.innerHTML += `&gt; ${esc(a)}${b ? '<span class="ok">OK</span>' : ""}\n`;
      log.scrollTop = log.scrollHeight;
      const p = Math.round((i + 1) / lines.length * 100);
      $("bootFill").style.width = p + "%"; $("bootPct").textContent = p;
      await sleep(170 + Math.random() * 120);
    }
    await sleep(350);
  },

  async init() {
    HUD.start();
    this.clock(); setInterval(() => this.clock(), 1000);
    const bootDone = this.boot();
    this.api = await getApi();
    const st = await this.api.ui_ready();
    this.applyState(st);
    this.fillSetup();
    await bootDone;
    $("boot").classList.add("gone");
    $("app").classList.add("on");
    setTimeout(() => $("boot").remove(), 1000);
    this.pollStats(); setInterval(() => this.pollStats(), 1000);
    this.glitchLoop();
    this.bindKeys();
    if (!st.configured) this.openSetup(false);
    else setTimeout(() => this.api.greet(), 900);
  },

  bindKeys() {
    $("cmd").addEventListener("keydown", (e) => { if (e.key === "Enter") this.send(); });
    $("sendBtn").onclick = () => this.send();
    $("micBtn").onclick = () => this.micPress();
    $("reactor").onclick = () => this.micPress();   // the core itself is a talk button
    window.addEventListener("keydown", (e) => {
      if (e.ctrlKey && e.code === "Space") { e.preventDefault(); this.micPress(); }
      if (e.key === "Escape") {
        if ($("memory").classList.contains("show")) return this.closeMemory();
        if (HUD.S.state === "SPEAKING") { this.stopSpeech(); this.api.speech_done(); $("subtitle").textContent = ""; }
      }
    });
    // any first interaction unlocks audio
    window.addEventListener("pointerdown", () => { this.ensureAudio(); this.actx.state === "suspended" && this.actx.resume(); }, { once: true });
  },

  // ── setup / settings ────────────────────────────────────────────
  fillSetup() {
    const st = this.st, c = st.config;
    $("fProvider").innerHTML = Object.entries(st.presets).map(([k, v]) => `<option value="${k}">${esc(v.label)}</option>`).join("");
    $("fVoice").innerHTML = st.voices.map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
    $("fLang").innerHTML = st.languages.map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
    $("themes").innerHTML = Object.entries(THEMES).map(([k, v]) => `<button type="button" data-t="${k}" title="${k}" style="background:${v};box-shadow:0 0 12px ${v}"></button>`).join("");
    $("themes").querySelectorAll("button").forEach((b) => b.onclick = () => this.pickTheme(b.dataset.t));
    $("fNick").oninput = () => { const v = ($("fNick").value || "YOUR AI").toUpperCase(); $("nickPreview").textContent = v; $("nickPreview").dataset.text = v; };
    $("fKey").oninput = () => { clearTimeout(this._kt); this._kt = setTimeout(() => this.detect(), 350); };
    $("fProvider").onchange = () => this.providerChanged(true);
    if (!this._bound) {
      this._bound = true;
      $("setup").addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.tagName === "INPUT") this.activate(true); });
    }
  },

  loadForm() {
    const c = this.st.config;
    $("fNick").value = c.nickname || ""; $("fNick").oninput();
    $("fUser").value = c.user_name || "";
    $("fKey").value = ""; $("fKey").placeholder = c.has_key ? `Saved key ${c.key_hint} — leave blank to keep` : "Paste any AI API key - Claude, Gemini, OpenAI, Groq, OpenRouter...";
    $("fProvider").value = c.provider || "anthropic";
    $("fModel").value = c.model || ""; $("fBase").value = c.base_url || "";
    $("fVoice").value = c.voice; $("fLang").value = c.language;
    $("fVoiceOn").checked = !!c.voice_enabled; $("fAuto").checked = !!c.auto_listen;
    this.pickTheme(c.theme || "cyan", true);
    this.providerChanged(false);
    if (!$("fModel").value) $("fModel").value = this.st.presets[$("fProvider").value]?.model || "";
    $("setupMsg").textContent = ""; $("setupMsg").className = "setup-msg";
    $("saveAnyway").style.display = "none"; $("detectBadge").classList.remove("show");
  },

  openSetup(settings) {
    this.settingsMode = settings;
    $("setup").classList.toggle("settings", settings);
    $("setupKicker").textContent = settings ? "SYSTEM CONFIGURATION // SETTINGS" : "FIRST BOOT // SYSTEM INITIALIZATION";
    $("activateBtn").querySelector("span").textContent = settings ? "SAVE" : "ACTIVATE";
    this.loadForm();
    $("setup").classList.add("show");
    setTimeout(() => $(settings ? "fNick" : "fNick").focus(), 300);
  },
  openSettings() { this.openSetup(true); },
  closeSettings() { this.pickTheme(this.st.config.theme || "cyan", true); $("setup").classList.remove("show"); },

  pickTheme(t, silent) {
    this.theme = t;
    document.documentElement.dataset.theme = t; HUD.refreshTheme();
    $("themes").querySelectorAll("button").forEach((b) => b.classList.toggle("sel", b.dataset.t === t));
  },

  async detect() {
    const key = $("fKey").value.trim(); if (!key) return;
    const p = await this.api.detect_provider(key);
    if (p && this.st.presets[p]) {
      $("fProvider").value = p; this.providerChanged(true);
      const b = $("detectBadge"); b.textContent = "DETECTED: " + this.st.presets[p].label.toUpperCase();
      b.classList.remove("show"); void b.offsetWidth; b.classList.add("show");
      this.scanModels(true);
    }
  },

  providerChanged(user) {
    const p = $("fProvider").value, pr = this.st.presets[p] || {};
    const local = ["ollama", "lmstudio", "custom"].includes(p);
    $("baseRow").style.display = local || $("fBase").value ? "" : "none";
    $("fBase").placeholder = pr.base || "https://your-endpoint/v1";
    if (user) {
      if (local) $("fBase").value = pr.base || "";
      else $("fBase").value = "";
      const known = Object.values(this.st.presets).map((x) => x.model);
      if (!$("fModel").value || known.includes($("fModel").value)) $("fModel").value = pr.model || "";
      $("modelList").innerHTML = "";
    }
  },

  async scanModels(quiet) {
    const btn = $("scanBtn"); btn.textContent = "...";
    const r = await this.api.fetch_models($("fProvider").value, $("fKey").value.trim(), $("fBase").value.trim());
    btn.textContent = "SCAN";
    const msg = $("setupMsg");
    if (r.ok) {
      $("modelList").innerHTML = r.models.map((m) => `<option value="${esc(m)}">`).join("");
      msg.className = "setup-msg ok"; msg.textContent = `${r.models.length} models found — click the MODEL box to pick one.`;
    } else if (!quiet) { msg.className = "setup-msg"; msg.textContent = r.error; }
  },

  async activate(test) {
    const v = {
      nickname: $("fNick").value.trim(), user_name: $("fUser").value.trim(), provider: $("fProvider").value,
      api_key: $("fKey").value.trim(), model: $("fModel").value.trim(), base_url: $("fBase").value.trim(),
      voice: $("fVoice").value, language: $("fLang").value, theme: this.theme || "cyan",
    };
    if (this.settingsMode) { v.voice_enabled = $("fVoiceOn").checked; v.auto_listen = $("fAuto").checked; }
    const msg = $("setupMsg"); msg.className = "setup-msg";
    if (!v.nickname) { msg.textContent = "Give your assistant a nickname."; return $("fNick").focus(); }
    const local = ["ollama", "lmstudio"].includes(v.provider);
    if (!v.api_key && !local && !this.st.config.has_key) { msg.textContent = "Paste an API key."; return $("fKey").focus(); }
    if (!v.model) { msg.textContent = "Choose a model (press SCAN to list them)."; return $("fModel").focus(); }
    const c = this.st.config;
    const linkChanged = v.api_key || v.provider !== c.provider || v.model !== c.model || v.base_url !== (c.base_url || "");
    const btn = $("activateBtn"); btn.classList.add("busy"); btn.querySelector("span").textContent = "LINKING...";
    msg.className = "setup-msg ok"; msg.textContent = test && linkChanged ? "Establishing neural uplink..." : "Saving...";
    const r = await this.api.save_setup(v, !!(test && linkChanged));
    btn.classList.remove("busy"); btn.querySelector("span").textContent = this.settingsMode ? "SAVE" : "ACTIVATE";
    if (!r.ok) {
      msg.className = "setup-msg"; msg.textContent = "UPLINK FAILED: " + r.error;
      $("saveAnyway").style.display = ""; return;
    }
    const first = !this.st.configured;
    this.applyState(r.state);
    $("setup").classList.remove("show");
    if (first) { this.toast("ONLINE", `${v.nickname.toUpperCase()} is now linked to ${r.state.presets[v.provider]?.label}.`); setTimeout(() => this.api.greet(), 500); }
    else this.toast("SETTINGS", "Configuration saved.");
  },

  toggleKey() { const k = $("fKey"); k.type = k.type === "password" ? "text" : "password"; },
};
window.NX = NX;

function getApi() {
  if (new URLSearchParams(location.search).has("mock")) return Promise.resolve(MOCK);
  if (window.pywebview?.api) return Promise.resolve(window.pywebview.api);
  return new Promise((res) => {
    window.addEventListener("pywebviewready", () => res(window.pywebview.api), { once: true });
    setTimeout(() => res(window.pywebview?.api || MOCK), 6000);   // opened outside the app
  });
}

window.addEventListener("DOMContentLoaded", () => NX.init());
