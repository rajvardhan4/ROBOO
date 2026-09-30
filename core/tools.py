"""
The assistant's skills. Every tool is a plain function plus a JSON-schema
description; the provider layer turns the schema into each API's format.

Actions that cannot be undone (shutdown, restart, sending a message, closing an
app, overwriting a file) go through ctx.confirm(), which shows a dialog in the
HUD and waits for a button YOU press - the model cannot approve its own action.
"""
from __future__ import annotations

import base64
import io
import json
import os
import platform
import subprocess
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Callable

import psutil
import requests

from core import memory

IS_WIN = platform.system() == "Windows"
_NO_WIN = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WIN else {}
HOME = Path.home()


class ToolContext:
    """What tools need from the app. Filled in by the Assistant."""
    emit: Callable[[str, dict], None] = staticmethod(lambda *_: None)
    confirm: Callable[[str, str], bool] = staticmethod(lambda *_: False)
    vision: Callable[[str, str], str] | None = None
    speak: Callable[[str], None] = staticmethod(lambda *_: None)


ctx = ToolContext()
_REGISTRY: dict[str, dict] = {}


def tool(name: str, description: str, properties: dict | None = None,
         required: list[str] | None = None):
    def deco(fn):
        _REGISTRY[name] = {
            "fn": fn,
            "schema": {"name": name, "description": description,
                       "parameters": {"type": "object", "properties": properties or {},
                                      "required": required or []}},
        }
        return fn
    return deco


def schemas() -> list[dict]:
    return [t["schema"] for t in _REGISTRY.values()]


def execute(name: str, args: dict) -> str:
    t = _REGISTRY.get(name)
    if not t:
        return f"Unknown tool: {name}"
    try:
        out = t["fn"](**args)
        return out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
    except TypeError as e:
        return f"Bad arguments for {name}: {e}"
    except Exception as e:  # a failing tool is reported to the model, never fatal
        return f"{name} failed: {e}"


def _s(desc: str) -> dict:
    return {"type": "string", "description": desc}


def _n(desc: str) -> dict:
    return {"type": "number", "description": desc}


def _key(k: str) -> None:
    import pyautogui
    pyautogui.press(k)


# ── apps & web ────────────────────────────────────────────────────────────────

_APP_ALIASES = {
    "chrome": "chrome", "google chrome": "chrome", "edge": "msedge",
    "microsoft edge": "msedge", "firefox": "firefox", "notepad": "notepad",
    "calculator": "calc", "calc": "calc", "paint": "mspaint", "cmd": "cmd",
    "command prompt": "cmd", "terminal": "wt", "powershell": "powershell",
    "explorer": "explorer", "file explorer": "explorer", "files": "explorer",
    "vs code": "code", "vscode": "code", "visual studio code": "code",
    "task manager": "taskmgr", "settings": "ms-settings:", "whatsapp": "whatsapp:",
    "spotify": "spotify:", "camera": "microsoft.windows.camera:",
    "word": "winword", "excel": "excel", "powerpoint": "powerpnt",
    "store": "ms-windows-store:", "mail": "outlookmail:", "calendar": "outlookcal:",
}
_WEB_APPS = {
    "youtube": "https://www.youtube.com", "gmail": "https://mail.google.com",
    "google": "https://www.google.com", "instagram": "https://www.instagram.com",
    "facebook": "https://www.facebook.com", "twitter": "https://x.com", "x": "https://x.com",
    "github": "https://github.com", "chatgpt": "https://chatgpt.com",
    "netflix": "https://www.netflix.com", "amazon": "https://www.amazon.in",
    "maps": "https://maps.google.com", "linkedin": "https://www.linkedin.com",
}


@tool("open_app", "Open an application or well-known website on the computer by name "
      "(e.g. chrome, notepad, vs code, spotify, whatsapp, settings, youtube, gmail).",
      {"name": _s("App or site name")}, ["name"])
