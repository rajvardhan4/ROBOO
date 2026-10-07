"""
Any-AI provider layer.

Three wire protocols cover every provider people actually use:

  anthropic  - Claude, through the official `anthropic` SDK
  gemini     - Google Gemini, native generateContent REST
  openai     - the OpenAI chat-completions shape, which OpenAI, Groq,
               OpenRouter, DeepSeek, Mistral, xAI, Together, Ollama and
               LM Studio all speak

Each Session keeps its own native-format history and runs the tool loop:
send -> run the tools the model asked for -> send the results -> repeat until
the model answers in plain text.
"""
from __future__ import annotations

import json
import time
from typing import Callable

import requests

PRESETS: dict[str, dict] = {
    "anthropic":  {"label": "Anthropic Claude", "kind": "anthropic",
                   "base": "https://api.anthropic.com", "model": "claude-opus-5-5"},
    "gemini":     {"label": "Google Gemini", "kind": "gemini",
                   "base": "https://generativelanguage.googleapis.com/v1beta",
                   "model": "gemini-flash-latest"},
    "openai":     {"label": "OpenAI", "kind": "openai",
                   "base": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    "groq":       {"label": "Groq", "kind": "openai",
                   "base": "https://api.groq.com/openai/v1", "model": "llama-3.3-70b-versatile"},
    "openrouter": {"label": "OpenRouter", "kind": "openai",
                   "base": "https://openrouter.ai/api/v1", "model": "openrouter/auto"},
    "deepseek":   {"label": "DeepSeek", "kind": "openai",
                   "base": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    "mistral":    {"label": "Mistral", "kind": "openai",
                   "base": "https://api.mistral.ai/v1", "model": "mistral-large-latest"},
    "xai":        {"label": "xAI Grok", "kind": "openai",
                   "base": "https://api.x.ai/v1", "model": "grok-3-mini"},
    "together":   {"label": "Together AI", "kind": "openai",
                   "base": "https://api.together.xyz/v1",
                   "model": "meta-llama/Llama-3.3-70B-Instruct-Turbo"},
    "ollama":     {"label": "Ollama (local)", "kind": "openai",
                   "base": "http://localhost:11434/v1", "model": "llama3.2"},
    "lmstudio":   {"label": "LM Studio (local)", "kind": "openai",
                   "base": "http://localhost:1234/v1", "model": "local-model"},
    "custom":     {"label": "Custom (OpenAI-compatible)", "kind": "openai",
                   "base": "", "model": ""},
}

# Claude models that take output_config.effort and the refusal fallback.
_CLAUDE_EFFORT = ("claude-opus-5", "claude-sonnet-5", "claude-fable-5",
                  "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
                  "claude-sonnet-4-6")
_CLAUDE_FALLBACK = ("claude-opus-5-5", "claude-opus-5", "claude-fable-5-1",
                    "claude-sonnet-5-5")


class ProviderError(Exception):
    def __init__(self, msg: str, status: int = 0):
        super().__init__(msg)
        self.status = status

    @property
    def transient(self) -> bool:
        """Worth trying another model: overloaded, rate-limited, retired or down."""
        return self.status in (404, 408, 429, 500, 502, 503, 504, 529) or self.status == -1


def fallback_models(cfg: dict, available: list[str]) -> list[str]:
    """Other models from the same provider to try when the chosen one is down,
    fastest-first. Only 'same family' models, so the assistant keeps its skills."""
    kind = preset(cfg.get("provider", "")).get("kind")
    cur = cfg.get("model", "")
    skip = ("image", "tts", "audio", "live", "embed", "thinking", "vision", "omni",
            "guard", "whisper", "moderation", "realtime", "search", "transcribe")
    cands = [m for m in available if m != cur and not any(s in m.lower() for s in skip)]
    import re

    def ver(m: str) -> float:          # newest first; "-latest" aliases count as new
        if "latest" in m:
            return 99.0
        v = re.search(r"(\d+(?:\.\d+)?)", m)
        return float(v.group(1)) if v else 0.0

    if kind == "gemini":
        cands = [m for m in cands if "flash" in m or "pro" in m]
        rank = lambda m: (0 if "lite" in m else 1 if "flash" in m else 2, "preview" in m, -ver(m))
    elif kind == "anthropic":
        rank = lambda m: (0 if "sonnet" in m else 1 if "haiku" in m else 2, m)
    else:
        return []          # arbitrary OpenAI-compatible catalogues: don't guess
    return sorted(cands, key=rank)[:6]


def detect_provider(key: str) -> str:
    """Best guess from the key's prefix. The user can always override it."""
    k = (key or "").strip()
    if k.startswith("sk-ant-"):
        return "anthropic"
    if k.startswith(("AIza", "AQ.")):
        return "gemini"
    if k.startswith("gsk_"):
        return "groq"
    if k.startswith("sk-or-"):
        return "openrouter"
    if k.startswith("xai-"):
        return "xai"
    if k.startswith("sk-"):
        return "openai"
    return ""


def preset(provider: str) -> dict:
    return PRESETS.get(provider, PRESETS["custom"])


def _base(cfg: dict) -> str:
    return (cfg.get("base_url") or preset(cfg.get("provider", "")).get("base") or "").rstrip("/")


def _http_error(r: requests.Response) -> ProviderError:
    try:
        j = r.json()
        msg = (j.get("error") or {}).get("message") if isinstance(j.get("error"), dict) \
            else j.get("error") or j.get("message") or r.text
    except Exception:
        msg = r.text
    hint = {401: "API key galat hai ya expire ho gayi.",
            403: "Is key ko permission nahi hai.",
            404: "Model ya endpoint nahi mila.",
            429: "Quota / rate limit khatam - thodi der baad try karo."}.get(r.status_code, "")
    return ProviderError(f"HTTP {r.status_code}: {str(msg)[:300]} {hint}".strip(), r.status_code)


def _post_retry(url: str, headers: dict, body: dict) -> requests.Response:
    """POST once more after a dropped connection - Wi-Fi blips are common and a
    second try usually lands. A real outage still surfaces as a network error."""
    for attempt in (0, 1):
        try:
            return requests.post(url, headers=headers, json=body, timeout=120)
        except requests.RequestException as e:
            if attempt:
                raise ProviderError(f"Network error: {e}", -1)
            time.sleep(0.8)


def list_models(cfg: dict) -> list[str]:
    kind = preset(cfg.get("provider", "")).get("kind")
    key, base = cfg.get("api_key", ""), _base(cfg)
    if kind == "anthropic":
        import anthropic
        client = anthropic.Anthropic(api_key=key, timeout=20, max_retries=1)
        return [m.id for m in client.models.list(limit=100)]
    if kind == "gemini":
        r = requests.get(f"{base}/models", headers={"x-goog-api-key": key},
                         params={"pageSize": 200}, timeout=20)
        if not r.ok:
            raise _http_error(r)
        out = []
        for m in r.json().get("models", []):
            if "generateContent" in m.get("supportedGenerationMethods", []):
                out.append(m["name"].split("/", 1)[-1])
        return out
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    r = requests.get(f"{base}/models", headers=headers, timeout=20)
    if not r.ok:
        raise _http_error(r)
    return sorted(m.get("id", "") for m in r.json().get("data", []) if m.get("id"))


# ── sessions ──────────────────────────────────────────────────────────────────

ToolExec = Callable[[str, dict], str]
Emit = Callable[[str, dict], None]
# quick(name, args, result) -> a finished spoken reply, or None. When every tool
# in a step returns one, the turn ends there instead of asking the model to
# phrase "done" - one network round trip instead of two for simple actions.
Quick = Callable[[str, dict, str], "str | None"]


def _quick_reply(calls: list[tuple[str, dict, str]], quick: Quick | None) -> str | None:
    if not quick or not calls:
        return None
    replies = [quick(n, a, out) for n, a, out in calls]
    return " ".join(replies) if all(replies) else None


class Session:
    def __init__(self, cfg: dict, system: str, tools: list[dict]):
        self.cfg, self.system, self.tools = cfg, system, tools
        self.model = cfg.get("model", "")

    def turns(self) -> int:
        raise NotImplementedError

    def run(self, text: str, execute: ToolExec, emit: Emit, max_steps: int = 8,
            quick: Quick | None = None) -> str:
        raise NotImplementedError

    def vision(self, image_b64: str, prompt: str) -> str:
        raise NotImplementedError


class AnthropicSession(Session):
    def __init__(self, cfg, system, tools):
        super().__init__(cfg, system, tools)
        import anthropic
        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=cfg.get("api_key"),
                                          base_url=cfg.get("base_url") or None,
                                          timeout=120, max_retries=2)
        self.messages: list = []
        self._tools = [{"name": t["name"], "description": t["description"],
                        "input_schema": t["parameters"]} for t in tools]

    def turns(self):
        return len(self.messages)

    def _create(self, messages, tools=True):
        kw = dict(model=self.model, max_tokens=4096, system=self.system, messages=messages)
        if tools and self._tools:
            kw["tools"] = self._tools
        if self.model.startswith(_CLAUDE_EFFORT):
            # The speed mode sets the effort; chat-length replies default to low.
            kw["output_config"] = {"effort": self.cfg.get("_effort") or "low"}
        a = self._anthropic
        try:
            if self.model in _CLAUDE_FALLBACK:
                try:
                    return self.client.beta.messages.create(
                        betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kw)
                except TypeError:
                    return self.client.beta.messages.create(
                        betas=["server-side-fallback-2026-07-01"],
                        extra_body={"fallbacks": "default"}, **kw)
            return self.client.messages.create(**kw)
        except a.AuthenticationError:
            raise ProviderError("API key galat hai (401).", 401)
        except a.NotFoundError:
            raise ProviderError(f"Model '{self.model}' nahi mila.", 404)
        except a.RateLimitError:
            raise ProviderError("Rate limit / quota khatam - thodi der baad try karo.", 429)
        except a.APIStatusError as e:
            raise ProviderError(f"Claude API error {e.status_code}: {e.message}", e.status_code)
        except a.APIConnectionError:
            raise ProviderError("Network error - internet check karo.", -1)

    def run(self, text, execute, emit, max_steps=8, quick=None):
        self.messages.append({"role": "user", "content": text})
        for _ in range(max_steps):
            resp = self._create(self.messages)
            # Append the full content (thinking blocks included) - history stays append-only.
            self.messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason == "refusal":
                return "Maaf kijiye, is request mein main madad nahi kar sakta."
            calls = [b for b in resp.content if getattr(b, "type", "") == "tool_use"]
            if not calls:
                return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
            results, done = [], []
            for c in calls:
                out = execute(c.name, dict(c.input or {}))
                results.append({"type": "tool_result", "tool_use_id": c.id, "content": out})
                done.append((c.name, dict(c.input or {}), out))
            self.messages.append({"role": "user", "content": results})
            fast = _quick_reply(done, quick)
            if fast:
                self.messages.append({"role": "assistant", "content": fast})
                return fast
        return "Bahut saare steps ho gaye, yahin rok raha hoon."

    def vision(self, image_b64, prompt):
        resp = self._create([{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": image_b64}},
            {"type": "text", "text": prompt}]}], tools=False)
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


