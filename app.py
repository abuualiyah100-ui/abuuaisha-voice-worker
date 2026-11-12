"""
ABUUAISHA DIGITAL STUDIO speech worker (v3).
Two free engines, both returning WAV audio:
  * Microsoft neural voices through the `edge-tts` package (no account or key; needs internet).
    Includes Kiswahili (Tanzania: Daudi, Rehema; Kenya: Rafiki, Zuri), many Arabic and English voices.
    This uses the service behind Microsoft Edge's "Read aloud"; it isn't an official paid API and
    could change. Check Microsoft's terms before commercial use.
  * Open-source Piper voices (runs on this CPU; works even if the Microsoft service is unavailable).
Deploy as a Hugging Face Docker Space (free CPU tier). See README.md.

Two ways to call it:
  * Server mode (production): the Next.js app sends `Authorization: Bearer <WORKER_TOKEN>`.
  * Public test mode (PUBLIC_MODE=1): browsers on ALLOWED_ORIGINS may call it without a token,
    with a per-visitor daily character limit. Meant for testing; turn it off in production.
"""
import array
import asyncio
import io
import subprocess
import logging
import os
import re
import secrets
import threading
import time
import wave
from collections import defaultdict
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from huggingface_hub import hf_hub_download

try:
    from huggingface_hub import file_exists
except ImportError:  # very old huggingface_hub: assume the files exist and let the first use decide
    def file_exists(repo, path):
        return True
from piper import PiperVoice, SynthesisConfig

try:
    import edge_tts
except ImportError:  # the worker still runs with Piper only
    edge_tts = None
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("worker")

TOKEN = os.environ.get("WORKER_TOKEN", "")
MAX_CHARS = int(os.environ.get("MAX_CHARS", "5000"))
PUBLIC_MODE = os.environ.get("PUBLIC_MODE", "0") == "1"
PUBLIC_DAILY_CHARS = int(os.environ.get("PUBLIC_DAILY_CHARS", "8000"))
PUBLIC_MAX_CHARS = int(os.environ.get("PUBLIC_MAX_CHARS", "2000"))
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
REPO = "rhasspy/piper-voices"
# Piper voices: "1" = offer them (checked at startup, downloaded the first time someone uses one),
# "0" = Microsoft voices only (best on very small free servers such as Render's 512 MB / 0.1 CPU).
ENABLE_PIPER = os.environ.get("ENABLE_PIPER", "1") == "1"
PIPER_KEEP = int(os.environ.get("PIPER_KEEP", "2"))   # how many Piper voices stay loaded in memory

# Gender is only filled in where public voice listings agree; otherwise it is None
# and the app shows "Not stated" rather than guessing.
CATALOG = {
    "piper:sw_CD-lanfrica-medium": {"path": "sw/sw_CD/lanfrica/medium/sw_CD-lanfrica-medium",
        "language": "sw", "locale": "sw-CD", "name": "Lanfrica", "accent": "Kiswahili (Congo)", "gender": None},
    "piper:ar_JO-kareem-medium": {"path": "ar/ar_JO/kareem/medium/ar_JO-kareem-medium",
        "language": "ar", "locale": "ar-JO", "name": "Kareem", "accent": "Arabic (Jordan)", "gender": "male"},
    "piper:en_US-ryan-medium": {"path": "en/en_US/ryan/medium/en_US-ryan-medium",
        "language": "en", "locale": "en-US", "name": "Ryan", "accent": "American English", "gender": "male"},
    "piper:en_US-joe-medium": {"path": "en/en_US/joe/medium/en_US-joe-medium",
        "language": "en", "locale": "en-US", "name": "Joe", "accent": "American English", "gender": "male"},
    "piper:en_US-amy-medium": {"path": "en/en_US/amy/medium/en_US-amy-medium",
        "language": "en", "locale": "en-US", "name": "Amy", "accent": "American English", "gender": "female"},
    "piper:en_US-lessac-medium": {"path": "en/en_US/lessac/medium/en_US-lessac-medium",
        "language": "en", "locale": "en-US", "name": "Lessac", "accent": "American English", "gender": None},
    "piper:en_GB-alan-medium": {"path": "en/en_GB/alan/medium/en_GB-alan-medium",
        "language": "en", "locale": "en-GB", "name": "Alan", "accent": "British English", "gender": "male"},
    "piper:en_GB-northern_english_male-medium": {"path": "en/en_GB/northern_english_male/medium/en_GB-northern_english_male-medium",
        "language": "en", "locale": "en-GB", "name": "Northern", "accent": "Northern English", "gender": "male"},
    "piper:en_GB-alba-medium": {"path": "en/en_GB/alba/medium/en_GB-alba-medium",
        "language": "en", "locale": "en-GB", "name": "Alba", "accent": "Scottish English", "gender": "female"},
    "piper:en_GB-jenny_dioco-medium": {"path": "en/en_GB/jenny_dioco/medium/en_GB-jenny_dioco-medium",
        "language": "en", "locale": "en-GB", "name": "Jenny", "accent": "British English", "gender": "female"},
}