def open_app(name: str) -> str:
    n = name.strip().lower()
    if any(w in n for w in ("incognito", "inprivate", "private window", "private mode")):
        b = next((k for k in _BROWSERS if k in n), "chrome")
        return open_url("", b, True)
    if n in _WEB_APPS:
        webbrowser.open(_WEB_APPS[n])
        return f"Opened {name} in the browser."
    target = _APP_ALIASES.get(n, n)
    if not IS_WIN:
        subprocess.Popen(["xdg-open" if platform.system() == "Linux" else "open", "-a", target])
        return f"Opened {name}."
    if target.endswith(":"):
        os.startfile(target)
        return f"Opened {name}."
    r = subprocess.run(["cmd", "/c", "start", "", target], capture_output=True, **_NO_WIN)
    if r.returncode == 0 and not r.stderr:
        return f"Opened {name}."
    # Fall back to the Start menu search, which finds anything installed.
    import pyautogui
    pyautogui.press("win")
    time.sleep(0.6)
    pyautogui.write(name, interval=0.03)
    time.sleep(0.8)
    pyautogui.press("enter")
    return f"Searched the Start menu for {name} and opened the top result."


@tool("close_app", "Close a running application by name (asks the user first).",
      {"name": _s("App/process name, e.g. chrome, notepad, spotify")}, ["name"])
def close_app(name: str) -> str:
    n = _APP_ALIASES.get(name.strip().lower(), name.strip().lower()).replace(".exe", "")
    procs = [p for p in psutil.process_iter(["name"])
             if n in (p.info.get("name") or "").lower()]
    if not procs:
        return f"No running process matches '{name}'."
    names = sorted({p.info["name"] for p in procs})
    if not ctx.confirm("CLOSE APPLICATION", f"Close {', '.join(names)}? Unsaved work may be lost."):
        return "The user declined; nothing was closed."
    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass
    return f"Closed {', '.join(names)}."


_BROWSERS = {"chrome": "chrome", "google chrome": "chrome", "edge": "msedge",
             "microsoft edge": "msedge", "msedge": "msedge", "firefox": "firefox",
             "brave": "brave"}
_PRIVATE_FLAG = {"chrome": "--incognito", "msedge": "--inprivate",
                 "firefox": "-private-window", "brave": "--incognito"}


@tool("open_url", "Open a website, or a new browser window, in a chosen browser. Set private=true "
      "for an incognito / InPrivate / private window (works with no URL too).",
      {"url": _s("URL or site; leave empty for a blank window"),
       "browser": {"type": "string", "enum": ["default", "chrome", "edge", "firefox", "brave"],
                   "description": "Which browser (default = system default; chrome if private)"},
       "private": {"type": "boolean", "description": "Open in incognito / private mode"}})
def open_url(url: str = "", browser: str = "default", private: bool = False) -> str:
    url = (url or "").strip()
    if url and not url.startswith(("http://", "https://", "about:", "chrome:", "edge:")):
        url = "https://" + url
    b = _BROWSERS.get((browser or "").strip().lower())
    if private and not b:
        b = "chrome"
    if not b:
        webbrowser.open(url or "https://www.google.com")
        return f"Opened {url or 'the browser'}."
    args = [b] + ([_PRIVATE_FLAG[b]] if private else []) + ([url] if url else [])
    r = subprocess.run(["cmd", "/c", "start", ""] + args, capture_output=True, **_NO_WIN)
    if r.returncode != 0 or r.stderr:
        return f"Could not start {b}: {r.stderr.decode(errors='ignore')[:120]}"
    mode = {"chrome": "incognito", "brave": "incognito", "msedge": "InPrivate",
            "firefox": "private"}[b] + " " if private else ""
    return f"Opened {'an' if mode[:1] in 'aeiouAEIOU' and mode else 'a'} {mode}{b} window" + (f" at {url}." if url else ".")


@tool("web_search", "Search the internet for current information, news, facts or prices. "
      "Returns the top results with snippets.", {"query": _s("Search query")}, ["query"])
