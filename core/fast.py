"""
Speed path.

instant()     - everyday commands ("chrome kholo", "volume 50", "next song",
                "incognito kholo") are recognised locally and run straight away:
                no AI round trip at all. Anything not clearly matched goes to
                the AI as before, so this can only make simple things faster.
quick_reply() - after the AI picks a simple action tool, the spoken "done" is
                written here instead of asking the AI a second time.
"""
from __future__ import annotations

import re

from core import tools

# Tools that only look things up - repeating them changes nothing on the PC.
READ_ONLY = {"web_search", "get_weather", "system_status", "list_files", "read_file",
             "look_at_screen", "wait"}

_FAIL = ("failed", "could not", "not found", "unknown tool", "bad arguments",
         "declined", "no running process", "not available")


def _hinglish(cfg: dict) -> bool:
    return not str(cfg.get("language", "")).startswith(("en-US", "en-GB"))


def _title(s: str) -> str:
    return " ".join(w if w.isupper() else w.capitalize() for w in s.split())


def quick_reply(cfg: dict, name: str, args: dict, out: str) -> str | None:
    if any(w in out.lower()[:120] for w in _FAIL):
        return None                       # let the AI explain what went wrong
    hi = _hinglish(cfg)
    if name == "open_app":
        n = _title(str(args.get("name", "")).strip())
        return f"{n} khol diya." if hi else f"Opened {n}."
    if name == "open_url":
        if args.get("private"):
            return "Incognito window khol di." if hi else "Opened a private window."
        site = re.sub(r"^https?://(www\.)?", "", str(args.get("url", ""))).split("/")[0]
        site = site or "Browser"
        return f"{site} khol diya." if hi else f"Opened {site}."
    if name == "play_youtube":
        q = str(args.get("query", "")).strip()
        return f"YouTube par {q} chala diya." if hi else f"Playing {q} on YouTube."
    if name == "set_volume":
        if args.get("mute") is True:
            return "Mute kar diya." if hi else "Muted."
        if args.get("mute") is False:
            return "Unmute kar diya." if hi else "Unmuted."
        if args.get("level") is not None:
            lv = int(float(args["level"]))
            return f"Volume {lv} percent kar diya." if hi else f"Volume set to {lv} percent."
        return None                       # a volume *reading* needs real phrasing
    if name == "media_control":
        a = args.get("action", "")
        hi_map = {"play_pause": "Ho gaya.", "next": "Agla gaana.", "previous": "Pichla gaana.",
                  "stop": "Band kar diya."}
        en_map = {"play_pause": "Done.", "next": "Next track.", "previous": "Previous track.",
                  "stop": "Stopped."}
        return (hi_map if hi else en_map).get(a)
    if name == "set_brightness":
        lv = int(float(args.get("level", 0)))
        return f"Brightness {lv} percent kar di." if hi else f"Brightness set to {lv} percent."
    if name == "power" and args.get("action") == "lock":
        return "PC lock kar diya." if hi else "Locked."
    if name in ("type_text", "press_keys"):
        return "Ho gaya." if hi else "Done."
    return None


_MULTI = re.compile(r",|\b(aur|and|phir|fir|then|also|uske baad|after that|usme|usmein|"
                    r"uspe|us par|ke baad|bhi|likho|type|search karo|dhundo)\b")


def single_step(text: str) -> bool:
    """True for a request that is one action ("chrome kholo"), so the turn may end
    as soon as that action is done. Anything that looks multi-step returns False."""
    t = re.sub(r"^\[[^\]]*\]\s*", "", text).lower()
    return len(t.split()) <= 9 and not _MULTI.search(t)


def mode_command(text: str) -> str | None:
    """'super fast mode', 'fast mode on', 'medium mode', 'low mode' -> mode name."""
    t = re.sub(r"^\[[^\]]*\]\s*", "", text).lower().strip(" .!?")
    if not re.search(r"\bmode\b", t) or len(t.split()) > 6:
        return None
    if re.search(r"super\s*fast|superfast|turbo", t):
        return "superfast"
    for name in ("medium", "fast", "low"):
        if re.search(rf"\b{name}\b", t):
            return name
    if re.search(r"\b(power|deep|smart)\b", t):
        return "low"
    return None