voices: dict = {}
load_errors: dict = {}
loading_done = threading.Event()

# ---- Microsoft neural voices (edge-tts) -----------------------------------
EDGE_LOCALES = {
    "sw-TZ": "Kiswahili (Tanzania)", "sw-KE": "Kiswahili (Kenya)",
    "en-TZ": "English (Tanzania)", "en-KE": "English (Kenya)", "en-US": "American English", "en-GB": "British English",
    "ar-SA": "Arabic (Saudi Arabia)", "ar-EG": "Arabic (Egypt)", "ar-AE": "Arabic (UAE)", "ar-JO": "Arabic (Jordan)",
    "ar-OM": "Arabic (Oman)", "ar-MA": "Arabic (Morocco)", "ar-QA": "Arabic (Qatar)", "ar-KW": "Arabic (Kuwait)",
}
edge_voices: dict = {}   # id -> public info (with "short" = Microsoft voice name)


def edge_name(short: str) -> str:
    part = short.split("-", 2)[-1].replace("Neural", "")
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", part).strip()


def load_edge_voices() -> None:
    if edge_tts is None:
        load_errors["edge"] = "edge-tts not installed"
        return
    try:
        listed = asyncio.run(edge_tts.list_voices())
    except Exception as exc:
        load_errors["edge"] = str(exc)[:300]
        log.exception("could not list Microsoft voices")
        return
    for v in listed:
        loc = v.get("Locale", "")
        if loc not in EDGE_LOCALES:
            continue
        short = v["ShortName"]
        gender = str(v.get("Gender", "")).lower()
        edge_voices["edge:" + short] = {
            "name": edge_name(short), "language": loc[:2], "locale": loc, "accent": EDGE_LOCALES[loc],
            "gender": gender if gender in ("male", "female") else None, "engine": "edge", "short": short,
        }
    log.info("Microsoft voices available: %d", len(edge_voices))


piper_available: set = set()      # voices whose files exist upstream
_piper_lock = threading.Lock()


def check_piper_voices() -> None:
    for vid, meta in CATALOG.items():
        try:
            if file_exists(REPO, meta["path"] + ".onnx") and file_exists(REPO, meta["path"] + ".onnx.json"):
                piper_available.add(vid)
            else:
                load_errors[vid] = "model files not found"
        except Exception as exc:
            load_errors[vid] = str(exc)[:300]
            log.exception("could not check %s", vid)


def piper_voice(vid: str):
    """Downloads and loads a Piper voice on first use; keeps only the most recent few in memory."""
    with _piper_lock:
        if vid in voices:
            voices[vid] = voices.pop(vid)          # mark as most recently used
            return voices[vid]
        meta = CATALOG[vid]
        onnx = hf_hub_download(REPO, meta["path"] + ".onnx")
        cfg = hf_hub_download(REPO, meta["path"] + ".onnx.json")
        voices[vid] = PiperVoice.load(onnx, config_path=cfg)
        while len(voices) > PIPER_KEEP:
            voices.pop(next(iter(voices)))         # drop the least recently used
        log.info("loaded %s", vid)
        return voices[vid]


def load_voices() -> None:
    load_edge_voices()
    if ENABLE_PIPER:
        check_piper_voices()
    loading_done.set()


threading.Thread(target=load_voices, daemon=True).start()
app = FastAPI(title="Voice AI Studio worker", docs_url=None, redoc_url=None)
if ALLOWED_ORIGINS:
    app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_methods=["GET", "POST"],
                       allow_headers=["Authorization", "Content-Type"], expose_headers=["X-Duration", "X-Remaining"])

# ---- access control -------------------------------------------------------
_usage: dict = defaultdict(lambda: [0, ""])   # visitor -> [chars used, day]
_usage_lock = threading.Lock()


def has_token(authorization) -> bool:
    given = (authorization or "").removeprefix("Bearer ")
    return bool(TOKEN) and secrets.compare_digest(given, TOKEN)


def visitor(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "unknown")