def web_search(query: str) -> str:
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS  # type: ignore
    results = list(DDGS().text(query, max_results=6))
    items = [{"title": r.get("title", ""), "url": r.get("href", ""),
              "snippet": r.get("body", "")} for r in results]
    ctx.emit("intel", {"kind": "search", "title": query, "items": items})
    if not items:
        return "No results."
    return "\n".join(f"{i+1}. {x['title']} - {x['snippet']} ({x['url']})"
                     for i, x in enumerate(items))


@tool("play_youtube", "Find and play a video or song on YouTube.",
      {"query": _s("What to play, e.g. 'lofi hip hop' or a song name")}, ["query"])
def play_youtube(query: str) -> str:
    url = None
    try:
        import yt_dlp
        with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True,
                               "extract_flat": True, "noplaylist": True}) as ydl:
            info = ydl.extract_info(f"ytsearch1:{query}", download=False)
        entry = (info.get("entries") or [None])[0]
        if entry:
            url = f"https://www.youtube.com/watch?v={entry['id']}"
            ctx.emit("intel", {"kind": "video", "title": entry.get("title", query),
                               "url": url, "id": entry["id"]})
    except Exception:
        pass
    webbrowser.open(url or "https://www.youtube.com/results?search_query="
                    + urllib.parse.quote(query))
    return f"Playing '{query}' on YouTube." if url else f"Opened YouTube search for '{query}'."


@tool("get_weather", "Current weather and short forecast for a city.",
      {"city": _s("City name")}, ["city"])
def get_weather(city: str) -> str:
    r = requests.get(f"https://wttr.in/{urllib.parse.quote(city)}",
                     params={"format": "j1"}, timeout=15)
    r.raise_for_status()
    d = r.json()
    cur = d["current_condition"][0]
    info = {"city": city, "temp_c": cur["temp_C"], "feels_c": cur["FeelsLikeC"],
            "desc": cur["weatherDesc"][0]["value"], "humidity": cur["humidity"],
            "wind_kmph": cur["windspeedKmph"],
            "forecast": [{"date": w["date"], "max": w["maxtempC"], "min": w["mintempC"]}
                         for w in d.get("weather", [])[:3]]}
    ctx.emit("intel", {"kind": "weather", "title": city, "data": info})
    return json.dumps(info)


# ── system ────────────────────────────────────────────────────────────────────

@tool("system_status", "CPU, RAM, disk, battery and uptime of this computer.")
def system_status() -> str:
    bat = psutil.sensors_battery()
    disk = psutil.disk_usage(str(HOME.anchor or "/"))
    return json.dumps({
        "cpu_percent": psutil.cpu_percent(interval=0.3),
        "ram_percent": psutil.virtual_memory().percent,
        "disk_percent": disk.percent, "disk_free_gb": round(disk.free / 1e9, 1),
        "battery_percent": bat.percent if bat else None,
        "charging": bat.power_plugged if bat else None,
        "uptime_hours": round((time.time() - psutil.boot_time()) / 3600, 1),
    })


def _endpoint():
    from pycaw.pycaw import AudioUtilities
    dev = AudioUtilities.GetSpeakers()
    if hasattr(dev, "EndpointVolume"):          # pycaw >= 2024
        return dev.EndpointVolume
    from ctypes import POINTER, cast
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import IAudioEndpointVolume
    iface = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(iface, POINTER(IAudioEndpointVolume))


@tool("set_volume", "Set the system volume, or mute/unmute.",
      {"level": _n("Volume 0-100 (omit when muting)"),
       "mute": {"type": "boolean", "description": "true to mute, false to unmute"}})
def set_volume(level: float | None = None, mute: bool | None = None) -> str:
    try:
        ep = _endpoint()
        if mute is not None:
            ep.SetMute(1 if mute else 0, None)
            return "Muted." if mute else "Unmuted."
        if level is not None:
            ep.SetMasterVolumeLevelScalar(max(0.0, min(100.0, float(level))) / 100.0, None)
            return f"Volume set to {int(level)}%."
        return f"Volume is {round(ep.GetMasterVolumeLevelScalar() * 100)}%."
    except Exception:
        import pyautogui
        if mute is not None:
            pyautogui.press("volumemute")
            return "Toggled mute."
        if level is not None:
            pyautogui.press("volumedown", presses=50)
            pyautogui.press("volumeup", presses=int(float(level) / 2))
            return f"Volume set to about {int(level)}%."
        return "Could not read the volume."


