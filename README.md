---
title: ABUUAISHA Voice Worker
emoji: 🎙️
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# ABUUAISHA DIGITAL STUDIO speech worker

Free voices on a free Hugging Face CPU Space, no account or API key:

- **Microsoft neural voices** (via the `edge-tts` package): Kiswahili Tanzania (Daudi ♂, Rehema ♀), Kiswahili Kenya (Rafiki ♂, Zuri ♀), English Tanzania/Kenya/US/UK, and many Arabic voices. The list is read live from Microsoft at startup, so only voices that really exist are offered. This is the free service behind Microsoft Edge’s “Read aloud”, not an official paid API: it could change, and you should check Microsoft’s terms before selling audio made with it.
- **Open-source Piper voices** (run on the Space’s own CPU, a backup if the Microsoft service is unavailable).

## Deploy (free)
1. huggingface.co → New Space → SDK **Docker** → Blank → Hardware **CPU basic (free)**.
2. Upload the four files in this folder: `app.py`, `requirements.txt`, `Dockerfile`, `README.md`.
3. Space → Settings → **Variables and secrets**:
   - Secret `WORKER_TOKEN` = a long random password (used by the full Next.js app).
   - **To test from the single-file app on Netlify**, also add variables
     `PUBLIC_MODE` = `1` and `ALLOWED_ORIGINS` = `https://your-site.netlify.app` (exact address, no slash at the end).
     Visitors from that site can then generate up to `PUBLIC_DAILY_CHARS` (default 8000) characters a day,
     max `PUBLIC_MAX_CHARS` (default 2000) per request. Set `PUBLIC_MODE` back to `0` for production.
4. Wait for "Running". The first start downloads the voices (a few minutes).
5. In the web app's `.env.local`: `TTS_WORKER_URL=https://<user>-<space>.hf.space` and `TTS_WORKER_TOKEN=<the same password>`.

## Endpoints (all need `Authorization: Bearer <WORKER_TOKEN>`)
- `GET /health` → `{"status":"ready"}` once at least one voice loaded
- `GET /capabilities` → voices that actually loaded
- `POST /synthesize` `{text, voice_id, speed, volume, paragraph_pause, sentence_pause, stability}` → `audio/wav`
  (`stability` 0–1 maps to Piper's noise settings: lower = more expressive, higher = steadier)
  (`[pause 2s]` / `[pumzika 2s]` tags inside the text become real silence, up to 5 s)

## Free-tier facts
- A free Space sleeps after a period without traffic; the first request after that takes a minute or two while it wakes and reloads voices.
- CPU only: roughly a few seconds of processing per paragraph. Fine for normal scripts, slow for whole books.

## Voices and honest quality notes
| Voice | Language | Gender | Note |
|---|---|---|---|
| Lanfrica | Kiswahili | not stated | Only free open Kiswahili Piper voice. Congolese Kiswahili dataset. |
| Kareem | Arabic | male | Best with harakat. |
| Ryan, Joe | English (US) | male | |
| Amy | English (US) | female | |
| Lessac | English (US) | not stated | Sources disagree, so no label. |
| Alan | English (UK) | male | |
| Northern | English (Northern England) | male | |
| Alba | English (Scotland) | female | |
| Jenny | English (UK) | female | |

There is currently **no free, open male Kiswahili voice** in Piper. Voices that fail to download are left out automatically.

Voices are MIT-licensed models from `rhasspy/piper-voices`; check each voice's model card for its dataset licence before commercial use.