class GeminiSession(Session):
    def __init__(self, cfg, system, tools):
        super().__init__(cfg, system, tools)
        self.url = f"{_base(cfg)}/models/{self.model}:generateContent"
        self.headers = {"x-goog-api-key": cfg.get("api_key", ""),
                        "Content-Type": "application/json"}
        self.contents: list = []
        self.think = cfg.get("_think")          # thinkingLevel from the speed mode, or None
        self._tools = [{"functionDeclarations": [
            {"name": t["name"], "description": t["description"],
             "parameters": t["parameters"]} for t in tools]}]

    def turns(self):
        return len(self.contents)

    def _post(self, body):
        if self.think:
            body = {**body, "generationConfig": {"thinkingConfig": {"thinkingLevel": self.think}}}
        r = _post_retry(self.url, self.headers, body)
        if r.status_code == 400 and self.think and "think" in r.text.lower():
            self.think = None                   # model has no thinking levels - drop it
            body.pop("generationConfig", None)
            return self._post(body)
        if not r.ok:
            raise _http_error(r)
        data = r.json()
        cands = data.get("candidates") or []
        if not cands:
            reason = (data.get("promptFeedback") or {}).get("blockReason", "empty response")
            raise ProviderError(f"Gemini ne jawab nahi diya ({reason}).")
        return cands[0].get("content") or {"role": "model", "parts": []}

    def run(self, text, execute, emit, max_steps=8, quick=None):
        self.contents.append({"role": "user", "parts": [{"text": text}]})
        for _ in range(max_steps):
            body = {"systemInstruction": {"parts": [{"text": self.system}]},
                    "contents": self.contents}
            if self.tools:
                body["tools"] = self._tools
            content = self._post(body)
            content.setdefault("role", "model")
            content.setdefault("parts", [])
            self.contents.append(content)       # kept verbatim (thought signatures)
            calls = [p["functionCall"] for p in content["parts"] if "functionCall" in p]
            if not calls:
                return "".join(p.get("text", "") for p in content["parts"]
                               if not p.get("thought")).strip()
            parts, done = [], []
            for c in calls:
                out = execute(c.get("name", ""), dict(c.get("args") or {}))
                fr = {"name": c.get("name", ""), "response": {"result": out}}
                if c.get("id"):
                    fr["id"] = c["id"]
                parts.append({"functionResponse": fr})
                done.append((c.get("name", ""), dict(c.get("args") or {}), out))
            self.contents.append({"role": "user", "parts": parts})
            fast = _quick_reply(done, quick)
            if fast:
                self.contents.append({"role": "model", "parts": [{"text": fast}]})
                return fast
        return "Bahut saare steps ho gaye, yahin rok raha hoon."

    def vision(self, image_b64, prompt):
        content = self._post({"contents": [{"role": "user", "parts": [
            {"inline_data": {"mime_type": "image/jpeg", "data": image_b64}},
            {"text": prompt}]}]})
        return "".join(p.get("text", "") for p in content.get("parts", []))


