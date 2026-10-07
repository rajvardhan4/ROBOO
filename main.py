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
import sys
import threading
import time
import uuid


def _unblock_bundle() -> None:
    """Windows tags every file unzipped from a downloaded zip as 'from the
    internet' (the Zone.Identifier stream, a.k.a. Mark of the Web). .NET then
    refuses to load the bundled Python.Runtime.dll that drives the window, and
    the app dies with 'Failed to resolve Python.Runtime.Loader.Initialize'.
    Removing the tag from our own bundled files - before .NET is touched - fixes
    it without asking anyone to right-click -> Properties -> Unblock."""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    root = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    for base in {root, os.path.dirname(sys.executable)}:
        for dirpath, _dirs, files in os.walk(base):
            for f in files:
                if f.lower().endswith((".dll", ".pyd", ".exe")):
                    try:
                        os.remove(os.path.join(dirpath, f) + ":Zone.Identifier")
                    except OSError:
                        pass                    # not tagged, or not ours to change


_unblock_bundle()

# Let the page play the assistant's voice without a click first. DirectComposition
# is off because on some GPU drivers it presents a frameless WebView2 window as
# solid black; GPU rasterisation stays on, so the animations stay smooth.
os.environ.setdefault("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
                      "--autoplay-policy=no-user-gesture-required "
                      "--disable-direct-composition")

import psutil
import webview

from core import config, fast, memory, providers, tools, voice
from core.config import RES_DIR

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

