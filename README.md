# NEXUS — your own holographic AI assistant

A JARVIS-style desktop assistant for Windows with a fully animated sci-fi HUD. Name it anything you
like, plug in **any** AI provider's API key, and talk to it or type to it. It answers out loud and
can control your computer.

## Run

```bash
python -m pip install -r requirements.txt
python main.py
```

You can also double-click `run.bat`. On first launch the setup screen asks for:

| Field | What it does |
|---|---|
| Assistant nickname | Your assistant's name: shown in the HUD, used in its personality, spoken in greetings |
| Your name | What it calls you |
| API key | Paste any key. The provider is detected from the key's prefix |
| Provider / Model | Press **SCAN** to list the models your key can use |
| Voice / Language | Voice for replies, and the language your speech is recognised in |

Change any of these later from **⚙ SETTINGS**, which also has 6 colour themes, "always listening",
and "speak replies".

## Supported AI providers

| Provider | Key looks like | Protocol |
|---|---|---|
| Anthropic Claude | `sk-ant-...` | official `anthropic` SDK |
| Google Gemini | `AIza...` / `AQ....` | native Gemini REST |
| OpenAI | `sk-...` | OpenAI chat completions |
| Groq | `gsk_...` | OpenAI-compatible |
| OpenRouter | `sk-or-...` | OpenAI-compatible (hundreds of models) |
| DeepSeek, Mistral, xAI, Together | — | OpenAI-compatible |
| Ollama / LM Studio | no key | local models, OpenAI-compatible |
| Custom | any | any OpenAI-compatible endpoint URL |

## Controls

- **Ctrl+Space** or the mic button: talk (one sentence)
- **Enter**: send a typed command
- **Esc**: stop it speaking
- Settings → **Always listening** keeps the mic open between turns

## Skills (22)

Open or close apps, open websites, web search, play YouTube, weather, system status, volume,
brightness, media keys, lock/sleep/shutdown/restart, type text, keyboard shortcuts, look at the
screen (vision), list/read/write/open files, send WhatsApp, reminders, and long-term memory.

Anything irreversible (shutdown, restart, closing apps, overwriting files, sending a WhatsApp
message) shows a red **AUTHORIZE** dialog. It only happens when **you** click it; the AI cannot
approve its own action.

## How it is built

```
main.py              pywebview window + JS bridge (Api) + Assistant (turn loop)
core/providers.py    any-AI layer: Anthropic / Gemini / OpenAI-compatible sessions + tool loop
core/tools.py        the 22 skills, each a function + JSON schema
core/voice.py        mic -> VAD -> speech recognition;  text -> edge-tts neural voice
core/memory.py       long-term facts (data/memory.json)
core/config.py       settings (config/settings.json)
web/index.html       HUD layout
web/style.css        themes, chamfered holo panels, glow, motion
web/hud.js           canvas renderers: reactor, particle sphere, globe, radar, gauges, waveform
web/app.js           boot sequence, chat, voice playback, setup/settings, bridge to Python
```

**Flow:** you speak → the mic stream is cut into a sentence by energy-based voice detection →
Google's free recogniser turns it into text → the chosen AI gets it along with the tool list →
when the AI calls a tool, Python runs it and sends back the result → the final reply is shown in
the comms log and turned into an MP3 by edge-tts → the page plays it through Web Audio, and the
analyser's frequency data drives the reactor, spectrum ring and waveform in real time.

Your API key stays on your machine in `config/settings.json` (git-ignored).
