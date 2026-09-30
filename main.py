"""
ROBOO - a personal AI assistant with a holographic HUD.

Python runs the brain (any AI provider + tools + voice); the interface is an
HTML/Canvas page rendered by Edge WebView2 through pywebview. The two talk
over pywebview's JS bridge: the page calls methods on `Api`, Python pushes
events with `NX.on({...})`.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
import uuid

# Let the page play the assistant's voice without a click first. DirectComposition
# is off because on some GPU drivers it presents a frameless WebView2 window as
# solid black; GPU rasterisation stays on, so the animations stay smooth.
os.environ.setdefault("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
                      "--autoplay-policy=no-user-gesture-required "
                      "--disable-direct-composition")

import psutil
import webview

from core import config, memory, providers, tools, voice
from core.config import BASE_DIR

APP_NAME = "ROBOO"


def system_prompt(cfg: dict) -> str:
    nick = cfg.get("nickname") or "Roboo"
    user = cfg.get("user_name") or "the user"
    return f"""You are {nick}, a personal AI assistant that lives on {user}'s Windows computer - \
in the spirit of JARVIS: calm, sharp, a little witty and completely dependable.

Your name is {nick}. The user's name is {user}; use it now and then, naturally.

Your replies are spoken aloud through a voice, so:
- Keep them short: one to three sentences unless the user asks for detail.
- No markdown, bullet lists, emojis or URLs in spoken replies.
- Answer in the same language and script the user used (English, Hindi, or Hinglish in Roman script).

You control this computer through your tools. When the user asks for an action, call the tool - \
never say you did something you did not do. For anything that changes over time (news, prices, \
scores, weather, recent events) use web_search or get_weather instead of guessing. If a tool \
reports the user declined, accept it and do not retry. When the user tells you something worth \
keeping about themselves, save it with remember.

Each user message starts with the current local date and time in square brackets.