Doing tasks properly:
- Finish the WHOLE request. "Notepad kholo aur hello likho" means open_app, then type_text - \
not just the first step. Keep calling tools until every part is done, then reply once.
- Be fast: when steps do not need to see an earlier result, call them ALL in one response - \
they run in the order you list them. "Notepad kholo aur hello likho" = one response with \
open_app, wait, press_keys(ctrl+n), type_text. Only wait for results you actually need to read \
(search results, file lists, weather).
- A website in a specific browser is one call: open_url with url and browser \
("chrome mein gmail kholo" -> open_url(url="mail.google.com", browser="chrome")).
- After opening an app you are about to type into or send keys to, call wait(1.5) first so it \
has focus.
- To write NEW text in Notepad, Word or any editor, first press_keys("ctrl+n") so a fresh \
document opens - these apps reopen old files, and typing into one of those would change it.
- To do something inside an app (new tab, save, search box), use press_keys with its shortcut \
(new tab = ctrl+t, save = ctrl+s, find = ctrl+f, address bar = ctrl+l, close tab = ctrl+w).
- If you cannot do something with your tools, say so plainly in one sentence - never pretend.
- If a step fails, read the error, try one sensible alternative, then report honestly.

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
        self._tools_this_turn: list[tuple[str, dict, str]] = []
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

    def _catalog_models(self) -> list[str]:
        if self._catalog is None:
            try:
                self._catalog = providers.list_models(self.cfg)
            except Exception:
                self._catalog = []
        return self._catalog

    def _pick_model(self) -> str:
        """The speed mode's model (Gemini) or the configured one - unless it failed
        recently, then the fallback in use."""
        now = time.time()
        alive = lambda m: m and self._dead.get(m, 0) < now
        want = self.cfg.get("model", "")
        if providers.preset(self.cfg.get("provider", "")).get("kind") == "gemini":
            cat = self._catalog_models()
            tier = [m for m in providers.mode(self.cfg)["gemini"] if m in cat or not cat]
            want = next((m for m in tier if alive(m)), want)
        if alive(want):
            return want
        if alive(self.active_model):
            return self.active_model
        return self._next_fallback() or want

    def _next_fallback(self) -> str | None:
        now = time.time()
        cat = self._catalog_models()
        tier = []
        if providers.preset(self.cfg.get("provider", "")).get("kind") == "gemini":
            # Stay in the mode's own class of model first (flash before lite in LOW).
            tier = [m for m in providers.mode(self.cfg)["gemini"] if m in cat]
            tier += [m for m in providers.MODES["medium"]["gemini"] + providers.MODES["fast"]["gemini"]
                     if m in cat and m not in tier]
        for m in tier + providers.fallback_models(self.cfg, cat):
            if self._dead.get(m, 0) < now:
                return m
        return None

    def _ensure_session(self) -> providers.Session:
        model, md = self._pick_model(), providers.mode(self.cfg)
        if (self.session is None or self.session.turns() > 60 or self.session.model != model
                or getattr(self.session, "_mode", None) != md["label"]):
            self.session = providers.make_session(
                {**self.cfg, "model": model, "_think": md["think"], "_effort": md["effort"]},
                system_prompt(self.cfg), tools.schemas())
            self.session._mode = md["label"]
            self.emit("model", {"model": model, "mode": self.cfg.get("mode", "fast")})
        return self.session

    def set_mode(self, name: str) -> str:
        if name not in providers.MODES:
            return "Unknown mode."
        self.cfg = config.save({"mode": name})
        self.session = None
        label = providers.MODES[name]["label"]
        self.emit("mode", {"mode": name})
        hi = not str(self.cfg.get("language", "")).startswith(("en-US", "en-GB"))
        return f"{label} mode on." if not hi else f"{label} mode chalu kar diya."

    def _ask(self, text: str) -> str:
        """One turn, stepping to another model when the current one is overloaded,
        rate-limited or retired - the way a busy model should never stall you."""
        tried = 0
        while True:
            self._tools_this_turn = []
            session = self._ensure_session()
            md = providers.mode(self.cfg)
            # Ending the turn right after the first action is only safe for one-step
            # requests - "notepad kholo aur hello likho" must reach the typing step.
            quick = (lambda n, a, o: fast.quick_reply(self.cfg, n, a, o)) \
                if md["quick"] and fast.single_step(text) else None
            try:
                return session.run(text, self._run_tool, self.emit,
                                   max_steps=md["steps"], quick=quick)
            except providers.ProviderError as e:
                self.session = None
                # Only *reading* tools ran (search, files, weather)? Then nothing on the
                # PC changed, and another model can safely redo the turn and answer.
                if all(n in fast.READ_ONLY for n, _, _ in self._tools_this_turn):
                    self._tools_this_turn = []
                if self._tools_this_turn and e.status in (-1, 429, 500, 502, 503, 504, 529):
                    # Actions already happened - re-running the turn would repeat them.
                    return self._summary()
                if e.status == -1:
                    # No internet: no model is to blame, so don't bench any of them.
                    local = self._local_command(text)
                    if local:
                        return local
                    raise providers.ProviderError(
                        "Internet connection nahi mil raha - net check karke dobara bolo.", -1)
                if not e.transient:
                    raise
                failed = session.model
                # Retired models (404) are skipped for the day; overloaded ones (503) for
                # 30 minutes; rate-limited ones (429 - free keys allow a few calls a minute)
                # for one minute, so a model that is down does not cost every command.
                self._dead[failed] = time.time() + {404: 86400, 429: 60}.get(e.status, 1800)
                if self._tools_this_turn:
                    return self._summary()
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

    def _summary(self) -> str:
        """A spoken 'done' for actions that ran before the AI could phrase one."""
        said = [fast.quick_reply(self.cfg, n, a, o) for n, a, o in self._tools_this_turn
                if n != "wait"]
        said = [s for s in said if s and s not in ("Ho gaya.", "Done.")]
        return " ".join(dict.fromkeys(said)) or "Ho gaya."

    def _instant(self, text: str) -> str | None:
        """Everyday commands that need no AI: run them now (see core/fast.py)."""
        m = fast.mode_command(text)
        if m:
            return self.set_mode(m)
        hit = fast.instant(text, self.cfg)
        if not hit:
            return None
        name, args = hit
        out = self._run_tool(name, args)
        return fast.quick_reply(self.cfg, name, args, out) or out

    def _local_command(self, text: str) -> str | None:
        """When no AI is reachable, still handle 'open X' / 'X kholo' / 'play X'."""
        import re
        done = self._instant(text)
        if done:
            return done
        t = re.sub(r"^\[[^\]]*\]\s*", "", text).strip().rstrip(".!?")
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
        # Sentence by sentence: the first sentence starts playing while the rest
        # are still being synthesised, instead of waiting for the whole reply.
        chunks = voice.split_sentences(text) or [text]
        sid, v = uuid.uuid4().hex[:6], self.cfg.get("voice", "en-IN-PrabhatNeural")
        self.listener.paused = True

        def synth(i: int, chunk: str) -> None:
            self.emit("speak", {"id": sid, "seq": i, "total": len(chunks), "text": chunk,
                                "full": text, "audio": voice.synthesize(chunk, v)})

        for i, chunk in enumerate(chunks[1:], 1):
            threading.Thread(target=synth, args=(i, chunk), daemon=True).start()
        synth(0, chunks[0])

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
                instant = self._instant(text)
                if instant:
                    reply = instant
                elif not config.is_configured(self.cfg):
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
        self._tools_this_turn.append((name, args, out))
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
            "modes": {k: v["label"] for k, v in providers.MODES.items()},
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
                                          "auto_listen", "mode") if k in values}
        config.save(allowed)
        self._assistant.reset()
        return self.get_state()

    def set_mode(self, name):
        msg = self._assistant.set_mode(name)
        return {"ok": name in providers.MODES, "message": msg}

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
        self._closed = True          # stop pushing events into a window being torn down
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
        APP_NAME, url=str(RES_DIR / "web" / "index.html"), js_api=api,
        width=1280, height=760, min_size=(1100, 660), frameless=True,
        easy_drag=False, background_color="#01060a",
    )
    api._window = window
    window.events.shown += lambda: threading.Timer(0.2, _fill_work_area).start()
    window.events.closing += lambda: setattr(api, "_closed", True)
    window.events.closed += lambda: setattr(api, "_closed", True)
    webview.start(http_server=True, debug=bool(os.environ.get("ROBOO_DEBUG")))