class OpenAISession(Session):
    def __init__(self, cfg, system, tools):
        super().__init__(cfg, system, tools)
        self.url = f"{_base(cfg)}/chat/completions"
        self.headers = {"Content-Type": "application/json"}
        if cfg.get("api_key"):
            self.headers["Authorization"] = f"Bearer {cfg['api_key']}"
        self.messages: list = [{"role": "system", "content": system}]
        self._tools = [{"type": "function", "function": {
            "name": t["name"], "description": t["description"],
            "parameters": t["parameters"]}} for t in tools]
        self._tools_ok = True

    def turns(self):
        return len(self.messages) - 1

    def _post(self, messages, tools=True):
        body = {"model": self.model, "messages": messages}
        if tools and self._tools_ok and self._tools:
            body["tools"] = self._tools
        r = _post_retry(self.url, self.headers, body)
        if not r.ok and "tools" in body and r.status_code == 400 and "tool" in r.text.lower():
            # Model without function calling: carry on as a plain chat model.
            self._tools_ok = False
            return self._post(messages, tools=False)
        if not r.ok:
            raise _http_error(r)
        return r.json()["choices"][0]["message"]

    def run(self, text, execute, emit, max_steps=8, quick=None):
        self.messages.append({"role": "user", "content": text})
        for _ in range(max_steps):
            msg = self._post(self.messages)
            keep = {"role": "assistant", "content": msg.get("content") or ""}
            if msg.get("tool_calls"):
                keep["tool_calls"] = msg["tool_calls"]
            self.messages.append(keep)
            calls = msg.get("tool_calls") or []
            if not calls:
                return (msg.get("content") or "").strip()
            done = []
            for c in calls:
                fn = c.get("function", {})
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                args = args if isinstance(args, dict) else {}
                out = execute(fn.get("name", ""), args)
                self.messages.append({"role": "tool", "tool_call_id": c.get("id", ""),
                                      "content": out})
                done.append((fn.get("name", ""), args, out))
            fast = _quick_reply(done, quick)
            if fast:
                self.messages.append({"role": "assistant", "content": fast})
                return fast
        return "Bahut saare steps ho gaye, yahin rok raha hoon."

    def vision(self, image_b64, prompt):
        msg = self._post([{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}}]}],
            tools=False)
        return msg.get("content") or ""