@tool("media_control", "Control media playback: play_pause, next, previous, stop.",
      {"action": {"type": "string", "enum": ["play_pause", "next", "previous", "stop"]}},
      ["action"])
def media_control(action: str) -> str:
    _key({"play_pause": "playpause", "next": "nexttrack",
          "previous": "prevtrack", "stop": "stop"}[action])
    return f"Media: {action}."


@tool("set_brightness", "Set screen brightness 0-100 (laptop screens).",
      {"level": _n("Brightness 0-100")}, ["level"])
def set_brightness(level: float) -> str:
    lv = int(max(0, min(100, level)))
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    f"(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods)"
                    f".WmiSetBrightness(1,{lv})"], capture_output=True, **_NO_WIN)
    return f"Brightness set to {lv}%."


@tool("power", "Shutdown, restart, sleep or lock the computer. Always asks the user first "
      "except for lock.",
      {"action": {"type": "string", "enum": ["shutdown", "restart", "sleep", "lock"]}},
      ["action"])
def power(action: str) -> str:
    if action == "lock":
        subprocess.run(["rundll32.exe", "user32.dll,LockWorkStation"], **_NO_WIN)
        return "Locked."
    if not ctx.confirm(f"{action.upper()} COMPUTER", f"Do you really want to {action} now?"):
        return "The user declined."
    cmds = {"shutdown": ["shutdown", "/s", "/t", "5"], "restart": ["shutdown", "/r", "/t", "5"],
            "sleep": ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"]}
    subprocess.Popen(cmds[action], **_NO_WIN)
    return f"{action.capitalize()} in 5 seconds."


# ── keyboard, screen ──────────────────────────────────────────────────────────

@tool("type_text", "Type text into whatever window currently has focus.",
      {"text": _s("Text to type")}, ["text"])
def type_text(text: str) -> str:
    import pyautogui
    import pyperclip
    pyperclip.copy(text)                      # clipboard paste handles every language
    pyautogui.hotkey("ctrl", "v")
    return "Typed."


@tool("press_keys", "Press a key or shortcut, e.g. 'enter', 'ctrl+c', 'alt+tab', 'win+d'.",
      {"keys": _s("Key or combination joined with +")}, ["keys"])
def press_keys(keys: str) -> str:
    import pyautogui
    parts = [k.strip().lower() for k in keys.split("+") if k.strip()]
    pyautogui.hotkey(*parts) if len(parts) > 1 else pyautogui.press(parts[0])
    return f"Pressed {keys}."


def _screenshot_b64(max_w: int = 1400) -> str:
    import mss
    from PIL import Image
    with mss.mss() as sct:
        shot = sct.grab(sct.monitors[1])
        img = Image.frombytes("RGB", shot.size, shot.rgb)
    if img.width > max_w:
        img = img.resize((max_w, int(img.height * max_w / img.width)))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=70)
    return base64.b64encode(buf.getvalue()).decode()


@tool("look_at_screen", "Take a screenshot and answer a question about what is on screen.",
      {"question": _s("What to look for or explain")}, ["question"])
def look_at_screen(question: str) -> str:
    if not ctx.vision:
        return "Vision is not available."
    img = _screenshot_b64()
    ctx.emit("intel", {"kind": "image", "title": "SCREEN CAPTURE", "b64": img})
    return ctx.vision(img, question)


# ── files ─────────────────────────────────────────────────────────────────────

def _path(p: str) -> Path:
    p = os.path.expandvars(os.path.expanduser(p.strip().strip('"')))
    for name in ("desktop", "documents", "downloads", "pictures", "music", "videos"):
        if p.lower() == name or p.lower().startswith(name + "/") or p.lower().startswith(name + "\\"):
            p = str(HOME / name.capitalize()) + p[len(name):]
    path = Path(p)
    return path if path.is_absolute() else HOME / path