def require_access(request: Request, authorization) -> bool:
    """Returns True for the trusted server, False for a public visitor; raises otherwise."""
    if has_token(authorization):
        return True
    if PUBLIC_MODE and request.headers.get("origin", "") in ALLOWED_ORIGINS:
        return False
    if not TOKEN and not PUBLIC_MODE:
        raise HTTPException(503, "worker not configured: set WORKER_TOKEN or PUBLIC_MODE")
    raise HTTPException(401, "not allowed")


def charge_public(who: str, chars: int) -> int:
    day = time.strftime("%Y-%m-%d", time.gmtime())
    with _usage_lock:
        used, d = _usage[who]
        if d != day:
            used = 0
        if used + chars > PUBLIC_DAILY_CHARS:
            raise HTTPException(429, f"daily test limit reached ({PUBLIC_DAILY_CHARS} characters)")
        _usage[who] = [used + chars, day]
        return PUBLIC_DAILY_CHARS - used - chars


def refund_public(who: str, chars: int) -> None:
    with _usage_lock:
        _usage[who][0] = max(0, _usage[who][0] - chars)


# ---- endpoints ------------------------------------------------------------
@app.get("/health")
def health(request: Request, authorization=Header(default=None)):
    require_access(request, authorization)
    if not loading_done.is_set():
        return JSONResponse({"status": "loading", "ready_voices": len(voices)}, status_code=503)
    if not piper_available and not edge_voices:
        return JSONResponse({"status": "error"}, status_code=503)
    return {"status": "ready", "ready_voices": len(piper_available) + len(edge_voices)}


@app.get("/capabilities")
def capabilities(request: Request, authorization=Header(default=None)):
    trusted = require_access(request, authorization)
    return {
        "max_chars": MAX_CHARS if trusted else min(MAX_CHARS, PUBLIC_MAX_CHARS),
        "daily_chars": None if trusted else PUBLIC_DAILY_CHARS,
        "formats": ["wav"],
        "supports": ["speed", "volume", "paragraph_pause", "sentence_pause", "pause_tags"],
        "voices": [{"id": vid, **{k: v for k, v in info.items() if k != "short"}} for vid, info in edge_voices.items()]
                  + [{"id": vid, "engine": "piper", **{k: v for k, v in CATALOG[vid].items() if k != "path"}}
                     for vid in CATALOG if vid in piper_available],
        "loading": not loading_done.is_set(),
    }


class SynthesisRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_CHARS)
    voice_id: str
    speed: float = Field(1.0, ge=0.5, le=2.0)
    volume: float = Field(1.0, ge=0.1, le=1.0)
    paragraph_pause: float = Field(0.6, ge=0.0, le=3.0)
    sentence_pause: float = Field(0.0, ge=0.0, le=2.0)
    # 0 = more variable/expressive, 1 = steadier. Mapped to Piper's noise settings; None keeps the voice default.
    stability: Optional[float] = Field(None, ge=0.0, le=1.0)
    pitch: float = Field(1.0, ge=0.5, le=2.0)   # Microsoft voices only


PAUSE_TAG = re.compile(r"\[(?:pause|pumzika)\s*(\d+(?:\.\d+)?)?\s*s?\]", re.I)
SENTENCE = re.compile(r"[^.!?؟۔\n]+[.!?؟۔]*[\"'”’)]*\s*")


def plan(text: str, sentence_pause: float, paragraph_pause: float):
    """Splits text into ('say', str) and ('pause', seconds) steps."""
    text = re.sub(r"[ \t\u00a0]+", " ", text.replace("\r\n", "\n")).strip()
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    steps = []
    for pi, para in enumerate(paragraphs):
        parts = PAUSE_TAG.split(para)
        for idx, part in enumerate(parts):
            if idx % 2 == 1:
                steps.append(("pause", min(5.0, float(part) if part else 1.0)))
                continue
            chunk = re.sub(r"\s+", " ", part).strip()
            if not chunk:
                continue
            if sentence_pause > 0:
                sents = [s.strip() for s in SENTENCE.findall(chunk) if s.strip()] or [chunk]
                for si, s in enumerate(sents):
                    steps.append(("say", s))
                    if si < len(sents) - 1:
                        steps.append(("pause", sentence_pause))
            else:
                steps.append(("say", chunk))
        if pi < len(paragraphs) - 1 and paragraph_pause > 0:
            steps.append(("pause", paragraph_pause))
    return steps


def scale_volume(frames: bytes, sampwidth: int, volume: float) -> bytes:
    if volume >= 0.999 or sampwidth != 2:
        return frames
    a = array.array("h")
    a.frombytes(frames)
    for i in range(len(a)):
        a[i] = int(a[i] * volume)
    return a.tobytes()


