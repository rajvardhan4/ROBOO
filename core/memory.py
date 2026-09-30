"""Long-term memory: a list of facts in data/memory.json."""
from __future__ import annotations

import json
import threading
import time

from core.config import DATA_DIR

_FILE = DATA_DIR / "memory.json"
_lock = threading.Lock()


def _load() -> list[dict]:
    try:
        return json.loads(_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(items: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _FILE.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")


def facts() -> list[dict]:
    with _lock:
        return _load()


def add(fact: str) -> None:
    fact = fact.strip()
    with _lock:
        items = _load()
        if any(i["fact"].lower() == fact.lower() for i in items):
            return
        items.append({"fact": fact, "at": time.strftime("%Y-%m-%d")})
        _save(items)


def remove(text: str) -> int:
    with _lock:
        items = _load()
        keep = [i for i in items if text.lower() not in i["fact"].lower()]
        _save(keep)
        return len(items) - len(keep)


def delete_index(i: int) -> None:
    with _lock:
        items = _load()
        if 0 <= i < len(items):
            items.pop(i)
            _save(items)


def as_prompt() -> str:
    items = facts()
    if not items:
        return "(nothing yet)"
    return "\n".join(f"- {i['fact']}" for i in items[-80:])