@tool("list_files", "List files in a folder (desktop, downloads, documents or any path).",
      {"folder": _s("Folder path or name like 'downloads'")}, ["folder"])
def list_files(folder: str) -> str:
    d = _path(folder)
    if not d.is_dir():
        return f"Folder not found: {d}"
    items = sorted(d.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True)[:60]
    return "\n".join(("[DIR] " if i.is_dir() else "") + i.name for i in items) or "(empty)"


@tool("read_file", "Read a text file's contents (first 20k characters).",
      {"path": _s("File path")}, ["path"])
def read_file(path: str) -> str:
    f = _path(path)
    if not f.is_file():
        return f"File not found: {f}"
    return f.read_text(encoding="utf-8", errors="replace")[:20000]


@tool("write_file", "Create a text file (asks before overwriting an existing one).",
      {"path": _s("File path, e.g. desktop/notes.txt"), "content": _s("Text content")},
      ["path", "content"])
def write_file(path: str, content: str) -> str:
    f = _path(path)
    if f.exists() and not ctx.confirm("OVERWRITE FILE", f"{f} already exists. Replace it?"):
        return "The user declined; file left unchanged."
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content, encoding="utf-8")
    return f"Saved {f}."


@tool("open_path", "Open a file or folder with its default program.",
      {"path": _s("File or folder path")}, ["path"])
def open_path(path: str) -> str:
    f = _path(path)
    if not f.exists():
        return f"Not found: {f}"
    os.startfile(str(f)) if IS_WIN else subprocess.Popen(["xdg-open", str(f)])
    return f"Opened {f}."


# ── messaging ─────────────────────────────────────────────────────────────────

@tool("send_whatsapp", "Send a WhatsApp message to a contact via WhatsApp Desktop. "
      "Always shows the user a confirmation first.",
      {"contact": _s("Contact name as saved in WhatsApp"), "message": _s("Message text")},
      ["contact", "message"])
def send_whatsapp(contact: str, message: str) -> str:
    if not ctx.confirm("SEND WHATSAPP", f"To: {contact}\n\n{message}"):
        return "The user declined; nothing was sent."
    import pyautogui
    import pyperclip
    os.startfile("whatsapp:")
    time.sleep(3.0)
    pyautogui.hotkey("ctrl", "f")
    time.sleep(0.6)
    pyperclip.copy(contact)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(1.5)
    pyautogui.press("down")
    pyautogui.press("enter")
    time.sleep(1.0)
    pyperclip.copy(message)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.3)
    pyautogui.press("enter")
    return f"Sent to {contact} (typed into WhatsApp Desktop - worth a glance to confirm)."


# ── time, reminders, memory ───────────────────────────────────────────────────

@tool("set_reminder", "Remind the user about something after N minutes.",
      {"minutes": _n("Minutes from now"), "message": _s("What to remind")},
      ["minutes", "message"])
def set_reminder(minutes: float, message: str) -> str:
    def fire():
        ctx.emit("reminder", {"message": message})
        ctx.speak(f"Reminder: {message}")
    t = threading.Timer(max(0.05, float(minutes)) * 60, fire)
    t.daemon = True
    t.start()
    ctx.emit("task", {"name": "reminder", "status": "scheduled",
                      "detail": f"{message} - in {minutes:g} min"})
    return f"Reminder set for {minutes:g} minutes from now."


@tool("remember", "Save a fact about the user to long-term memory (name, preferences, "
      "projects, people).", {"fact": _s("The fact to remember")}, ["fact"])
def remember(fact: str) -> str:
    memory.add(fact)
    ctx.emit("memory", {"facts": memory.facts()})
    return "Saved to memory."


@tool("forget", "Remove facts from memory that contain the given text.",
      {"text": _s("Text to match")}, ["text"])
def forget(text: str) -> str:
    n = memory.remove(text)
    ctx.emit("memory", {"facts": memory.facts()})
    return f"Removed {n} fact(s)."