# ── instant commands ────────────────────────────────────────────────────────

_OPEN_TAIL = (r"(?:ko\s+)?(?:kholo|khol do|khol de|khol|open karo|open kar do|open kr do|"
              r"open kro|open kar|open|chalu karo|chalu kar do|start karo|start kar do)")
_KNOWN = set(tools._APP_ALIASES) | set(tools._WEB_APPS) | set(tools._BROWSERS)


def _clean(text: str, nick: str) -> str:
    t = re.sub(r"^\[[^\]]*\]\s*", "", text).strip().lower().rstrip(".!?। ")
    words = [w for w in (nick.lower(), "please", "plz", "zara", "jaldi", "hey", "ok", "okay")
             if w]
    for _ in range(3):
        for w in words:
            t = re.sub(rf"^{re.escape(w)}[\s,]+|[\s,]+{re.escape(w)}$", "", t).strip()
    return t


def instant(text: str, cfg: dict) -> tuple[str, dict] | None:
    """Return (tool, args) for a clear everyday command, else None."""
    t = _clean(text, cfg.get("nickname", ""))
    if not t or len(t.split()) > 7:
        return None

    if re.search(r"incognito|inprivate|private (window|mode|tab)", t):
        b = next((x for x in ("edge", "firefox", "brave") if x in t), "chrome")
        m = re.search(r"(?:mein|me|in)\s+([a-z0-9.]+)\s+(?:kholo|khol|open)", t)
        site = m.group(1) if m and m.group(1) in tools._WEB_APPS else ""
        return "open_url", {"url": tools._WEB_APPS.get(site, ""), "browser": b, "private": True}

    m = (re.match(r"^(?:open|launch|start)\s+(.+)$", t)
         or re.match(rf"^(.+?)\s+{_OPEN_TAIL}$", t))
    if m:
        target = m.group(1).strip()
        if target in _KNOWN:
            return "open_app", {"name": target}
        return None

    m = re.search(r"\bvolume\s*(?:ko\s*)?(\d{1,3})\b|\b(\d{1,3})\s*(?:%|percent)?\s*volume\b", t)
    if m:
        return "set_volume", {"level": min(100, int(m.group(1) or m.group(2)))}
    if re.fullmatch(r"(un\s?mute|unmute karo|unmute kar do|awaaz chalu karo)", t):
        return "set_volume", {"mute": False}
    if re.fullmatch(r"(mute|mute karo|mute kar do|awaaz band karo|awaz band karo)", t):
        return "set_volume", {"mute": True}

    if re.fullmatch(r"(next|next song|agla gaana|agla gana|agla song|skip|skip karo)", t):
        return "media_control", {"action": "next"}
    if re.fullmatch(r"(previous|previous song|pichla gaana|pichla gana|pichla song)", t):
        return "media_control", {"action": "previous"}
    if re.fullmatch(r"(pause|resume|play|gaana roko|gana roko|song roko|music roko|"
                    r"gaana chalu karo|pause karo|ruko)", t):
        return "media_control", {"action": "play_pause"}

    if re.fullmatch(r"(lock|lock karo|lock kar do|pc lock karo|computer lock karo|"
                    r"screen lock karo|lock the (pc|computer|screen))", t):
        return "power", {"action": "lock"}

    m = (re.match(r"^play\s+(.+)$", t)
         or re.match(r"^(.+?)\s+(?:bajao|play karo|play kro|play kar do)$", t)
         or re.match(r"^(.+?(?:gaana|gaane|gana|gane|song|songs|music))\s+(?:chalao|lagao|sunao)$", t))
    if m:
        return "play_youtube", {"query": m.group(1).strip()}
    return None