# ── speed modes ───────────────────────────────────────────────────────────────
# Speed vs. depth. Every mode has every skill; slower modes give the AI more
# room to think, which is what multi-step tasks need. Gemini lists are tried in
# order (the first one this key can use wins); other providers keep the user's
# model and change only how hard it thinks.
MODES: dict[str, dict] = {
    "low": {"label": "LOW", "think": "high", "effort": "high", "steps": 12, "quick": False,
            "gemini": ["gemini-3.1-pro-preview", "gemini-pro-latest", "gemini-3.7-flash",
                       "gemini-3.6-flash", "gemini-3-flash-preview"]},
    "medium": {"label": "MEDIUM", "think": None, "effort": "medium", "steps": 10, "quick": False,
               "gemini": ["gemini-3-flash-preview", "gemini-3.6-flash", "gemini-3.5-flash",
                          "gemini-flash-latest"]},
    "fast": {"label": "FAST", "think": "low", "effort": "low", "steps": 8, "quick": True,
             "gemini": ["gemini-3-flash-preview", "gemini-3.5-flash-lite",
                        "gemini-flash-lite-latest"]},
    "superfast": {"label": "SUPER FAST", "think": "minimal", "effort": "low", "steps": 6,
                  "quick": True,
                  "gemini": ["gemini-3.5-flash-lite", "gemini-flash-lite-latest",
                             "gemini-3.1-flash-lite"]},
}
DEFAULT_MODE = "fast"


def mode(cfg: dict) -> dict:
    return MODES.get(cfg.get("mode") or DEFAULT_MODE, MODES[DEFAULT_MODE])


def make_session(cfg: dict, system: str, tools: list[dict]) -> Session:
    kind = preset(cfg.get("provider", "")).get("kind")
    if kind == "anthropic":
        return AnthropicSession(cfg, system, tools)
    if kind == "gemini":
        return GeminiSession(cfg, system, tools)
    return OpenAISession(cfg, system, tools)
