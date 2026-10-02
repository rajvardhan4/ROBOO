"""
Voice in and out, independent of which AI is answering.

Listening: sounddevice mic stream -> energy-based voice activity detection ->
Google's free speech recogniser (via SpeechRecognition). No key needed.
Speaking:  Microsoft Edge neural voices via edge-tts -> MP3 bytes, which the
UI plays through Web Audio so the HUD can react to the actual waveform.
"""
from __future__ import annotations

import asyncio
import base64
import queue
import re
import threading
import time
from typing import Callable

import numpy as np

VOICES = [
    ("en-IN-PrabhatNeural", "Prabhat - English (India), male"),
    ("en-IN-NeerjaNeural", "Neerja - English (India), female"),
    ("hi-IN-MadhurNeural", "Madhur - Hindi, male"),
    ("hi-IN-SwaraNeural", "Swara - Hindi, female"),
    ("en-US-GuyNeural", "Guy - English (US), male"),
    ("en-US-JennyNeural", "Jenny - English (US), female"),
    ("en-GB-RyanNeural", "Ryan - English (UK), male"),
    ("en-GB-SoniaNeural", "Sonia - English (UK), female"),
    ("en-US-AndrewNeural", "Andrew - English (US), male"),
    ("en-US-AvaNeural", "Ava - English (US), female"),
]

LANGUAGES = [("en-IN", "English (India)"), ("hi-IN", "Hindi"), ("en-US", "English (US)"),
             ("en-GB", "English (UK)"), ("ur-PK", "Urdu"), ("bn-IN", "Bengali"),
             ("ta-IN", "Tamil"), ("te-IN", "Telugu"), ("mr-IN", "Marathi")]

RATE = 16000
BLOCK = 1600   # 100 ms