def make_config(speed: float, stability):
    """Speed -> length_scale; stability -> noise_scale / noise_w_scale (lower noise = steadier delivery)."""
    if stability is None:
        return SynthesisConfig(length_scale=1.0 / speed)
    try:
        return SynthesisConfig(length_scale=1.0 / speed,
                               noise_scale=round(1.0 - 0.8 * stability, 3),
                               noise_w_scale=round(1.0 - 0.6 * stability, 3))
    except TypeError:  # older piper-tts without these fields: keep speed only
        return SynthesisConfig(length_scale=1.0 / speed)


def mp3_to_wav(mp3: bytes) -> bytes:
    """Converts Microsoft's MP3 to 16-bit mono WAV so pauses, joining and volume work the same for every voice."""
    # Raw PCM out, then our own WAV header: a WAV written to a pipe can't have its length filled in.
    out = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
                          "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1", "-ar", "24000", "pipe:1"],
                         input=mp3, capture_output=True, timeout=60)
    if out.returncode != 0 or len(out.stdout) < 100:
        raise RuntimeError("audio conversion failed")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(out.stdout)
    return buf.getvalue()


async def edge_mp3(text: str, short: str, speed: float, pitch: float) -> bytes:
    rate = max(-50, min(100, round((speed - 1) * 100)))
    hz = max(-50, min(50, round((pitch - 1) * 50)))
    com = edge_tts.Communicate(text, short, rate=f"{rate:+d}%", pitch=f"{hz:+d}Hz")
    data = bytearray()
    async for chunk in com.stream():
        if chunk.get("type") == "audio":
            data += chunk["data"]
    if not data:
        raise RuntimeError("Microsoft returned no audio")
    return bytes(data)


def speak_step(req, text: str) -> bytes:
    """Returns WAV bytes for one piece of text with the requested voice."""
    if req.voice_id in edge_voices:
        return mp3_to_wav(asyncio.run(edge_mp3(text, edge_voices[req.voice_id]["short"], req.speed, req.pitch)))
    voice = piper_voice(req.voice_id)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w_out:
        voice.synthesize_wav(text, w_out, syn_config=make_config(req.speed, req.stability))
    return buf.getvalue()


@app.post("/synthesize")
def synthesize(req: SynthesisRequest, request: Request, authorization=Header(default=None)):
    trusted = require_access(request, authorization)
    if req.voice_id not in piper_available and req.voice_id not in edge_voices:
        raise HTTPException(404, "voice not available")
    if not trusted and len(req.text) > PUBLIC_MAX_CHARS:
        raise HTTPException(413, f"test mode allows up to {PUBLIC_MAX_CHARS} characters per request")

    steps = plan(req.text, req.sentence_pause, req.paragraph_pause)
    spoken = sum(len(s) for kind, s in steps if kind == "say")
    if not spoken:
        raise HTTPException(422, "no speakable text")
    who = visitor(request)
    remaining = None if trusted else charge_public(who, spoken)

    try:
        frames = bytearray()
        params = None
        pending_silence = 0.0
        for kind, value in steps:
            if kind == "pause":
                pending_silence += value
                continue
            with wave.open(io.BytesIO(speak_step(req, value)), "rb") as w_in:
                if params is None:
                    params = w_in.getparams()
                if pending_silence and frames:
                    frames += b"\x00" * (int(params.framerate * pending_silence) * params.sampwidth * params.nchannels)
                pending_silence = 0.0
                frames += w_in.readframes(w_in.getnframes())
        if params is None or not frames:
            raise RuntimeError("no audio produced")

        data = scale_volume(bytes(frames), params.sampwidth, req.volume)
        out = io.BytesIO()
        with wave.open(out, "wb") as w_final:
            w_final.setnchannels(params.nchannels)
            w_final.setsampwidth(params.sampwidth)
            w_final.setframerate(params.framerate)
            w_final.writeframes(data)
    except HTTPException:
        raise
    except Exception:
        if not trusted:
            refund_public(who, spoken)
        log.exception("synthesis failed")
        if req.voice_id in edge_voices:
            raise HTTPException(502, "the Microsoft voice service didn't answer; try again or use another voice")
        raise HTTPException(500, "synthesis failed")

    duration = len(data) / (params.framerate * params.sampwidth * params.nchannels)
    headers = {"X-Duration": f"{duration:.2f}"}
    if remaining is not None:
        headers["X-Remaining"] = str(remaining)
    return Response(out.getvalue(), media_type="audio/wav", headers=headers)