WEBVIEW2_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"


def _message(text: str, error: bool = True) -> None:
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, APP_NAME, 0x10 if error else 0x40)
    except Exception:
        print(text)


def _webview2_installed() -> bool:
    """Edge WebView2 draws the HUD. Windows 11 always has it; some older
    Windows 10 PCs do not, and pywebview would silently fall back to Internet
    Explorer and show a broken page."""
    if os.name != "nt":
        return True
    import winreg
    guid = r"{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    for hive, path in ((winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{guid}"),
                       (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{guid}"),
                       (winreg.HKEY_CURRENT_USER, rf"Software\Microsoft\EdgeUpdate\Clients\{guid}")):
        try:
            with winreg.OpenKey(hive, path) as k:
                if str(winreg.QueryValueEx(k, "pv")[0]) not in ("", "0.0.0.0"):
                    return True
        except OSError:
            continue
    return False


if __name__ == "__main__":
    if not _webview2_installed():
        import webbrowser
        _message("ROBOO needs Microsoft Edge WebView2 (a free Microsoft component).\n\n"
                 "The download page will open now - install it, then start ROBOO again.")
        webbrowser.open(WEBVIEW2_URL)
        sys.exit(1)
    try:
        main()
    except Exception:
        # Never show a raw traceback dialog: save it, and say something useful.
        import traceback
        log = config.DATA_DIR / "error.log"
        try:
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            log.write_text(traceback.format_exc(), encoding="utf-8")
        except OSError:
            pass
        _message("ROBOO could not start.\n\n"
                 "Try: right-click the downloaded zip -> Properties -> tick 'Unblock' -> OK, "
                 f"then extract it again.\n\nDetails were saved to:\n{log}")
        sys.exit(1)