def clean_for_speech(text: str) -> str:
    text = re.sub(r"```.*?```", " code block ", text, flags=re.S)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[*_#`>|\[\]]", "", text)
    text = re.sub(r"[\U0001F000-\U0001FAFF☀-➿]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def split_sentences(text: str, max_parts: int = 4) -> list[str]:
    """Split a reply into speakable sentences; tiny fragments join the next one."""
    parts = [p.strip() for p in re.split(r"(?<=[.!?।])\s+", text.strip()) if p.strip()]
    out: list[str] = []
    for p in parts:
        if out and len(out[-1]) < 30:
            out[-1] = f"{out[-1]} {p}"
        else:
            out.append(p)
    if len(out) > max_parts:
        out = out[:max_parts - 1] + [" ".join(out[max_parts - 1:])]
    return out


def synthesize(text: str, voice: str) -> str | None:
    """Return base64 MP3 of `text`, or None if the TTS service is unreachable."""
    text = clean_for_speech(text)
    if not text:
        return None
    # An English voice returns no audio at all for Devanagari, so Hindi-script
    # replies go to the Hindi voice of the same gender.
    if re.search(r"[ऀ-ॿ]", text) and not voice.startswith("hi-"):
        female = voice in ("en-IN-NeerjaNeural", "en-US-JennyNeural", "en-GB-SoniaNeural",
                           "en-US-AvaNeural")
        voice = "hi-IN-SwaraNeural" if female else "hi-IN-MadhurNeural"
    try:
        import edge_tts

        async def _run() -> bytes:
            out = bytearray()
            async for chunk in edge_tts.Communicate(text, voice).stream():
                if chunk.get("type") == "audio":
                    out += chunk["data"]
            return bytes(out)

        data = asyncio.run(_run())
        return base64.b64encode(data).decode() if data else None
    except Exception as e:
        print(f"[TTS] edge-tts failed: {e}")
        return None


class Listener:
    """Always-open mic that reports its level and hands over finished utterances."""

    def __init__(self, on_level: Callable[[float], None], on_text: Callable[[str], None],
                 on_state: Callable[[str], None]):
        self.on_level, self.on_text, self.on_state = on_level, on_text, on_state
        self.language = "en-IN"
        self.auto = False        # capture every utterance
        self.armed = False       # capture the next utterance only
        self.paused = False      # assistant is speaking - ignore the mic
        self.device_name = ""
        self.ok = False
        self._q: queue.Queue = queue.Queue()
        self._stream = None
        self._armed_at = 0.0

    # -- device ------------------------------------------------------------
    def _candidates(self):
        import sounddevice as sd
        devs, apis = sd.query_devices(), sd.query_hostapis()
        order = {"Windows WASAPI": 0, "MME": 1, "Windows DirectSound": 2}
        default = sd.default.device[0] if sd.default.device else None
        cands = [default] if default is not None and default >= 0 else []
        rest = [i for i, d in enumerate(devs) if d["max_input_channels"] > 0
                and "mapper" not in d["name"].lower() and "stereo mix" not in d["name"].lower()
                and apis[d["hostapi"]]["name"] in order]
        rest.sort(key=lambda i: order[apis[devs[i]["hostapi"]]["name"]])
        return cands + [i for i in rest if i not in cands]

    def start(self) -> bool:
        import sounddevice as sd
        for idx in self._candidates():
            try:
                q: queue.Queue = queue.Queue()
                st = sd.InputStream(samplerate=RATE, channels=1, dtype="int16",
                                    blocksize=BLOCK, device=idx,
                                    callback=lambda data, *_: q.put(bytes(data)))
                st.start()
                t0, got = time.time(), 0
                while time.time() - t0 < 1.5 and got < 3:
                    try:
                        q.get(timeout=0.3)
                        got += 1
                    except queue.Empty:
                        pass
                if got >= 3:
                    self._q, self._stream = q, st
                    self.device_name = sd.query_devices(idx)["name"]
                    self.ok = True
                    threading.Thread(target=self._loop, daemon=True, name="mic").start()
                    print(f"[Voice] mic: {self.device_name}")
                    return True
                st.stop(); st.close()
            except Exception as e:
                print(f"[Voice] mic {idx} failed: {e}")
        print("[Voice] no working microphone found - text input only.")
        return False

    def arm(self) -> None:
        self.armed, self._armed_at = True, time.time()
        self.on_state("LISTENING")

    def disarm(self) -> None:
        self.armed = False

    # -- VAD loop ----------------------------------------------------------
    def _loop(self):
        floor, speaking, silence, frames, pre = 200.0, False, 0, [], []
        last_level = 0.0
        while True:
            try:
                raw = self._q.get(timeout=1.0)
            except queue.Empty:
                continue
            x = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
            rms = float(np.sqrt(np.mean(x * x))) if x.size else 0.0
            level = min(1.0, rms / 4000.0)
            if abs(level - last_level) > 0.01 or level > 0.02:
                self.on_level(0.0 if self.paused else level)
                last_level = level

            capturing = (self.auto or self.armed) and not self.paused
            if self.armed and not speaking and time.time() - self._armed_at > 8:
                self.armed = False                      # nobody spoke - give up
                self.on_state("IDLE")
            if not capturing:
                floor = 0.95 * floor + 0.05 * rms
                speaking, frames, pre = False, [], []
                continue

            if not speaking:
                pre = (pre + [raw])[-4:]
                if rms > max(floor * 2.8, 200):
                    speaking, silence, frames = True, 0, list(pre)
                else:
                    floor = 0.95 * floor + 0.05 * rms
                continue

            frames.append(raw)
            silence = silence + 1 if rms < max(floor * 1.8, 140) else 0
            if silence >= 6 or len(frames) > 200:        # 0.6 s pause or 20 s max
                audio, speaking, frames, pre = b"".join(frames), False, [], []
                if len(audio) > RATE * 2 * 0.4:
                    if not self.auto:
                        self.armed = False
                    threading.Thread(target=self._recognise, args=(audio,), daemon=True).start()

    def _recognise(self, audio: bytes):
        import speech_recognition as sr
        self.on_state("RECOGNISING")
        try:
            text = sr.Recognizer().recognize_google(sr.AudioData(audio, RATE, 2),
                                                    language=self.language)
        except sr.UnknownValueError:
            text = ""
        except Exception as e:
            print(f"[Voice] recognition failed: {e}")
            text = ""
        if text.strip():
            self.on_text(text.strip())
        else:
            self.on_state("LISTENING" if self.auto else "IDLE")