What you already know about {user}:
{memory.as_prompt()}"""


class Assistant:
    def __init__(self, api: "Api"):
        self.api = api
        self.cfg = config.load()
        self.session: providers.Session | None = None
        self.active_model: str | None = None
        self._dead: dict[str, float] = {}          # model -> time it may be retried
        self._catalog: list[str] | None = None     # models this key can use
        self._tools_this_turn: list[str] = []
        self._jobs: queue.Queue = queue.Queue()
        self._confirms: dict[str, tuple[threading.Event, list]] = {}
        self.busy = False
        tools.ctx.emit = self.emit
        tools.ctx.confirm = self.confirm
        tools.ctx.speak = self.say
        tools.ctx.vision = lambda img, q: self._ensure_session().vision(img, q)
        self.listener = voice.Listener(self._on_level, self._on_heard, self._on_mic_state)
        threading.Thread(target=self._worker, daemon=True, name="brain").start()

    # -- plumbing --------------------------------------------------------
    def emit(self, kind: str, data: dict | None = None) -> None:
        self.api.push({"type": kind, **(data or {})})

    def state(self, s: str) -> None:
        self.emit("state", {"state": s})

    def _pick_model(self) -> str:
        """The configured model, unless it failed recently - then the fallback in use."""
        now = time.time()
        want = self.cfg.get("model", "")
        if self._dead.get(want, 0) < now:
            return want
        if self.active_model and self._dead.get(self.active_model, 0) < now:
            return self.active_model
        return self._next_fallback() or want

    def _next_fallback(self) -> str | None:
        if self._catalog is None:
            try:
                self._catalog = providers.list_models(self.cfg)
            except Exception:
                self._catalog = []
        now = time.time()
        for m in providers.fallback_models(self.cfg, self._catalog):
            if self._dead.get(m, 0) < now:
                return m
        return None

    def _ensure_session(self) -> providers.Session:
        model = self._pick_model()
        if self.session is None or self.session.turns() > 60 or self.session.model != model:
            self.session = providers.make_session({**self.cfg, "model": model},
                                                  system_prompt(self.cfg), tools.schemas())
        return self.session

    def _ask(self, text: str) -> str:
        """One turn, stepping to another model when the current one is overloaded,
        rate-limited or retired - the way a busy model should never stall you."""
        tried = 0
        while True:
            self._tools_this_turn = []
            session = self._ensure_session()
            try:
                return session.run(text, self._run_tool, self.emit)
            except providers.ProviderError as e:
                self.session = None
                if not e.transient:
                    raise
                failed = session.model
                self._dead[failed] = time.time() + 300          # rest it for 5 minutes
                if self._tools_this_turn:
                    # Actions already happened - re-running the turn would repeat them.
                    done = "; ".join(self._tools_this_turn)
                    return f"Ho gaya. ({done})"
                nxt = self._next_fallback() if tried < 4 else None
                if not nxt:
                    local = self._local_command(text)
                    if local:
                        return local
                    raise
                tried += 1
                self.active_model = nxt
                print(f"[AI] {failed} failed ({e.status}) - switching to {nxt}", flush=True)
                self.emit("task", {"name": "model switch", "status": "warn",
                                   "detail": f"{failed} busy ({e.status}) -> {nxt}"})

    def _local_command(self, text: str) -> str | None:
        """When no AI is reachable, still handle 'open X' / 'X kholo' / 'play X'."""
        import re
        t = re.sub(r"^\[[^\]]*\]\s*", "", text).strip().rstrip(".!?")
        if re.search(r"incognito|inprivate|private (window|mode)", t, re.I):
            b = next((x for x in ("edge", "firefox", "brave") if x in t.lower()), "chrome")
            return tools.execute("open_url", {"browser": b, "private": True})
        m = (re.match(r"^(?:please\s+)?(?:open|launch|start)\s+(.+)$", t, re.I)
             or re.match(r"^(.+?)\s+(?:kholo|khol do|khol|open karo|open kar do|open kr do|"
                         r"open kro|chalu karo|start karo)$", t, re.I))
        if m:
            return tools.execute("open_app", {"name": m.group(1).strip()}) + \
                " (AI abhi busy hai, isliye seedha khola.)"
        m = (re.match(r"^play\s+(.+)$", t, re.I)
             or re.match(r"^(.+?)\s+(?:chalao|play karo|play kro|bajao)$", t, re.I))
        if m:
            return tools.execute("play_youtube", {"query": m.group(1).strip()})
        return None

    def reset(self) -> None:
        self.cfg = config.load()
        self.session = None
        self.active_model = None
        self._dead: dict[str, float] = {}
        self._catalog = None
        self.listener.language = self.cfg.get("language", "en-IN")
        self.listener.auto = bool(self.cfg.get("auto_listen"))

    # -- confirmation gate ---------------------------------------------------
    def confirm(self, title: str, detail: str) -> bool:
        cid = uuid.uuid4().hex[:8]
        ev, box = threading.Event(), [False]
        self._confirms[cid] = (ev, box)
        self.emit("confirm", {"id": cid, "title": title, "detail": detail})
        self.say_local(f"Confirmation needed: {title.lower()}.")
        ev.wait(timeout=90)
        self._confirms.pop(cid, None)
        self.emit("confirm_close", {"id": cid})
        return box[0]

    def confirm_reply(self, cid: str, ok: bool) -> None:
        item = self._confirms.get(cid)
        if item:
            item[1][0] = bool(ok)
            item[0].set()

    # -- voice ---------------------------------------------------------------
    def _on_level(self, lv: float) -> None:
        self.emit("mic", {"level": round(lv, 3)})

    def _on_mic_state(self, s: str) -> None:
        if not self.busy:
            self.state(s)

    def _on_heard(self, text: str) -> None:
        self.submit(text, source="voice")

    def say(self, text: str) -> None:
        """Speak `text` (used for reminders and other unprompted speech)."""
        self.emit("chat", {"role": "assistant", "text": text})
        self.say_local(text)

    def say_local(self, text: str) -> None:
        if not self.cfg.get("voice_enabled", True):
            return
        audio = voice.synthesize(text, self.cfg.get("voice", "en-IN-PrabhatNeural"))
        self.listener.paused = True
        self.emit("speak", {"audio": audio, "text": text})

    # -- the turn ------------------------------------------------------------
    def submit(self, text: str, source: str = "text") -> None:
        text = (text or "").strip()
        if text:
            self._jobs.put((text, source))

    def _worker(self):
        while True:
            text, source = self._jobs.get()
            self.busy = True
            self.emit("chat", {"role": "user", "text": text, "source": source})
            self.state("THINKING")
            try:
                if not config.is_configured(self.cfg):
                    reply = "Pehle setup complete karo - API key aur nickname daalo."
                else:
                    stamp = time.strftime("%A %d %B %Y, %I:%M %p")
                    reply = self._ask(f"[{stamp}] {text}")
                reply = reply or "Done."
            except providers.ProviderError as e:
                reply = f"AI provider error: {e}"
                self.session = None
            except Exception as e:
                reply = f"Kuch gadbad ho gayi: {e}"
                self.session = None
            self.emit("chat", {"role": "assistant", "text": reply})
            self.busy = False
            if self.cfg.get("voice_enabled", True):
                self.state("SPEAKING")
                self.say_local(reply)
            else:
                self.state("LISTENING" if self.listener.auto else "IDLE")

    def _run_tool(self, name: str, args: dict) -> str:
        tid = uuid.uuid4().hex[:6]
        brief = ", ".join(f"{k}={str(v)[:40]}" for k, v in args.items())
        self.emit("task", {"id": tid, "name": name, "status": "running", "detail": brief})
        self.state("PROCESSING")
        t0 = time.time()
        out = tools.execute(name, args)
        self._tools_this_turn.append(out[:80])
        ok = not any(w in out[:80].lower() for w in ("failed", "unknown tool", "bad arguments",
                                                     "not found", "declined"))
        self.emit("task", {"id": tid, "name": name, "status": "done" if ok else "warn",
                           "detail": out[:160], "ms": int((time.time() - t0) * 1000)})
        self.state("THINKING")
        return out


class Api:
    """Every public method here is callable from the page as pywebview.api.<name>()."""

    def __init__(self):
        self._window = None
        self._ready = False
        self._closed = False
        self._pending: list[dict] = []
        self._lock = threading.Lock()
        self._net = psutil.net_io_counters()
        self._net_t = time.time()
        self._assistant = Assistant(self)

    # -- python -> page ------------------------------------------------------
    def push(self, event: dict) -> None:
        with self._lock:
            if self._closed:
                return
            if not self._ready or not self._window:
                self._pending.append(event)
                return
        try:
            self._window.evaluate_js(f"window.NX && NX.on({json.dumps(event)})")
        except Exception as e:
            if not self._closed:
                print(f"[UI] push failed: {e}")

    # -- page -> python ------------------------------------------------------
    def ui_ready(self):
        print("[UI] ready", flush=True)
        with self._lock:
            self._ready = True
            pending, self._pending = self._pending, []
        for ev in pending:
            self.push(ev)
        a = self._assistant
        a.reset()
        if not a.listener.ok:
            threading.Thread(target=self._start_mic, daemon=True).start()
        return self.get_state()

    def _start_mic(self):
        ok = self._assistant.listener.start()
        self.push({"type": "mic_status", "ok": ok, "device": self._assistant.listener.device_name})

    def get_state(self):
        cfg = config.load()
        key = cfg.get("api_key", "")
        return {
            "configured": config.is_configured(cfg),
            "config": {**cfg, "api_key": "", "has_key": bool(key),
                       "key_hint": (key[:6] + "..." + key[-4:]) if len(key) > 12 else ""},
            "presets": {k: {"label": v["label"], "base": v["base"], "model": v["model"]}
                        for k, v in providers.PRESETS.items()},
            "voices": voice.VOICES, "languages": voice.LANGUAGES,
            "memory": memory.facts(),
            "mic": {"ok": self._assistant.listener.ok,
                    "device": self._assistant.listener.device_name},
            "tools": [t["name"] for t in tools.schemas()],
        }

    def detect_provider(self, key):
        return providers.detect_provider(key)

    def fetch_models(self, provider, key, base_url):
        cfg = config.load()
        key = key or (cfg.get("api_key") if cfg.get("provider") == provider else "")
        try:
            return {"ok": True, "models": providers.list_models(
                {"provider": provider, "api_key": key, "base_url": base_url})}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def save_setup(self, values, test=True):
        cur = config.load()
        if not values.get("api_key") and values.get("provider") == cur.get("provider"):
            values["api_key"] = cur.get("api_key", "")   # blank field = keep the saved key
        cand = {**cur, **values}
        if test:
            try:
                s = providers.make_session(cand, "Reply with the single word OK.", [])
                s.run("ping", lambda *_: "", lambda *_: None, max_steps=1)
            except Exception as e:
                return {"ok": False, "error": str(e)}
        config.save(cand)
        self._assistant.reset()
        return {"ok": True, "state": self.get_state()}

    def save_prefs(self, values):
        allowed = {k: values[k] for k in ("theme", "voice", "language", "voice_enabled",
                                          "auto_listen") if k in values}
        config.save(allowed)
        self._assistant.reset()
        return self.get_state()

    def send_text(self, text):
        self._assistant.submit(text, "text")

    def mic_press(self):
        l = self._assistant.listener
        if not l.ok:
            return {"ok": False, "error": "Microphone not available"}
        if l.armed:
            l.disarm()
            self._assistant.state("IDLE")
        else:
            l.arm()
        return {"ok": True, "armed": l.armed}

    def speech_done(self):
        l = self._assistant.listener
        l.paused = False
        if not self._assistant.busy:
            self._assistant.state("LISTENING" if l.auto else "IDLE")

    def confirm_reply(self, cid, ok):
        self._assistant.confirm_reply(cid, ok)

    def clear_chat(self):
        self._assistant.session = None

    def delete_memory(self, index):
        memory.delete_index(int(index))
        return memory.facts()

    def greet(self):
        cfg = config.load()
        h = time.localtime().tm_hour
        part = "morning" if h < 12 else "afternoon" if h < 17 else "evening"
        user = cfg.get("user_name") or ""
        text = f"Good {part}{', ' + user if user else ''}. {cfg.get('nickname') or 'Roboo'} " \
               f"online. All systems nominal."
        threading.Thread(target=self._assistant.say, args=(text,), daemon=True).start()

    def stats(self):
        now = time.time()
        nc = psutil.net_io_counters()
        dt = max(0.001, now - self._net_t)
        up = (nc.bytes_sent - self._net.bytes_sent) / dt
        down = (nc.bytes_recv - self._net.bytes_recv) / dt
        self._net, self._net_t = nc, now
        bat = psutil.sensors_battery()
        vm = psutil.virtual_memory()
        disk = psutil.disk_usage(os.path.expanduser("~")[:3] if os.name == "nt" else "/")
        return {"cpu": psutil.cpu_percent(interval=None),
                "cores": psutil.cpu_percent(interval=None, percpu=True),
                "ram": vm.percent, "ram_used": round(vm.used / 1e9, 1),
                "ram_total": round(vm.total / 1e9, 1), "disk": disk.percent,
                "up": up, "down": down, "procs": len(psutil.pids()),
                "battery": bat.percent if bat else None,
                "plugged": bat.power_plugged if bat else None,
                "uptime": int(now - psutil.boot_time())}

    # -- window --------------------------------------------------------------
    def win_minimize(self):
        self._window.minimize()

    def win_maximize(self):
        self._window.toggle_fullscreen()

    def win_close(self):
        self._window.destroy()


def _fill_work_area() -> None:
    """Frameless windows ignore `maximized`, so size the window to the screen's
    work area (everything except the taskbar) directly."""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        hwnd = u.FindWindowW(None, APP_NAME)
        area = wintypes.RECT()
        u.SystemParametersInfoW(0x0030, 0, ctypes.byref(area), 0)   # SPI_GETWORKAREA
        u.SetWindowPos(hwnd, 0, area.left, area.top, area.right - area.left,
                       area.bottom - area.top, 0x0004 | 0x0040)      # NOZORDER | SHOWWINDOW
    except Exception as e:
        print(f"[UI] could not fit window: {e}")


def main():
    api = Api()
    # Opens maximised: the HUD is designed to own the screen, and it sidesteps
    # display-scaling maths. The restore size below is used when un-maximised.
    window = webview.create_window(
        APP_NAME, url=str(BASE_DIR / "web" / "index.html"), js_api=api,
        width=1280, height=760, min_size=(1100, 660), frameless=True,
        easy_drag=False, background_color="#01060a",
    )
    api._window = window
    window.events.shown += lambda: threading.Timer(0.2, _fill_work_area).start()
    window.events.closing += lambda: setattr(api, "_closed", True)
    webview.start(http_server=True, debug=bool(os.environ.get("ROBOO_DEBUG")))


if __name__ == "__main__":
    main()
