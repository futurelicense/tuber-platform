#!/usr/bin/env python3
"""
Clipper — a tiny local web app to cut a segment out of a YouTube video.

Paste a URL, set in/out timecodes, hit Cut. It uses yt-dlp to grab only the
requested section (not the whole video) and ffmpeg to make the cut frame-accurate.

Run:
    pip install flask yt-dlp
    # ffmpeg must be installed and on PATH  (brew install ffmpeg / apt install ffmpeg / choco install ffmpeg)
    python app.py
    # then open http://127.0.0.1:5000
"""

import os, sys

# Ensure user site-packages are on the path even when running as root
_user_site = os.path.expanduser("~emc2/.local/lib/python{}.{}/site-packages".format(*sys.version_info[:2]))
if os.path.isdir(_user_site) and _user_site not in sys.path:
    sys.path.insert(0, _user_site)
import re
import json
import glob
import queue
import shutil
import platform
import threading
import subprocess
import traceback
import uuid
import math
import time
import urllib.request as _urllib_req
import urllib.parse

from flask import Flask, request, Response, send_file, jsonify

# Load .env from the app directory (HF_API_KEY etc.)
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _ef:
        for _line in _ef:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip())

import yt_dlp

# Google Drive / YouTube (optional — install: pip install google-api-python-client google-auth-oauthlib)
try:
    from googleapiclient.discovery import build as _gbuild
    from googleapiclient.http import MediaFileUpload as _GMedia
    from google_auth_oauthlib.flow import Flow as _GFlow
    from google.auth.transport.requests import Request as _GRequest
    from google.oauth2.credentials import Credentials as _GCreds
    _GDRIVE_LIBS = True
    print("  [clipper] Google libs: OK", flush=True)
except Exception as _ge:
    _GDRIVE_LIBS = False
    print(f"  [clipper] Google libs: FAILED — {_ge}", flush=True)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.environ.get("CLIPPER_DOWNLOAD_DIR") or os.path.join(APP_DIR, "clips")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Base URL the platform is reachable at (e.g. https://tuber-platform.onrender.com).
# When mounted under the platform, OAuth redirect URIs must point back through
# the /clip prefix rather than this app's own bare localhost:5000 default.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:5000").rstrip("/")
_MOUNT_PREFIX = os.environ.get("CLIPPER_MOUNT_PREFIX", "")

# Drop a cookies.txt (Netscape format) exported from your browser into this directory
# to enable the web YouTube client — required for heatmap and avoids bot detection.
COOKIES_FILE = os.path.join(APP_DIR, "cookies.txt")

app = Flask(__name__)

# job_id -> {"q": Queue, "file": path|None, "name": str|None, "error": str|None}
JOBS = {}

# yt-dlp logger that swallows console output (errors still surface as exceptions)
class _YdlLogger:
    def debug(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass

_YDL_LOGGER = _YdlLogger()

# Detect a JS runtime so yt-dlp can generate YouTube PO tokens (prevents bot errors).
# Only deno is enabled in yt-dlp by default; nodejs must be declared explicitly.
# Python API expects {runtime_name: {config_dict}}, e.g. {"nodejs": {"path": "/usr/bin/nodejs"}}
def _find_js_runtime():
    candidates = [
        ("nodejs", ("nodejs", "node")),
        ("deno",   ("deno",)),
    ]
    for rt_name, binaries in candidates:
        for binary in binaries:
            path = shutil.which(binary)
            if path:
                return {rt_name: {"path": path}}
    return None

_js_runtime = _find_js_runtime()

# Cached browser for cookie auth: None=not yet found, str=working browser
_cookie_browser = None

# On Linux, Firefox doesn't need the keyring daemon → try it first
if platform.system() == "Linux":
    _BROWSERS = ("firefox", "chrome", "chromium", "edge", "brave", "opera")
else:
    _BROWSERS = ("chrome", "chromium", "firefox", "edge", "safari", "brave")

# YouTube player clients to try; android/ios/tv bypass bot-detection without cookies
_YT_CLIENTS = (None, "android", "ios", "tv_embedded", "mweb")
_BOT_PHRASES = ("Sign in", "bot", "cookies", "login", "403", "confirm your age")
# FFmpegFD exits 183 when fast-seeking into DASH streams (YouTube uses custom range=
# URL params incompatible with ffmpeg's HTTP byte-range seek). Treat as retriable:
# android/ios clients return single progressive MP4 URLs that ffmpeg can seek normally.
_RETRIABLE_PHRASES = _BOT_PHRASES + ("ffmpeg exited with code 183",)


def _is_retriable_err(exc):
    s = str(exc)
    return any(p in s for p in _RETRIABLE_PHRASES)


def _ydl_extract(url, opts, download=False):
    """extract_info: tries alt YouTube clients then browser cookies on bot-detection."""
    global _cookie_browser

    base = dict(opts, logger=_YDL_LOGGER)
    if _js_runtime:
        base["js_runtimes"] = _js_runtime
    if os.path.exists(COOKIES_FILE):
        base["cookiefile"] = COOKIES_FILE

    def _run(client=None, browser=None):
        o = dict(base)
        if client:
            o["extractor_args"] = {"youtube": {"player_client": [client]}}
        b = browser or (isinstance(_cookie_browser, str) and _cookie_browser)
        if b:
            o["cookiesfrombrowser"] = (b,)
        with yt_dlp.YoutubeDL(o) as ydl:
            return ydl.extract_info(url, download=download)

    # Known working browser → use it directly
    if isinstance(_cookie_browser, str):
        return _run()

    errors = []

    # 1. Try different YouTube player clients (no cookies needed)
    for client in _YT_CLIENTS:
        label = client or "default"
        try:
            result = _run(client=client)
            print(f"  [clipper] OK with client={label}", flush=True)
            return result
        except Exception as e:
            err = str(e)
            print(f"  [clipper] client={label} → {err[:120]}", file=sys.stderr, flush=True)
            errors.append(f"[{label}] {err[:80]}")
            if not _is_retriable_err(e):
                raise  # Not a known-retriable error — propagate immediately

    # 2. Try browser cookies
    for browser in _BROWSERS:
        try:
            result = _run(browser=browser)
            _cookie_browser = browser
            print(f"  [clipper] OK with {browser} cookies", flush=True)
            return result
        except Exception as e:
            err = str(e)
            print(f"  [clipper] browser={browser} → {err[:120]}", file=sys.stderr, flush=True)
            errors.append(f"[{browser}] {err[:80]}")

    summary = " | ".join(errors[-4:])
    js_hint = "" if _js_runtime else " No JS runtime found — run: sudo apt install nodejs"
    raise Exception(
        f"YouTube access blocked after trying {len(errors)} methods.{js_hint} "
        f"Also log into YouTube in Firefox then restart. Details: {summary}"
    )

RATIO_TARGETS = {
    "16:9": (16, 9),
    "9:16": (9, 16),
    "1:1":  (1, 1),
    "4:3":  (4, 3),
}


def _compute_crop_from_dims(w, h, ratio):
    """Return crop=W:H:X:Y string from known pixel dimensions — no ffprobe needed."""
    rw, rh = RATIO_TARGETS[ratio]
    ev = lambda x: int(x // 2) * 2
    if (w / h) > (rw / rh):
        nw, nh = ev(h * rw / rh), ev(h)
    else:
        nw, nh = ev(w), ev(w * rh / rw)
    return f"crop={nw}:{nh}:{(w - nw) // 2}:{(h - nh) // 2}"


def _ffmpeg_download_clip(data, start, end, quality, ratio, out_file, push, srt_path=None):
    """Download, cut, optionally crop, and optionally burn in captions with ffmpeg."""
    duration = end - start
    formats = data.get("requested_formats") or [data]

    cmd = ["ffmpeg", "-y", "-progress", "pipe:1", "-nostats"]

    # Add one -i per stream (video, audio or single progressive)
    for fmt in formats:
        url = fmt.get("url") or data.get("url", "")
        headers = fmt.get("http_headers") or data.get("http_headers") or {}
        if headers:
            cmd += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
        # Fast-seek before -i: ffmpeg uses HTTP range/seek rather than reading from 0
        cmd += ["-ss", str(start), "-t", str(duration), "-i", url]

    if len(formats) > 1:
        cmd += ["-map", "0:v:0", "-map", "1:a:0"]

    if quality == "audio":
        cmd += ["-vn", "-c:a", "libmp3lame", "-q:a", "2", "-f", "mp3"]
    else:
        crop_vf = None
        if ratio in RATIO_TARGETS:
            video_fmt = next((f for f in formats if (f.get("vcodec") or "none") != "none"), formats[0])
            w, h = video_fmt.get("width"), video_fmt.get("height")
            if w and h:
                crop_vf = _compute_crop_from_dims(w, h, ratio)
        sub_vf = f"subtitles='{srt_path}':force_style='FontSize=16,Alignment=2,BorderStyle=3,BackColour=&H80000000,PrimaryColour=&H00FFFFFF'" if srt_path else None
        vf = ",".join(filter(None, [crop_vf, sub_vf]))
        if vf:
            cmd += ["-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "copy"]
        else:
            cmd += ["-c", "copy"]
        cmd += ["-movflags", "+faststart", "-f", "mp4"]

    cmd.append(out_file)
    print(f"  [clipper] ffmpeg: {cmd[0]} ... {' '.join(cmd[-4:])}", flush=True)

    stderr_lines = []
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1)

    def _drain_stderr():
        for ln in proc.stderr:
            stderr_lines.append(ln)

    drain = threading.Thread(target=_drain_stderr, daemon=True)
    drain.start()

    try:
        for line in proc.stdout:
            k, _, v = line.strip().partition("=")
            if k == "out_time_ms" and duration > 0:
                try:
                    pct = min(99.0, int(v) / 1_000_000 / duration * 100)
                    push({"phase": "downloading", "pct": round(pct, 1)})
                except (ValueError, ZeroDivisionError):
                    pass
    finally:
        proc.wait()
        drain.join(timeout=5)

    if proc.returncode != 0:
        err_tail = "".join(stderr_lines)[-500:]
        raise RuntimeError(f"ffmpeg exited {proc.returncode}: {err_tail}")


# ----------------------------- helpers -----------------------------

def parse_time(value):
    """Accept '90', '1:30', '01:30', '00:01:30', '1:02:03.5' -> seconds (float)."""
    if value is None:
        return None
    value = str(value).strip()
    if value == "":
        return None
    parts = value.split(":")
    try:
        parts = [float(p) for p in parts]
    except ValueError:
        raise ValueError(f"Couldn't read the time '{value}'. Use seconds or mm:ss or hh:mm:ss.")
    if len(parts) == 1:
        secs = parts[0]
    elif len(parts) == 2:
        secs = parts[0] * 60 + parts[1]
    elif len(parts) == 3:
        secs = parts[0] * 3600 + parts[1] * 60 + parts[2]
    else:
        raise ValueError(f"Couldn't read the time '{value}'.")
    if secs < 0:
        raise ValueError("Times can't be negative.")
    return secs


def fmt_tc(seconds):
    seconds = int(round(seconds or 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def safe_name(text):
    text = re.sub(r"[^\w\- ]+", "", text or "clip").strip()
    text = re.sub(r"\s+", "_", text)
    return text[:80] or "clip"


def format_for_quality(quality):
    """Map a UI quality choice to a yt-dlp format string.

    Prefer DASH/HTTP formats over HLS (m3u8). HLS requires the ffmpeg
    external downloader which exits with code 183 on download_ranges.
    """
    no_hls = "[protocol!*=m3u8]"
    if quality == "audio":
        return f"ba{no_hls}/ba/b"
    caps = {"best": None, "1080": 1080, "720": 720, "480": 480}
    h = caps.get(quality, None)
    if h is None:
        return f"bv*{no_hls}+ba{no_hls}/bv*+ba/b"
    hq = f"[height<={h}]"
    return f"bv*{no_hls}{hq}+ba{no_hls}/bv*{hq}+ba/b[height<={h}]"


def _fetch_heatmap(url):
    """Fetch heatmap via web client. Only attempted when cookies are available."""
    has_cookies = os.path.exists(COOKIES_FILE) or isinstance(_cookie_browser, str)
    if not has_cookies:
        return []
    try:
        opts = {"quiet": True, "no_warnings": True, "skip_download": True, "logger": _YDL_LOGGER}
        if _js_runtime:
            opts["js_runtimes"] = _js_runtime
        if os.path.exists(COOKIES_FILE):
            opts["cookiefile"] = COOKIES_FILE
        elif isinstance(_cookie_browser, str):
            opts["cookiesfrombrowser"] = (_cookie_browser,)
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = ydl.extract_info(url, download=False)
        if data and data.get("_type") == "playlist":
            data = (data.get("entries") or [{}])[0]
        hm = (data or {}).get("heatmap") or []
        print(f"  [clipper] heatmap (web+cookies) segments: {len(hm)}", flush=True)
        return hm
    except Exception as e:
        print(f"  [clipper] heatmap web fetch failed: {e}", file=sys.stderr, flush=True)
        return []


# ─── Transcript helpers ────────────────────────────────────────────

def _parse_vtt(text):
    """Parse WebVTT into [{start, end, text}] with timestamps in seconds.

    YouTube's auto-generated VTT uses roll-up cues: each cue repeats the previous
    cue's last line, then appends the next line as it's spoken (via inline <c> word
    timing tags). Naively taking each cue's full text therefore duplicates most
    lines 2-3x and, since cue-settings like "align:start position:0%" sit on the
    same physical line as the timestamp arrow, also leaks that literal text in.
    We strip the cue-settings, drop inline tags, and de-dupe consecutive repeats.
    """
    def ts2s(ts):
        ts = ts.strip().replace(",", ".")
        parts = ts.split(":")
        if len(parts) == 3:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        return float(parts[0]) * 60 + float(parts[1])

    cues = []
    last_line = None
    for block in re.split(r"\n\n+", text):
        m = re.search(r"([\d:,.]+)\s*-->\s*([\d:,.]+)", block)
        if not m:
            continue
        s, e = ts2s(m.group(1)), ts2s(m.group(2))

        rest = block[m.end():]
        nl = rest.find("\n")  # drop trailing cue-settings on the timing line
        rest = rest[nl + 1:] if nl != -1 else ""
        rest = re.sub(r"<[^>]+>", "", rest)  # drop inline word-timing tags

        for line in rest.split("\n"):
            line = line.strip()
            if not line or line == last_line:
                continue
            cues.append({"start": round(s, 3), "end": round(e, 3), "text": line})
            last_line = line
    return cues


def _srt_from_cues(cues, shift=0.0):
    """Render cues as SRT, shifting all timestamps by -shift seconds."""
    def fmt(s):
        s = max(0.0, s - shift)
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        ms = int((sec % 1) * 1000)
        return f"{int(h):02d}:{int(m):02d}:{int(sec):02d},{ms:03d}"
    parts = []
    idx = 1
    for c in cues:
        if c["end"] <= shift:
            continue
        parts.append(f"{idx}\n{fmt(c['start'])} --> {fmt(c['end'])}\n{c['text']}\n")
        idx += 1
    return "\n".join(parts)


# url → cues: avoids a second yt-dlp extraction when /suggest is called after /transcript
_transcript_cache: dict = {}


def _fetch_raw_transcript(url):
    """Fetch auto-captions via yt-dlp. Returns [{start, end, text}] or [].

    Results are cached by URL so /transcript and /suggest share one extraction.
    Forces the 'default' web client which always returns automatic_captions;
    android/ios fallback clients omit subtitle data.
    """
    if url in _transcript_cache:
        print(f"  [clipper] transcript: cache hit ({len(_transcript_cache[url])} cues)", flush=True)
        return _transcript_cache[url]

    # Force the default (web) client — android/ios strip automatic_captions
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extractor_args": {"youtube": {"player_client": ["default"]}},
    }
    try:
        data = _ydl_extract(url, opts, download=False)
        if data.get("_type") == "playlist" and data.get("entries"):
            data = data["entries"][0]
        caps = data.get("automatic_captions") or data.get("subtitles") or {}
        for lang in ("en", "en-US", "en-GB"):
            fmts = caps.get(lang, [])
            vtt = next((f for f in fmts if f.get("ext") == "vtt"), None)
            if vtt and vtt.get("url"):
                raw = _urllib_req.urlopen(vtt["url"], timeout=15).read().decode("utf-8", errors="replace")
                cues = _parse_vtt(raw)
                print(f"  [clipper] transcript: {len(cues)} cues ({lang})", flush=True)
                _transcript_cache[url] = cues
                return cues
        print("  [clipper] transcript: no en captions found", file=sys.stderr, flush=True)
    except Exception as exc:
        print(f"  [clipper] transcript fetch failed: {exc}", file=sys.stderr, flush=True)
    return []


# ─── Google Drive helpers ──────────────────────────────────────────

# Combined scopes: Drive upload + YouTube upload — single OAuth flow covers both
_GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]
_GDRIVE_CREDS  = os.path.join(APP_DIR, "credentials.json")
_GOOGLE_TOKEN  = os.path.join(APP_DIR, "google_token.json")
_GDRIVE_FOLDER = "Clipper"
_gdrive_pending_flow = None


def _google_creds():
    """Return valid Google OAuth2 credentials (Drive + YouTube) or None."""
    if not _GDRIVE_LIBS or not os.path.exists(_GDRIVE_CREDS):
        return None
    creds = None
    if os.path.exists(_GOOGLE_TOKEN):
        try:
            creds = _GCreds.from_authorized_user_file(_GOOGLE_TOKEN, _GOOGLE_SCOPES)
        except Exception:
            creds = None
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(_GRequest())
            with open(_GOOGLE_TOKEN, "w") as f:
                f.write(creds.to_json())
        except Exception:
            creds = None
    return creds if (creds and creds.valid) else None


def _gdrive_service():
    creds = _google_creds()
    return _gbuild("drive", "v3", credentials=creds, cache_discovery=False) if creds else None


def _youtube_service():
    creds = _google_creds()
    return _gbuild("youtube", "v3", credentials=creds, cache_discovery=False) if creds else None


def _gdrive_ensure_folder(svc):
    q = (f"name='{_GDRIVE_FOLDER}' and mimeType='application/vnd.google-apps.folder'"
         f" and trashed=false")
    res = svc.files().list(q=q, fields="files(id)", spaces="drive").execute()
    if res["files"]:
        return res["files"][0]["id"]
    f = svc.files().create(
        body={"name": _GDRIVE_FOLDER, "mimeType": "application/vnd.google-apps.folder"},
        fields="id",
    ).execute()
    return f["id"]


def _gdrive_upload_file(filepath, filename):
    svc = _gdrive_service()
    if not svc:
        raise RuntimeError("Google Drive is not authenticated.")
    folder_id = _gdrive_ensure_folder(svc)
    media = _GMedia(filepath, resumable=True)
    meta = {"name": filename, "parents": [folder_id]}
    uploaded = svc.files().create(body=meta, media_body=media, fields="id,webViewLink").execute()
    svc.permissions().create(
        fileId=uploaded["id"],
        body={"type": "anyone", "role": "reader"},
    ).execute()
    return uploaded.get("webViewLink", "")


def _youtube_upload_file(filepath, filename, title=None, privacy="private"):
    svc = _youtube_service()
    if not svc:
        raise RuntimeError("YouTube is not connected. Connect via the Google button.")
    video_title = title or os.path.splitext(filename)[0]
    body = {
        "snippet": {
            "title": video_title,
            "description": "Clipped with Team MoneyTuber Clipper",
            "categoryId": "25",  # News & Politics — sensible default for news/sports/entertainment
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }
    media = _GMedia(filepath, mimetype="video/*", resumable=True)
    insert_req = svc.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        _, response = insert_req.next_chunk()
    video_id = response.get("id", "")
    return f"https://youtu.be/{video_id}" if video_id else ""


# ─── AI clip suggester (provider-agnostic) ────────────────────────
# Reads from .env — supports Groq, OpenRouter, HF, or any OpenAI-compatible endpoint.
#
# Groq (free, fastest):
#   AI_KEY=gsk_...          from console.groq.com
#   AI_MODEL=llama-3.3-70b-versatile
#   AI_ENDPOINT=https://api.groq.com/openai/v1/chat/completions
#
# OpenRouter (free models):
#   AI_KEY=sk-or-...        from openrouter.ai
#   AI_MODEL=meta-llama/llama-3.1-8b-instruct:free
#   AI_ENDPOINT=https://openrouter.ai/api/v1/chat/completions
#
# HF (if you have a PRO key):
#   AI_KEY=hf_...
#   AI_MODEL=meta-llama/Meta-Llama-3.1-8B-Instruct
#   AI_ENDPOINT=https://router.huggingface.co/hf-inference/v1/chat/completions

_AI_ENDPOINT = os.environ.get("AI_ENDPOINT", "https://api.groq.com/openai/v1/chat/completions")
_AI_MODEL    = os.environ.get("AI_MODEL",    "llama-3.3-70b-versatile")

_PLATFORM_CFG = {
    "TikTok": {
        "min_dur": 25, "max_dur": 55,
        "style": "shock-first, emotionally charged, group-chat-worthy",
        "signals": (
            "heated confrontation, unexpected admission, politician caught contradicting themselves, "
            "breaking revelation, crowd eruption, record broken, scandal exposed, "
            "celebrity feud, shocking statistic, dramatic game moment, surprise announcement, "
            "leaked information, denied then confirmed, audience reaction shot"
        ),
        "hook_rule": (
            "clip MUST open mid-action or mid-sentence — never on a presenter intro or 'welcome back'. "
            "The very first words must create instant curiosity or shock."
        ),
    },
    "Shorts": {
        "min_dur": 40, "max_dur": 58,
        "style": "context-drop, narrative reframe, single punchy takeaway",
        "signals": (
            "here is what actually happened, what they are not telling you, "
            "the real reason behind, caught on camera, official contradiction, "
            "breaking development, historic first, surprising statistic, "
            "policy reversal, match-winning moment, underdog triumph, "
            "expert debunked, timeline twist, prediction vs reality"
        ),
        "hook_rule": (
            "clip must open with the most information-dense line — a stat, a quote, or a reveal. "
            "No slow build. Viewer must know WHY they are watching within 3 seconds."
        ),
    },
    "Reels": {
        "min_dur": 25, "max_dur": 60,
        "style": "peak emotion, human arc, share-to-story worthy",
        "signals": (
            "visible emotional reaction — tears, celebration, rage, disbelief, "
            "comeback story, athlete or celebrity at their lowest then highest, "
            "behind-the-scenes human moment, crowd going wild, "
            "locker room confession, post-match raw interview, "
            "political speech crowd reaction, unexpected act of sportsmanship, "
            "viral-worthy confrontation, cultural or historic milestone"
        ),
        "hook_rule": (
            "clip must open on a face, a crowd, or a sound that immediately signals high emotion. "
            "No talking-head cold opens. Lead with the feeling, then the context."
        ),
    },
    "Twitter/X": {
        "min_dur": 30, "max_dur": 140,
        "style": "ratio-worthy hot take, debate-starting, instantly quotable",
        "signals": (
            "unpopular opinion stated confidently, politician or pundit self-own, "
            "expert contradicted by events, strong rebuttal, gotcha moment, "
            "this aged poorly, fiery exchange, bold prediction, crowd booing or cheering"
        ),
        "hook_rule": (
            "clip must open on the sharpest, most controversial line — the one people will quote-tweet. "
            "No preamble."
        ),
    },
    "LinkedIn": {
        "min_dur": 60, "max_dur": 600,
        "style": "professional insight, career or business lesson with stakes",
        "signals": (
            "leadership failure turned lesson, industry secret revealed, "
            "counterintuitive business result, career pivot moment, "
            "data point that changes the narrative, executive candid admission"
        ),
        "hook_rule": (
            "clip should open with the stakes — what was at risk — before delivering the lesson."
        ),
    },
}


def _hf_suggest_clips(cues, platform, n=5):
    # Accept AI_KEY first, fall back to HF_API_KEY for backwards compat
    ai_key = os.environ.get("AI_KEY", "").strip() or os.environ.get("HF_API_KEY", "").strip()
    if not ai_key:
        raise RuntimeError("Set AI_KEY in .env (get a free key at console.groq.com)")

    cfg = _PLATFORM_CFG.get(platform, {
        "min_dur": 25, "max_dur": 90,
        "style": "engaging, emotionally resonant",
        "signals": "surprising reveal, emotional peak, confrontation, breaking moment",
        "hook_rule": "open mid-action, never on a presenter intro",
    })

    # Sample cues evenly if transcript is very long, keeping first/last for context
    if len(cues) > 600:
        step = len(cues) // 500
        sampled = cues[::step][:500]
    else:
        sampled = cues

    transcript = "\n".join(f"[{int(c['start'])}s] {c['text']}" for c in sampled)
    # Hard cap to avoid overflowing context window
    if len(transcript) > 14000:
        transcript = transcript[:14000] + "\n…[truncated]"

    signals  = cfg.get("signals",  "emotional peak, surprising reveal, confrontation, breaking news")
    hook_rule = cfg.get("hook_rule", "open mid-action, never on an intro")

    prompt = f"""You are a senior viral content strategist who specialises in news, entertainment, politics, and sports clips.

Below is the transcript of a video with timestamps in seconds.

TASK: Find the {n} best moments to clip for **{platform}**.

PLATFORM TONE: {cfg["style"]}

VIRAL SIGNALS — scan the transcript specifically for these:
{signals}

HOOK RULE: {hook_rule}

CLIP LENGTH: each clip MUST be between {cfg["min_dur"]} and {cfg["max_dur"]} seconds.

RULES:
- Every clip must span multiple transcript lines — a single cue is never enough
- Start just BEFORE the tension builds so the viewer gets just enough context
- End AFTER the payoff lands — punchline, reaction, crowd response, or conclusion
- Full arc required: setup → peak moment → resolution or reaction
- Rank by virality potential — most shareable clip is clip #1
- NO overlapping clips
- SKIP: video intros, outros, sponsor reads, dead air, off-topic tangents

TRANSCRIPT (each line = one caption cue with its start time):
{transcript}

Return ONLY a valid JSON array, no explanation, no markdown:
[{{"start": <int>, "end": <int>, "title": "<hook title under 8 words — write it like a viral headline>", "reason": "<one sentence on exactly why this moment is shareable>"}}]"""

    body = json.dumps({
        "model": _AI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 2048,
        "temperature": 0.3,
    }).encode()
    req = _urllib_req.Request(
        _AI_ENDPOINT, data=body,
        headers={
            "Authorization": f"Bearer {ai_key}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; Clipper/1.0)",
            "Accept": "application/json",
        },
    )
    try:
        with _urllib_req.urlopen(req, timeout=90) as resp:
            result = json.loads(resp.read().decode())
    except _urllib_req.HTTPError as e:
        body_err = e.read().decode()[:300]
        raise RuntimeError(f"AI API returned {e.code}: {body_err}")

    content = result["choices"][0]["message"]["content"].strip()
    # Strip markdown code fences if present
    content = re.sub(r"```[a-z]*\n?", "", content).strip()
    # Extract the JSON array even if the model wraps it in extra text
    m = re.search(r"\[.*\]", content, re.DOTALL)
    if not m:
        raise ValueError(f"Model returned no JSON array: {content[:300]}")
    raw_json = m.group()
    try:
        clips = json.loads(raw_json)
    except json.JSONDecodeError:
        # Model truncated mid-object — recover complete objects by trimming trailing junk
        # Find last complete object ending with }
        last_brace = raw_json.rfind("}")
        if last_brace == -1:
            raise ValueError(f"No complete JSON objects in response: {raw_json[:300]}")
        trimmed = raw_json[: last_brace + 1].rstrip().rstrip(",") + "]"
        try:
            clips = json.loads(trimmed)
        except json.JSONDecodeError as exc2:
            raise ValueError(f"Could not recover JSON: {exc2} — raw: {raw_json[:300]}")

    # Clamp each clip to platform bounds and filter obvious bad results
    min_dur = cfg.get("min_dur", 20)
    max_dur = cfg["max_dur"]
    out = []
    for c in clips:
        s = max(0, int(c.get("start", 0)))
        e = int(c.get("end", s + min_dur))
        # Enforce minimum — extend end if model was too conservative
        if e - s < min_dur:
            e = s + min_dur
        # Enforce maximum
        if e - s > max_dur:
            e = s + max_dur
        out.append({"start": s, "end": e,
                    "title": c.get("title", ""), "reason": c.get("reason", "")})
    return out


def _ai_chat_completion(prompt, max_tokens):
    """POST a single chat completion to the configured AI provider (Groq/OpenRouter/HF/etc,
    same config as the clip suggester). Shared by script cleanup and chapter generation.
    """
    ai_key = os.environ.get("AI_KEY", "").strip() or os.environ.get("HF_API_KEY", "").strip()
    if not ai_key:
        raise RuntimeError("Set AI_KEY in .env (get a free key at console.groq.com)")

    body = json.dumps({
        "model": _AI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }).encode()
    req = _urllib_req.Request(
        _AI_ENDPOINT, data=body,
        headers={
            "Authorization": f"Bearer {ai_key}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; Clipper/1.0)",
            "Accept": "application/json",
        },
    )
    try:
        with _urllib_req.urlopen(req, timeout=150) as resp:
            result = json.loads(resp.read().decode())
    except _urllib_req.HTTPError as e:
        if e.code == 413:
            raise RuntimeError(
                "This video's transcript is too long for the configured AI model's token/rate "
                "limit. Try a shorter video, or check your AI provider's tier limits in .env."
            )
        raise RuntimeError(f"AI API returned {e.code}: {e.read().decode()[:300]}")

    content = result["choices"][0]["message"]["content"].strip()
    return re.sub(r"```[a-z]*\n?", "", content).strip()


def _ai_clean_script(cues, title=""):
    """Turn raw unpunctuated auto-captions into a readable script via the same AI provider
    used for clip suggestions. Returns {"title", "summary", "script"}.

    Uses a plain-text delimiter format rather than asking the model for JSON — a full
    cleaned transcript is long free-text full of quotes/apostrophes, which is exactly the
    kind of content LLMs frequently mis-escape when asked to wrap it in a JSON string.
    """
    raw = " ".join(c.get("text", "") for c in cues)
    if len(raw) > 14000:
        raw = raw[:14000] + " …[truncated]"

    prompt = f"""You are a professional transcript editor.

Below is a raw, unpunctuated auto-generated caption transcript from a YouTube video{f' titled "{title}"' if title else ""}. It has no punctuation, no paragraph breaks, and may contain filler words ("um", "uh"), false starts, and repeated words from auto-captioning errors.

TASK:
1. Rewrite it as a clean, readable script: add correct punctuation and capitalization, break it into natural paragraphs, and remove filler words/false starts/stutters WITHOUT changing the actual meaning or wording of what was said. Do not summarize, shorten, invent, or embellish — this must stay a faithful transcript, just cleaned up.
2. Write a one-sentence summary of what the video is about.
3. Suggest a short punchy title under 10 words (keep close to the given title if one was provided).

RAW TRANSCRIPT:
{raw}

Respond in EXACTLY this format and nothing else — no markdown, no extra commentary:
TITLE: <title>
SUMMARY: <one sentence>
---
<the cleaned script, paragraphs separated by blank lines>"""

    content = _ai_chat_completion(prompt, max_tokens=4096)

    m = re.match(r"TITLE:\s*(.*?)\nSUMMARY:\s*(.*?)\n-{3,}\n(.*)", content, re.DOTALL)
    if not m:
        # Model didn't follow the format — surface the raw cleaned text rather than failing outright
        return {"title": title, "summary": "", "script": content}
    return {
        "title": m.group(1).strip() or title,
        "summary": m.group(2).strip(),
        "script": m.group(3).strip(),
    }


def _ai_generate_chapters(cues, title="", known_chapters=None):
    """Split the transcript into titled, cleaned chapters via the same AI provider.

    If the video already has uploader-defined chapters (passed in via known_chapters,
    read from yt-dlp's info dict), the model is told to use those exact boundaries
    instead of guessing — cheaper to get right and matches what the uploader intended.
    Uses the same plain-text delimiter trick as _ai_clean_script to dodge JSON-escaping
    failures on long free-text chapter bodies.
    """
    transcript = "\n".join(f"[{int(c.get('start', 0))}s] {c.get('text', '')}" for c in cues)
    if len(transcript) > 14000:
        transcript = transcript[:14000] + "\n…[truncated]"

    if known_chapters:
        chapter_hint = (
            "The uploader already defined these chapters for this video — segment the "
            "transcript using these EXACT start times in seconds and these titles "
            "(only lightly polish wording, don't rename them):\n" +
            "\n".join(f"- {int(c.get('start', 0))}s: {c.get('title', '')}" for c in known_chapters)
        )
    else:
        chapter_hint = (
            "This video has no defined chapters. Split it into natural chapters yourself — "
            "wherever the topic or focus clearly shifts. Don't force a fixed count; a short "
            "video might only need 2-3, a long one might need 8-10. Each chapter's start time "
            "must be one of the actual timestamps shown in the transcript below."
        )

    prompt = f"""You are a professional transcript editor.

Below is a raw, unpunctuated auto-generated caption transcript from a YouTube video{f' titled "{title}"' if title else ""}, with each line's approximate start time in seconds.

{chapter_hint}

For EACH chapter:
- Give it a short, punchy title (under 8 words).
- Rewrite its portion of the transcript as a clean, readable script: correct punctuation and capitalization, natural paragraphs, filler words and false starts removed — WITHOUT changing, shortening, or summarizing the actual content of what was said.

RAW TRANSCRIPT:
{transcript}

Respond with ONLY a sequence of chapter blocks in EXACTLY this format, nothing else — no markdown, no extra commentary:
### CHAPTER
START: <seconds, integer>
TITLE: <title>
TEXT:
<cleaned script for this chapter>
### CHAPTER
START: <seconds, integer>
TITLE: <title>
TEXT:
<cleaned script for this chapter>
(repeat for every chapter, covering the entire transcript start to finish with no gaps)"""

    content = _ai_chat_completion(prompt, max_tokens=5000)

    chapters = []
    for block in re.split(r"###\s*CHAPTER\s*\n", content):
        block = block.strip()
        if not block:
            continue
        m = re.match(r"START:\s*(\d+).*?\nTITLE:\s*(.*?)\s*\nTEXT:\s*\n(.*)", block, re.DOTALL)
        if not m:
            continue
        chapters.append({
            "start": int(m.group(1)),
            "title": m.group(2).strip(),
            "text": m.group(3).strip(),
        })
    if not chapters:
        raise ValueError(f"Model returned no parseable chapters: {content[:300]}")
    chapters.sort(key=lambda c: c["start"])
    return chapters


# ----------------------------- routes -----------------------------

@app.route("/")
def index():
    return Response(PAGE, mimetype="text/html")


@app.route("/info")
def info():
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL given."}), 400
    try:
        opts = {"quiet": True, "no_warnings": True, "skip_download": True}
        data = _ydl_extract(url, opts, download=False)
        if data.get("_type") == "playlist" and data.get("entries"):
            data = data["entries"][0]

        heatmap = data.get("heatmap") or []
        print(f"  [clipper] heatmap segments: {len(heatmap)}", flush=True)

        # android/ios clients don't return heatmap; retry with web client + cookies.
        if not heatmap:
            heatmap = _fetch_heatmap(url)

        chapters = [
            {"start_time": c.get("start_time"), "title": c.get("title", "")}
            for c in (data.get("chapters") or [])
        ]

        return jsonify({
            "title": data.get("title", "Untitled"),
            "duration": data.get("duration"),
            "thumbnail": data.get("thumbnail"),
            "uploader": data.get("uploader"),
            "heatmap": heatmap,
            "chapters": chapters,
        })
    except Exception as e:
        return jsonify({"error": f"Couldn't read that video: {e}"}), 400


@app.route("/clip", methods=["POST"])
def clip():
    body = request.get_json(force=True, silent=True) or {}
    url = (body.get("url") or "").strip()
    quality = body.get("quality", "best")
    ratio = body.get("ratio", "original")

    if not url:
        return jsonify({"error": "Paste a video URL first."}), 400

    try:
        start = parse_time(body.get("start"))
        end = parse_time(body.get("end"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    if start is None or end is None:
        return jsonify({"error": "Set both a start and an end time."}), 400
    if end <= start:
        return jsonify({"error": "End time has to be after the start time."}), 400

    burn_captions = bool(body.get("burn_captions", False))

    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"q": queue.Queue(), "file": None, "name": None, "error": None}
    threading.Thread(
        target=run_clip,
        args=(job_id, url, start, end, quality, ratio, burn_captions),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id})


def run_clip(job_id, url, start, end, quality, ratio="original", burn_captions=False):
    job = JOBS[job_id]
    q = job["q"]

    def push(event):
        q.put(event)

    ext = "mp3" if quality == "audio" else "mp4"
    out_file = os.path.join(DOWNLOAD_DIR, f"{job_id}.{ext}")

    try:
        # Phase 1: resolve stream URLs (no download yet)
        push({"phase": "starting"})
        info_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "format": format_for_quality(quality),
        }
        data = _ydl_extract(url, info_opts, download=False)
        if data.get("_type") == "playlist" and data.get("entries"):
            data = data["entries"][0]

        # Phase 1b (optional): fetch captions for burn-in
        srt_path = None
        if burn_captions and quality != "audio":
            push({"phase": "captions"})
            cues = _fetch_raw_transcript(url)
            if cues:
                seg_cues = [c for c in cues if c["end"] >= start and c["start"] <= end]
                srt_content = _srt_from_cues(seg_cues, shift=start)
                srt_path = os.path.join(DOWNLOAD_DIR, f"{job_id}.srt")
                with open(srt_path, "w", encoding="utf-8") as f:
                    f.write(srt_content)

        # Phase 2: download + cut + crop in one ffmpeg pass with live progress
        push({"phase": "downloading"})
        _ffmpeg_download_clip(data, start, end, quality, ratio, out_file, push, srt_path=srt_path)

        nice = (f"{safe_name(data.get('title'))}"
                f"_{fmt_tc(start).replace(':','-')}"
                f"_{fmt_tc(end).replace(':','-')}.{ext}")
        job["file"] = out_file
        job["name"] = nice
        push({"phase": "done", "name": nice})

    except Exception as e:
        traceback.print_exc()
        msg = str(e)
        if "ffmpeg" in msg.lower() and not shutil.which("ffmpeg"):
            msg = "ffmpeg isn't installed or isn't on PATH. Install it and try again."
        job["error"] = msg
        push({"phase": "error", "message": msg})


@app.route("/download_video", methods=["POST"])
def download_video():
    body = request.get_json(force=True, silent=True) or {}
    url = (body.get("url") or "").strip()
    quality = body.get("quality", "best")
    if not url:
        return jsonify({"error": "Paste a video URL first."}), 400

    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"q": queue.Queue(), "file": None, "name": None, "error": None}
    threading.Thread(
        target=run_download_video,
        args=(job_id, url, quality),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id})


def run_download_video(job_id, url, quality):
    """Download the full video (no cutting) straight through yt-dlp, reusing its own
    fragment-download + merge pipeline rather than our custom ffmpeg clip path.
    """
    job = JOBS[job_id]
    q = job["q"]

    def push(event):
        q.put(event)

    def progress_hook(d):
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes", 0)
            pct = round(downloaded / total * 100, 1) if total else None
            push({"phase": "downloading", "pct": pct, "speed": d.get("speed"), "eta": d.get("eta")})
        elif status == "finished":
            push({"phase": "merging"})

    outtmpl = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")
    try:
        push({"phase": "starting"})
        opts = {
            "quiet": True,
            "no_warnings": True,
            "format": format_for_quality(quality),
            "outtmpl": outtmpl,
            "progress_hooks": [progress_hook],
        }
        if quality == "audio":
            opts["postprocessors"] = [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
            ]
        else:
            opts["merge_output_format"] = "mp4"

        data = _ydl_extract(url, opts, download=True)
        if data.get("_type") == "playlist" and data.get("entries"):
            data = data["entries"][0]

        matches = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*"))
        if not matches:
            raise RuntimeError("Download finished but the output file wasn't found.")
        actual_file = matches[0]
        ext = os.path.splitext(actual_file)[1].lstrip(".")

        nice = f"{safe_name(data.get('title'))}.{ext}"
        job["file"] = actual_file
        job["name"] = nice
        push({"phase": "done", "name": nice})

    except Exception as e:
        traceback.print_exc()
        msg = str(e)
        if "ffmpeg" in msg.lower() and not shutil.which("ffmpeg"):
            msg = "ffmpeg isn't installed or isn't on PATH. Install it and try again."
        job["error"] = msg
        push({"phase": "error", "message": msg})


@app.route("/progress/<job_id>")
def progress(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job."}), 404

    def stream():
        q = job["q"]
        while True:
            event = q.get()
            yield f"data: {json.dumps(event)}\n\n"
            if event.get("phase") in ("done", "error"):
                break

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/download/<job_id>")
def download(job_id):
    job = JOBS.get(job_id)
    if not job or not job.get("file") or not os.path.exists(job["file"]):
        return "Clip not found.", 404
    return send_file(job["file"], as_attachment=True, download_name=job["name"])


@app.route("/stream/<job_id>")
def stream_clip(job_id):
    """Serve the clip for in-browser playback (supports HTTP Range for seeking)."""
    job = JOBS.get(job_id)
    if not job or not job.get("file") or not os.path.exists(job["file"]):
        return "Clip not found.", 404
    return send_file(job["file"], conditional=True)


@app.route("/transcript")
def transcript_route():
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL."}), 400
    cues = _fetch_raw_transcript(url)
    return jsonify({"cues": cues})


@app.route("/script/clean", methods=["POST"])
def script_clean():
    body = request.get_json(force=True, silent=True) or {}
    cues = body.get("cues") or []
    title = (body.get("title") or "").strip()
    if not cues:
        return jsonify({"error": "No transcript to clean."}), 400
    ai_key = os.environ.get("AI_KEY", "").strip() or os.environ.get("HF_API_KEY", "").strip()
    if not ai_key:
        return jsonify({"error": "Set AI_KEY in .env — get a free key at console.groq.com"}), 400
    try:
        return jsonify(_ai_clean_script(cues, title))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/script/chapters", methods=["POST"])
def script_chapters():
    body = request.get_json(force=True, silent=True) or {}
    cues = body.get("cues") or []
    title = (body.get("title") or "").strip()
    known_chapters = body.get("known_chapters") or []
    if not cues:
        return jsonify({"error": "No transcript to chapter."}), 400
    ai_key = os.environ.get("AI_KEY", "").strip() or os.environ.get("HF_API_KEY", "").strip()
    if not ai_key:
        return jsonify({"error": "Set AI_KEY in .env — get a free key at console.groq.com"}), 400
    try:
        return jsonify({"chapters": _ai_generate_chapters(cues, title, known_chapters)})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/suggest")
def suggest():
    url      = request.args.get("url", "").strip()
    platform = request.args.get("platform", "TikTok")
    n        = min(10, max(1, int(request.args.get("n", 5))))
    if not url:
        return jsonify({"error": "No URL."}), 400
    ai_key = os.environ.get("AI_KEY", "").strip() or os.environ.get("HF_API_KEY", "").strip()
    if not ai_key:
        return jsonify({"error": "Set AI_KEY in .env — get a free key at console.groq.com"}), 400
    cues = _fetch_raw_transcript(url)
    if not cues:
        return jsonify({"error": "No transcript found for this video."}), 400
    try:
        clips = _hf_suggest_clips(cues, platform, n)
        return jsonify({"clips": clips, "platform": platform})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/google/status")
def google_status():
    creds   = _google_creds()
    authed  = creds is not None
    channel = ""
    if authed:
        try:
            svc = _gbuild("youtube", "v3", credentials=creds, cache_discovery=False)
            r   = svc.channels().list(part="snippet", mine=True).execute()
            if r.get("items"):
                channel = r["items"][0]["snippet"]["title"]
        except Exception:
            pass
    return jsonify({
        "libs":    _GDRIVE_LIBS,
        "creds":   os.path.exists(_GDRIVE_CREDS),
        "authed":  authed,
        "channel": channel,
    })


@app.route("/google/auth")
def google_auth():
    global _gdrive_pending_flow
    if not _GDRIVE_LIBS:
        return jsonify({"error": "Run: pip install google-api-python-client google-auth-oauthlib"}), 400
    if not os.path.exists(_GDRIVE_CREDS):
        return jsonify({"error": f"Place credentials.json (OAuth2 Desktop client) in {APP_DIR}"}), 400
    flow = _GFlow.from_client_secrets_file(
        _GDRIVE_CREDS, _GOOGLE_SCOPES,
        redirect_uri=f"{PUBLIC_BASE_URL}{_MOUNT_PREFIX}/google/callback",
    )
    auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
    _gdrive_pending_flow = flow
    return jsonify({"auth_url": auth_url})


@app.route("/google/callback")
def google_callback():
    global _gdrive_pending_flow
    code = request.args.get("code", "")
    if not code or not _gdrive_pending_flow:
        return "Auth failed.", 400
    _gdrive_pending_flow.fetch_token(code=code)
    creds = _gdrive_pending_flow.credentials
    with open(_GOOGLE_TOKEN, "w") as f:
        f.write(creds.to_json())
    _gdrive_pending_flow = None
    return "<script>window.close()</script><p>Google connected! Drive &amp; YouTube are ready. Close this tab.</p>"


# Keep old /gdrive/ paths alive so any bookmarks / cached requests still work
@app.route("/gdrive/status")
def gdrive_status():
    return google_status()

@app.route("/gdrive/auth")
def gdrive_auth():
    return google_auth()

@app.route("/gdrive/callback")
def gdrive_callback():
    return google_callback()


@app.route("/gdrive/upload/<job_id>", methods=["POST"])
def gdrive_upload(job_id):
    job = JOBS.get(job_id)
    if not job or not job.get("file") or not os.path.exists(job["file"]):
        return jsonify({"error": "Clip not found."}), 404
    try:
        link = _gdrive_upload_file(job["file"], job.get("name") or "clip.mp4")
        return jsonify({"link": link})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/youtube/upload/<job_id>", methods=["POST"])
def youtube_upload(job_id):
    job = JOBS.get(job_id)
    if not job or not job.get("file") or not os.path.exists(job["file"]):
        return jsonify({"error": "Clip not found."}), 404
    data    = request.get_json(silent=True) or {}
    title   = data.get("title") or job.get("name") or "clip"
    privacy = data.get("privacy", "private")
    try:
        url = _youtube_upload_file(job["file"], job.get("name") or "clip.mp4",
                                   title=title, privacy=privacy)
        return jsonify({"url": url})
    except Exception as e:
        traceback.print_exc()
        msg = str(e)
        # Surface actionable guidance for common API errors
        if "accessNotConfigured" in msg or "has not been used" in msg:
            msg = ("YouTube Data API v3 is not enabled. "
                   "Go to Google Cloud Console → APIs & Services → Enable APIs → "
                   "search 'YouTube Data API v3' → Enable.")
        elif "quotaExceeded" in msg:
            msg = "YouTube upload quota exceeded for today. Try again tomorrow."
        elif "forbidden" in msg.lower() or "403" in msg:
            msg = "YouTube upload forbidden. Re-connect your Google account and try again."
        return jsonify({"error": msg}), 500


# ─── TikTok Content Posting API ──────────────────────────────────────
_TIKTOK_KEY    = os.environ.get("TIKTOK_CLIENT_KEY", "")
_TIKTOK_SECRET = os.environ.get("TIKTOK_CLIENT_SECRET", "")
_TIKTOK_TOKEN  = os.path.join(APP_DIR, "tiktok_token.json")
_TIKTOK_REDIRECT = f"{PUBLIC_BASE_URL}{_MOUNT_PREFIX}/tiktok/callback"
_TIKTOK_SCOPE    = "user.info.basic,video.publish"
_tiktok_pending_state = None


def _tiktok_load_token():
    if not os.path.exists(_TIKTOK_TOKEN):
        return None
    try:
        with open(_TIKTOK_TOKEN) as f:
            tok = json.load(f)
        if time.time() < tok.get("expires_at", 0) - 60:
            return tok
        # Try refresh
        if time.time() < tok.get("refresh_expires_at", 0) - 60:
            return _tiktok_refresh(tok["refresh_token"])
    except Exception:
        pass
    return None


def _tiktok_refresh(refresh_token):
    body = urllib.parse.urlencode({
        "client_key": _TIKTOK_KEY,
        "client_secret": _TIKTOK_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }).encode()
    req = _urllib_req.Request(
        "https://open.tiktokapis.com/v2/oauth/token/",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with _urllib_req.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    tok = {
        "access_token":       data["access_token"],
        "refresh_token":      data.get("refresh_token", refresh_token),
        "open_id":            data.get("open_id", ""),
        "expires_at":         time.time() + int(data.get("expires_in", 86400)),
        "refresh_expires_at": time.time() + int(data.get("refresh_expires_in", 31536000)),
    }
    with open(_TIKTOK_TOKEN, "w") as f:
        json.dump(tok, f)
    return tok


def _tiktok_upload_file(filepath, title):
    tok = _tiktok_load_token()
    if not tok:
        raise RuntimeError("TikTok is not connected.")
    access_token = tok["access_token"]
    headers_auth = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=UTF-8",
    }
    file_size = os.path.getsize(filepath)
    chunk_size = 5 * 1024 * 1024  # 5 MB chunks
    total_chunks = max(1, math.ceil(file_size / chunk_size))

    # Step 1 — initialise upload
    init_body = json.dumps({
        "post_info": {
            "title": title[:150],
            "privacy_level": "SELF_ONLY",   # lands in inbox/drafts — safest default
            "disable_duet": False,
            "disable_comment": False,
            "disable_stitch": False,
        },
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": file_size,
            "chunk_size": chunk_size,
            "total_chunk_count": total_chunks,
        },
    }).encode()
    req = _urllib_req.Request(
        "https://open.tiktokapis.com/v2/post/publish/video/init/",
        data=init_body, headers=headers_auth,
    )
    with _urllib_req.urlopen(req, timeout=30) as r:
        init_resp = json.loads(r.read())
    if init_resp.get("error", {}).get("code", "ok") != "ok":
        raise RuntimeError(f"TikTok init error: {init_resp['error']}")
    upload_url  = init_resp["data"]["upload_url"]
    publish_id  = init_resp["data"]["publish_id"]

    # Step 2 — upload chunks
    with open(filepath, "rb") as fh:
        for chunk_idx in range(total_chunks):
            chunk = fh.read(chunk_size)
            start = chunk_idx * chunk_size
            end   = start + len(chunk) - 1
            put_req = _urllib_req.Request(upload_url, data=chunk, method="PUT")
            put_req.add_header("Content-Type",   "video/mp4")
            put_req.add_header("Content-Length", str(len(chunk)))
            put_req.add_header("Content-Range",  f"bytes {start}-{end}/{file_size}")
            with _urllib_req.urlopen(put_req, timeout=120) as r:
                r.read()

    print(f"  [tiktok] uploaded — publish_id={publish_id}", flush=True)
    return publish_id


@app.route("/tiktok/status")
def tiktok_status():
    if not _TIKTOK_KEY:
        return jsonify({"configured": False, "authed": False})
    tok = _tiktok_load_token()
    username = ""
    if tok:
        try:
            req = _urllib_req.Request(
                "https://open.tiktokapis.com/v2/user/info/?fields=display_name",
                headers={"Authorization": f"Bearer {tok['access_token']}"},
            )
            with _urllib_req.urlopen(req, timeout=10) as r:
                info = json.loads(r.read())
            username = info.get("data", {}).get("user", {}).get("display_name", "")
        except Exception:
            pass
    return jsonify({"configured": bool(_TIKTOK_KEY), "authed": tok is not None, "username": username})


@app.route("/tiktok/auth")
def tiktok_auth():
    global _tiktok_pending_state
    if not _TIKTOK_KEY:
        return jsonify({"error": "Set TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET in .env"}), 400
    import secrets as _sec
    _tiktok_pending_state = _sec.token_hex(16)
    params = urllib.parse.urlencode({
        "client_key": _TIKTOK_KEY,
        "response_type": "code",
        "scope": _TIKTOK_SCOPE,
        "redirect_uri": _TIKTOK_REDIRECT,
        "state": _tiktok_pending_state,
    })
    return jsonify({"auth_url": f"https://www.tiktok.com/v2/auth/authorize/?{params}"})


@app.route("/tiktok/callback")
def tiktok_callback():
    global _tiktok_pending_state
    code  = request.args.get("code", "")
    state = request.args.get("state", "")
    if not code:
        return "TikTok auth failed — no code returned.", 400
    if state != _tiktok_pending_state:
        return "State mismatch — possible CSRF.", 400
    body = urllib.parse.urlencode({
        "client_key":    _TIKTOK_KEY,
        "client_secret": _TIKTOK_SECRET,
        "code":          code,
        "grant_type":    "authorization_code",
        "redirect_uri":  _TIKTOK_REDIRECT,
    }).encode()
    req = _urllib_req.Request(
        "https://open.tiktokapis.com/v2/oauth/token/",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with _urllib_req.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
    except _urllib_req.HTTPError as e:
        return f"Token exchange failed: {e.read().decode()}", 400
    tok = {
        "access_token":       data["access_token"],
        "refresh_token":      data.get("refresh_token", ""),
        "open_id":            data.get("open_id", ""),
        "expires_at":         time.time() + int(data.get("expires_in", 86400)),
        "refresh_expires_at": time.time() + int(data.get("refresh_expires_in", 31536000)),
    }
    with open(_TIKTOK_TOKEN, "w") as f:
        json.dump(tok, f)
    _tiktok_pending_state = None
    return "<script>window.close()</script><p>TikTok connected! Close this tab.</p>"


@app.route("/tiktok/upload/<job_id>", methods=["POST"])
def tiktok_upload(job_id):
    job = JOBS.get(job_id)
    if not job or not job.get("file") or not os.path.exists(job["file"]):
        return jsonify({"error": "Clip not found."}), 404
    data  = request.get_json(silent=True) or {}
    title = data.get("title") or job.get("name") or "clip"
    try:
        publish_id = _tiktok_upload_file(job["file"], title)
        return jsonify({"publish_id": publish_id,
                        "note": "Video sent to your TikTok inbox/drafts — review and post from the TikTok app."})
    except Exception as e:
        traceback.print_exc()
        msg = str(e)
        if "spam" in msg.lower() or "SPAM" in msg:
            msg = "TikTok flagged the upload as spam. Try again in a few minutes."
        elif "not connect" in msg.lower():
            msg = "TikTok not connected. Click Connect TikTok first."
        return jsonify({"error": msg}), 500


# ----------------------------- frontend -----------------------------

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Clipper</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=Space+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>
  :root{
    --ink:#14161b;--panel:#1b1e25;--panel-2:#22262f;
    --line:#2c3039;--line-bright:#3a3f4a;
    --text:#e9e7e1;--muted:#8b909c;--faint:#5d626d;
    --amber:#ffb13c;--amber-dim:#7a5a22;
    --in:#56d364;--out:#f76d6d;
    --radius:14px;
  }
  *{box-sizing:border-box}
  html,body{margin:0;height:100%;overflow:hidden}
  body{
    background:radial-gradient(1200px 500px at 50% -10%,#1d2128 0%,var(--ink) 60%),var(--ink);
    color:var(--text);font-family:"Space Grotesk",system-ui,sans-serif;
    -webkit-font-smoothing:antialiased;display:flex;flex-direction:column;padding:0;
  }
  .wrap{flex:1;display:flex;flex-direction:column;padding:14px 14px 0;min-height:0;max-width:none;margin:0}
  header{display:flex;align-items:center;gap:12px;margin-bottom:12px;flex:none}
  /* ── 3-column workspace ── */
  .workspace{flex:1;min-height:0;display:grid;grid-template-columns:26fr 37fr 37fr;gap:10px;padding-bottom:10px}
  .col{display:flex;flex-direction:column;background:linear-gradient(180deg,var(--panel) 0%,#181b21 100%);
    border:1px solid var(--line);border-radius:var(--radius);overflow:hidden;min-height:0}
  .col-head{padding:9px 14px;font-size:10px;letter-spacing:.08em;text-transform:uppercase;
    color:var(--muted);font-weight:700;border-bottom:1px solid var(--line);flex:none;
    background:rgba(255,255,255,.025);display:flex;align-items:center;gap:6px}
  .col-body{flex:1;overflow-y:auto;padding:14px;min-height:0;
    scrollbar-width:thin;scrollbar-color:var(--line-bright) transparent}
  /* Col 1 — video + transcript */
  .col-body-video{display:flex;flex-direction:column}
  .transcript-section{flex:1;display:flex;flex-direction:column;margin-top:12px;min-height:120px}
  #transcriptList{flex:1;overflow-y:auto;margin-top:5px;background:var(--ink);
    border:1px solid var(--line);border-radius:8px;min-height:80px}
  /* Col 2 — suggestions */
  .col-body-suggest{display:flex;flex-direction:column}
  .suggest-cards{overflow-y:auto;min-height:0;margin-top:8px}
  .suggest-cards.show{flex:1}
  /* Col 3 — cut controls + results (col-body scroll handles it all) */
  button.cut{width:100%;margin-top:12px}
  .results{margin-top:10px}
  footer{flex:none;text-align:center;font-size:10px;color:var(--faint);
    font-family:"Space Mono",monospace;padding:6px 0 10px}
  .logo{display:flex;align-items:center;gap:10px}
  .logo .mark{width:30px;height:30px;border:2px solid var(--amber);border-radius:7px;
    display:grid;place-items:center;color:var(--amber);font-family:"Space Mono",monospace;
    font-weight:700;font-size:13px}
  h1{font-size:22px;font-weight:700;letter-spacing:-.02em;margin:0}
  .tag{color:var(--muted);font-size:13px;font-family:"Space Mono",monospace;margin-left:auto}
  .card{background:linear-gradient(180deg,var(--panel) 0%,#181b21 100%);
    border:1px solid var(--line);border-radius:var(--radius);padding:20px;margin-bottom:16px}
  label{display:block;font-size:12px;letter-spacing:.06em;text-transform:uppercase;
    color:var(--muted);margin-bottom:8px;font-weight:500}
  input[type=text]{
    width:100%;background:var(--ink);border:1px solid var(--line-bright);
    color:var(--text);border-radius:10px;padding:13px 14px;font-size:15px;
    font-family:inherit;outline:none;transition:border-color .15s,box-shadow .15s}
  input[type=text]:focus{border-color:var(--amber);box-shadow:0 0 0 3px rgba(255,177,60,.13)}
  input::placeholder{color:var(--faint)}

  /* preview */
  .preview{display:none;gap:14px;align-items:center;margin-top:16px}
  .preview.show{display:flex}
  .thumb{width:116px;height:66px;border-radius:8px;object-fit:cover;
    background:var(--panel-2);border:1px solid var(--line);flex:none}
  .meta .ttl{font-size:15px;font-weight:600;line-height:1.3;margin-bottom:4px}
  .meta .sub{font-size:12px;color:var(--muted);font-family:"Space Mono",monospace}

  /* timeline */
  .timeline{margin-top:20px;display:none}
  .timeline.show{display:block}
  .track{position:relative;height:46px;border-radius:9px;overflow:hidden;
    cursor:crosshair;user-select:none;
    background:repeating-linear-gradient(90deg,#20232b 0 13px,#1a1d24 13px 14px),var(--panel-2);
    border:1px solid var(--line-bright)}
  #heatCanvas{position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;border-radius:8px}
  #segOverlays{position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none}
  .clip-preview{display:none;width:100%;border-radius:8px;margin-top:10px;
    background:#000;max-height:260px;outline:none}
  .clip-preview.show{display:block}
  .tc-row{display:flex;justify-content:space-between;margin-top:8px;
    font-family:"Space Mono",monospace;font-size:11px;color:var(--faint)}
  .peak-badge{display:none;margin-top:8px;gap:7px;align-items:center;
    font-family:"Space Mono",monospace;font-size:11px;color:var(--amber)}
  .peak-badge.show{display:flex}
  .peak-pill{background:rgba(255,177,60,.15);border:1px solid var(--amber-dim);
    border-radius:20px;padding:2px 10px;letter-spacing:.03em}

  /* segment list */
  .segs-wrap{margin-top:0;flex:none}
  .seg-row{display:flex;gap:8px;align-items:flex-end;margin-bottom:8px}
  .seg-dot{width:8px;height:8px;border-radius:50%;flex:none;margin-bottom:15px}
  .tfield{flex:1}
  .tfield label{display:flex;align-items:center;gap:6px}
  .tfield input{font-family:"Space Mono",monospace;letter-spacing:.04em}
  button.seg-del{background:none;border:1px solid var(--line-bright);color:var(--muted);
    border-radius:8px;width:36px;height:44px;cursor:pointer;font-size:14px;flex:none;
    transition:color .15s,border-color .15s;font-family:inherit}
  button.seg-del:hover{color:var(--out);border-color:var(--out)}
  button.add-seg{background:none;border:1px dashed var(--line-bright);color:var(--muted);
    border-radius:9px;padding:9px 14px;font-size:13px;font-family:inherit;cursor:pointer;
    width:100%;margin-top:2px;transition:color .15s,border-color .15s}
  button.add-seg:hover{color:var(--amber);border-color:var(--amber)}

  /* quality/ratio row */
  .row{display:grid;grid-template-columns:1fr 1fr;gap:12px;align-items:end;margin-top:12px;flex:none}
  select{width:100%;background:var(--ink);border:1px solid var(--line-bright);color:var(--text);
    border-radius:10px;padding:13px 14px;font-size:14px;font-family:inherit;outline:none;
    appearance:none;cursor:pointer;
    background-image:linear-gradient(45deg,transparent 50%,var(--muted) 50%),
      linear-gradient(135deg,var(--muted) 50%,transparent 50%);
    background-position:calc(100% - 18px) center,calc(100% - 13px) center;
    background-size:5px 5px,5px 5px;background-repeat:no-repeat}
  select:focus{border-color:var(--amber)}
  button.cut{background:var(--amber);color:#1a1205;border:none;border-radius:10px;
    padding:14px 22px;font-size:15px;font-weight:700;font-family:inherit;cursor:pointer;
    white-space:nowrap;transition:transform .08s,filter .15s}
  button.cut:hover{filter:brightness(1.07)}
  button.cut:active{transform:translateY(1px)}
  button.cut:disabled{background:var(--panel-2);color:var(--faint);cursor:not-allowed}

  .hint{color:var(--faint);font-size:12px;font-family:"Space Mono",monospace;margin-top:10px}

  /* per-clip result cards */
  .clip-card{background:var(--panel-2);border:1px solid var(--line);border-radius:10px;
    padding:12px 14px;margin-bottom:8px}
  .clip-label{display:flex;align-items:center;gap:6px;font-family:"Space Mono",monospace;
    font-size:11px;color:var(--muted);margin-bottom:8px}
  .clip-dot{width:7px;height:7px;border-radius:50%;flex:none}
  .cbar{height:6px;border-radius:4px;background:var(--ink);overflow:hidden;border:1px solid var(--line)}
  .cbar>i{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--amber),#ffd089);
    transition:width .25s ease;border-radius:4px}
  .cbar.indet>i{width:35%;animation:slide 1.1s ease-in-out infinite}
  @keyframes slide{0%{margin-left:-35%}100%{margin-left:100%}}
  .cphase{font-family:"Space Mono",monospace;font-size:11px;color:var(--muted);margin-top:5px}
  .cdl-row{display:none;align-items:center;gap:10px;margin-top:8px}
  .cdl-row.show{display:flex}
  a.cdl{background:var(--in);color:#08210c;text-decoration:none;border-radius:8px;
    padding:8px 16px;font-weight:700;font-size:13px}
  .cfname{font-family:"Space Mono",monospace;font-size:11px;color:var(--muted);word-break:break-all}
  .cerr{color:#ffb4b4;font-size:12px;margin-top:6px;display:none}
  .cerr.show{display:block}

  .err{display:none;margin-top:14px;background:rgba(247,109,109,.1);
    border:1px solid rgba(247,109,109,.4);color:#ffb4b4;border-radius:10px;
    padding:12px 14px;font-size:13px}
  .err.show{display:block}
  footer{text-align:center;color:var(--faint);font-size:12px;margin-top:30px;
    font-family:"Space Mono",monospace}

  /* ── AI suggest ── */
  .suggest-bar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;flex:none}
  button.suggest-btn{background:linear-gradient(135deg,#7c3aed,#4f46e5);color:#fff;
    border:none;border-radius:10px;padding:9px 16px;font-size:13px;font-weight:700;
    font-family:inherit;cursor:pointer;transition:filter .15s,transform .08s;white-space:nowrap}
  button.suggest-btn:hover{filter:brightness(1.12)}
  button.suggest-btn:active{transform:translateY(1px)}
  button.suggest-btn:disabled{background:var(--panel-2);color:var(--faint);cursor:not-allowed}
  select.suggest-plat{background:var(--ink);border:1px solid var(--line-bright);color:var(--text);
    border-radius:9px;padding:8px 12px;font-size:13px;font-family:inherit;outline:none;
    appearance:none;cursor:pointer;
    background-image:linear-gradient(45deg,transparent 50%,var(--muted) 50%),
      linear-gradient(135deg,var(--muted) 50%,transparent 50%);
    background-position:calc(100% - 14px) center,calc(100% - 9px) center;
    background-size:4px 4px,4px 4px;background-repeat:no-repeat;padding-right:28px}
  select.suggest-plat:focus{border-color:#7c3aed}
  .suggest-status{font-size:12px;font-family:"Space Mono",monospace;color:var(--faint)}
  .suggest-cards{margin-top:10px;display:none}
  .suggest-cards.show{display:block}
  .scard{background:var(--ink);border:1px solid var(--line);border-radius:9px;
    padding:10px 12px;margin-bottom:6px;cursor:pointer;transition:border-color .15s,background .15s;
    display:flex;gap:10px;align-items:flex-start}
  .scard:hover{border-color:#7c3aed;background:rgba(124,58,237,.07)}
  .scard-num{font-family:"Space Mono",monospace;font-size:10px;color:#7c3aed;
    background:rgba(124,58,237,.15);border-radius:5px;padding:2px 6px;white-space:nowrap;flex:none}
  .scard-body{flex:1;min-width:0}
  .scard-title{font-size:13px;font-weight:600;margin-bottom:3px;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .scard-tc{font-family:"Space Mono",monospace;font-size:10px;color:var(--amber);margin-bottom:3px}
  .scard-reason{font-size:11px;color:var(--muted);line-height:1.4}
  .scard-actions{display:flex;align-items:center;gap:6px;flex:none;padding-top:1px}
  .scard-preview{font-size:11px;color:var(--muted);background:none;border:1px solid var(--line-bright);
    border-radius:5px;padding:3px 8px;cursor:pointer;font-family:inherit;
    transition:all .15s;white-space:nowrap;line-height:1.4}
  .scard-preview:hover{border-color:#7c3aed;color:#a78bfa}
  .scard-preview.active{border-color:#7c3aed;color:#a78bfa;background:rgba(124,58,237,.14)}
  .scard-add{font-size:11px;color:#7c3aed;white-space:nowrap;flex:none;padding:3px 2px;cursor:pointer}
  /* Preview panel */
  .preview-panel{display:none;margin:8px 0 10px;background:var(--ink);
    border:1px solid #7c3aed;border-radius:10px;overflow:hidden}
  .preview-panel.show{display:block}
  .preview-panel-head{display:flex;align-items:center;justify-content:space-between;
    padding:8px 12px;border-bottom:1px solid var(--line);background:rgba(124,58,237,.08)}
  .preview-panel-title{font-size:12px;font-weight:600;color:#a78bfa;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:68%}
  .preview-close{background:none;border:none;color:var(--muted);cursor:pointer;
    font-size:16px;padding:0 4px;line-height:1;transition:color .15s}
  .preview-close:hover{color:var(--text)}
  .preview-embed{position:relative;width:100%;padding-bottom:56.25%;background:#000}
  .preview-embed iframe,.preview-fallback{position:absolute;top:0;left:0;width:100%;height:100%;border:none}
  .preview-fallback{display:flex;align-items:center;justify-content:center;
    color:var(--muted);font-size:13px;flex-direction:column;gap:6px;text-align:center;padding:16px}
  .preview-controls{display:flex;align-items:center;gap:10px;padding:9px 12px;flex-wrap:wrap;
    border-top:1px solid var(--line)}
  .preview-tfield{display:flex;align-items:center;gap:5px;font-size:12px;color:var(--muted)}
  .preview-tfield input{width:74px;background:var(--panel-2);border:1px solid var(--line-bright);
    color:var(--text);border-radius:6px;padding:4px 7px;font-size:12px;
    font-family:"Space Mono",monospace;outline:none;transition:border-color .15s}
  .preview-tfield input:focus{border-color:#7c3aed}
  .preview-dur{font-family:"Space Mono",monospace;font-size:11px;color:var(--faint)}
  .preview-add-btn{margin-left:auto;background:#7c3aed;color:#fff;border:none;
    border-radius:7px;padding:6px 14px;font-size:12px;font-family:inherit;
    cursor:pointer;transition:filter .15s;white-space:nowrap}
  .preview-add-btn:hover{filter:brightness(1.12)}

  /* ── Platform presets ── */
  .presets{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-top:14px}
  .presets-lbl{font-size:11px;letter-spacing:.06em;text-transform:uppercase;
    color:var(--faint);font-weight:500;margin-right:2px}
  .preset-btn{background:none;border:1px solid var(--line-bright);color:var(--muted);
    border-radius:20px;padding:4px 11px;font-size:12px;font-family:"Space Mono",monospace;
    cursor:pointer;transition:all .15s;letter-spacing:.02em}
  .preset-btn:hover{border-color:var(--amber);color:var(--amber)}
  .preset-btn.active{background:rgba(255,177,60,.15);color:var(--amber);border-color:var(--amber)}

  /* ── Transcript panel (always visible in col 1) ── */
  .transcript-wrap{margin-top:0;display:flex;flex-direction:column;flex:1;min-height:0}
  .tscript-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:6px}
  .tscript-label{font-size:11px;letter-spacing:.06em;text-transform:uppercase;
    color:var(--muted);font-weight:500}
  .tscript-status{font-size:11px;font-family:"Space Mono",monospace;color:var(--faint)}
  .transcript-search{width:100%;background:var(--ink);border:1px solid var(--line-bright);
    color:var(--text);border-radius:8px;padding:8px 12px;font-size:13px;
    font-family:inherit;outline:none;transition:border-color .15s}
  .transcript-search:focus{border-color:var(--amber)}
  .transcript-list{max-height:180px;overflow-y:auto;margin-top:5px;
    background:var(--ink);border:1px solid var(--line);border-radius:8px}
  .tcue{padding:7px 11px;cursor:pointer;border-bottom:1px solid var(--line);
    font-size:12px;display:flex;gap:8px;transition:background .1s}
  .tcue:last-child{border-bottom:none}
  .tcue:hover{background:var(--panel-2)}
  .tcue .ts{color:var(--amber);font-family:"Space Mono",monospace;font-size:10px;
    white-space:nowrap;padding-top:2px;flex:none}
  .tcue .txt{color:var(--text);line-height:1.4}
  .tcue .txt mark{background:rgba(255,177,60,.3);color:var(--text);border-radius:2px;padding:0 2px}

  /* ── Auth bar ── */
  .auth-bar{display:flex;align-items:center;gap:14px;
    background:var(--panel);border:1px solid var(--line);border-radius:10px;
    padding:10px 16px;margin-bottom:10px;flex:none}
  .auth-bar-label{display:flex;align-items:center;gap:8px;white-space:nowrap;flex:none}
  .auth-bar-icon{width:26px;height:26px;border-radius:7px;
    background:rgba(255,177,60,.1);border:1px solid var(--amber-dim);
    display:grid;place-items:center;font-size:15px;flex:none}
  .auth-bar-title{font-size:11px;letter-spacing:.07em;text-transform:uppercase;
    color:var(--faint);font-weight:700}
  .auth-divider{width:1px;height:22px;background:var(--line-bright);flex:none}
  .auth-status{flex:1;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
  .auth-connect-btn{background:linear-gradient(135deg,#312e7a 0%,#6d28d9 100%);
    color:#e0d9ff;border:1px solid rgba(139,92,246,.4);border-radius:8px;
    padding:7px 18px;font-size:12px;font-family:inherit;font-weight:600;
    cursor:pointer;transition:filter .15s;letter-spacing:.02em;white-space:nowrap}
  .auth-connect-btn:hover{filter:brightness(1.18)}
  .auth-pill{display:inline-flex;align-items:center;gap:5px;font-size:12px;
    font-family:"Space Mono",monospace;padding:4px 11px;border-radius:20px;
    border:1px solid var(--line);color:var(--muted);white-space:nowrap}
  .auth-pill.ok{border-color:#2a6a3a;color:var(--in);background:rgba(86,211,100,.07)}
  .auth-pill.warn{border-color:var(--amber-dim);color:var(--amber);background:rgba(255,177,60,.07)}
  .auth-pill .picon{font-size:13px}
  .auth-reconnect{background:none;border:none;color:var(--faint);font-size:11px;
    font-family:"Space Mono",monospace;cursor:pointer;padding:0;
    text-decoration:underline;text-underline-offset:2px;transition:color .15s}
  .auth-reconnect:hover{color:var(--text)}
  /* ── Burn captions row (col 3) ── */
  .options-row{display:flex;align-items:center;gap:10px;margin-top:10px;flex:none}
  .burn-row{display:flex;align-items:center;gap:8px}
  .burn-lbl{font-size:12px;color:var(--muted);cursor:pointer;user-select:none}
  .tgl{position:relative;width:34px;height:18px;flex:none}
  .tgl input{opacity:0;width:0;height:0;position:absolute}
  .tgl-sl{position:absolute;inset:0;background:var(--line-bright);border-radius:18px;
    transition:.18s;cursor:pointer}
  .tgl-sl:before{content:"";position:absolute;height:12px;width:12px;
    left:3px;bottom:3px;background:#aaa;border-radius:50%;transition:.18s}
  .tgl input:checked+.tgl-sl{background:var(--amber)}
  .tgl input:checked+.tgl-sl:before{transform:translateX(16px);background:#1a1205}

  /* ── Push buttons inside clip cards (Drive + YouTube) ── */
  .push-row{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
  button.gdrive-btn,button.yt-btn{background:none;border:1px solid var(--line-bright);
    color:var(--muted);border-radius:7px;padding:5px 12px;font-size:11px;
    font-family:"Space Mono",monospace;cursor:pointer;transition:all .15s;white-space:nowrap}
  button.gdrive-btn{border-color:#2a3a5c;color:#5b8dd9}
  button.yt-btn{border-color:#5c1a1a;color:#e05555}
  button.tt-btn{border-color:#1a1a1a;color:#aaa}
  button.gdrive-btn:hover:not(:disabled){border-color:#4285f4;color:#4285f4;background:rgba(66,133,244,.08)}
  button.yt-btn:hover:not(:disabled){border-color:#ff4444;color:#ff4444;background:rgba(255,68,68,.08)}
  button.tt-btn:hover:not(:disabled){border-color:#69c9d0;color:#69c9d0;background:rgba(105,201,208,.08)}
  button.gdrive-btn:disabled,button.yt-btn:disabled,button.tt-btn:disabled{opacity:.55;cursor:not-allowed}
  button.gdrive-btn.uploading,button.yt-btn.uploading,button.tt-btn.uploading{opacity:.6;pointer-events:none}
  .push-links{margin-top:4px;display:flex;flex-direction:column;gap:3px}
  a.gdrive-link,a.yt-link{font-size:11px;font-family:"Space Mono",monospace;
    text-decoration:none;word-break:break-all}
  a.gdrive-link{color:#4285f4}
  a.yt-link{color:#ff0000}
  a.gdrive-link:hover,a.yt-link:hover{text-decoration:underline}

  /* ── Menu bar / pages ── */
  .menubar{display:flex;gap:6px;flex:none;margin-bottom:12px}
  .menu-tab{background:none;border:1px solid var(--line-bright);color:var(--muted);
    border-radius:9px;padding:8px 18px;font-size:13px;font-family:inherit;font-weight:600;
    cursor:pointer;transition:all .15s}
  .menu-tab:hover{border-color:var(--amber);color:var(--amber)}
  .menu-tab.active{background:rgba(255,177,60,.15);color:var(--amber);border-color:var(--amber)}
  .page{display:none;flex:1;flex-direction:column;min-height:0}
  .page.active{display:flex}

  /* ── Video page (Script + Edit) ── */
  .video-url-card{flex:none}
  .subnav{display:flex;gap:6px;flex:none;margin-bottom:14px}
  .subnav-tab{background:none;border:1px solid var(--line-bright);color:var(--muted);
    border-radius:9px;padding:7px 15px;font-size:12px;font-family:inherit;font-weight:600;
    cursor:pointer;transition:all .15s}
  .subnav-tab:hover{border-color:var(--amber);color:var(--amber)}
  .subnav-tab.active{background:rgba(255,177,60,.15);color:var(--amber);border-color:var(--amber)}
  .video-section{display:none;flex:1;flex-direction:column;min-height:0}
  .video-section.active{display:flex;overflow-y:auto;scrollbar-width:thin;scrollbar-color:var(--line-bright) transparent}
  .settings-card{max-width:640px;width:100%;margin:0 auto;flex:none}

  /* ── Video → Script page ── */
  .script-card{max-width:860px;width:100%;margin:0 auto;display:flex;flex-direction:column;
    flex:1;min-height:0}
  .script-input-row{display:flex;gap:10px}
  .script-input-row input{flex:1}
  .script-input-row button{width:auto;margin-top:0;white-space:nowrap}
  .script-toolbar{display:flex;align-items:center;justify-content:space-between;
    margin-top:14px;flex-wrap:wrap;gap:10px}
  .script-toolbar-right{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
  .script-ts-toggle{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--muted);
    cursor:pointer;user-select:none}
  button.script-action{background:none;border:1px dashed var(--line-bright);color:var(--muted);
    border-radius:9px;padding:8px 14px;font-size:13px;font-family:inherit;cursor:pointer;
    width:auto;margin-top:0;transition:color .15s,border-color .15s}
  button.script-action:hover{color:var(--amber);border-color:var(--amber)}
  button.script-action:disabled{opacity:.5;cursor:not-allowed}
  .script-view-toggle{display:none;gap:4px}
  .script-view-toggle.show{display:flex}
  .script-summary{display:none;font-size:12px;color:var(--muted);margin-top:10px;
    padding:10px 12px;background:rgba(124,58,237,.08);border:1px solid rgba(124,58,237,.25);
    border-radius:8px;line-height:1.5}
  .script-summary.show{display:block}
  #scriptText{flex:1;margin-top:10px;width:100%;min-height:200px;resize:none;
    background:var(--ink);border:1px solid var(--line);border-radius:10px;color:var(--text);
    padding:16px;font-size:14px;line-height:1.7;font-family:inherit;outline:none}
  #scriptText:focus{border-color:var(--amber)}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="logo"><div class="mark">▣</div><h1>Team MoneyTuber - Clipper</h1></div>
    <span class="tag">in · out · cut</span>
  </header>

  <nav class="menubar">
    <button class="menu-tab active" data-page="clipper">Clipper</button>
    <button class="menu-tab" data-page="video">Video</button>
  </nav>

  <!-- ══ Page: Clipper ══ -->
  <div class="page active" id="page-clipper">

  <!-- ── Auth bar ── -->
  <div class="auth-bar">
    <div class="auth-bar-label">
      <div class="auth-bar-icon">G</div>
      <span class="auth-bar-title">Google</span>
    </div>
    <div class="auth-divider"></div>
    <div class="auth-status" id="driveArea"></div>
    <div class="auth-divider"></div>
    <div class="auth-bar-label">
      <div class="auth-bar-icon" style="background:rgba(0,0,0,.3);border-color:#333;color:#fff;font-size:11px;font-weight:700">TT</div>
      <span class="auth-bar-title">TikTok</span>
    </div>
    <div class="auth-divider"></div>
    <div class="auth-status" id="tiktokArea"></div>
  </div>

  <div class="workspace">

    <!-- ── Col 1: Video + Transcript ── -->
    <div class="col">
      <div class="col-head">&#9654; Video</div>
      <div class="col-body col-body-video">
        <label for="url">Video URL</label>
        <input id="url" type="text" placeholder="https://www.youtube.com/watch?v=…" autocomplete="off" spellcheck="false">

        <div class="preview" id="preview">
          <img class="thumb" id="thumb" alt="">
          <div class="meta">
            <div class="ttl" id="ptitle"></div>
            <div class="sub" id="psub"></div>
          </div>
        </div>

        <div class="timeline" id="timeline">
          <div class="track">
            <canvas id="heatCanvas"></canvas>
            <div id="segOverlays"></div>
          </div>
          <div class="tc-row"><span>00:00:00</span><span id="dur">--:--:--</span></div>
          <div class="peak-badge" id="peakBadge">
            <span class="peak-pill">&#9670; Peak detected</span><span id="peakInfo"></span>
          </div>
        </div>

        <div class="transcript-wrap" id="transcriptWrap">
          <div class="tscript-head">
            <span class="tscript-label">Transcript</span>
            <span class="tscript-status" id="transcriptStatus"></span>
          </div>
          <input type="text" class="transcript-search" id="transcriptSearch" placeholder="Search — click a line to add segment">
          <div class="transcript-list" id="transcriptList"></div>
        </div>
      </div>
    </div>

    <!-- ── Col 2: AI Suggestions ── -->
    <div class="col">
      <div class="col-head">&#10024; Suggestions</div>
      <div class="col-body col-body-suggest">
        <div class="suggest-bar" id="suggestBar">
          <button class="suggest-btn" id="suggestBtn">&#10024; Suggest clips</button>
          <select class="suggest-plat" id="suggestPlat">
            <option>TikTok</option>
            <option>Shorts</option>
            <option>Reels</option>
            <option>Twitter/X</option>
            <option>LinkedIn</option>
          </select>
          <span class="suggest-status" id="suggestStatus"></span>
        </div>

        <div class="suggest-cards" id="suggestCards"></div>

        <div class="preview-panel" id="previewPanel">
          <div class="preview-panel-head">
            <span class="preview-panel-title" id="previewTitle"></span>
            <button class="preview-close" id="previewClose" title="Close preview">&#10005;</button>
          </div>
          <div class="preview-embed" id="previewEmbed"></div>
          <div class="preview-controls">
            <div class="preview-tfield"><label>Start</label><input type="text" id="previewStart" placeholder="0:00"></div>
            <div class="preview-tfield"><label>End</label><input type="text" id="previewEnd" placeholder="0:30"></div>
            <span class="preview-dur" id="previewDur"></span>
            <button class="preview-add-btn" id="previewAddBtn">+ Add to segments</button>
          </div>
        </div>
      </div>
    </div>

    <!-- ── Col 3: Cut ── -->
    <div class="col">
      <div class="col-head">&#9986; Cut</div>
      <div class="col-body col-body-cut">
        <div class="segs-wrap">
          <div id="segList"></div>
          <button class="add-seg" id="addSeg">+ Add segment</button>
        </div>

        <div class="presets">
          <span class="presets-lbl">Presets</span>
          <button class="preset-btn" data-ratio="9:16" data-quality="best" data-maxdur="60">TikTok</button>
          <button class="preset-btn" data-ratio="9:16" data-quality="1080" data-maxdur="59">Shorts</button>
          <button class="preset-btn" data-ratio="9:16" data-quality="best" data-maxdur="90">Reels</button>
          <button class="preset-btn" data-ratio="16:9" data-quality="720" data-maxdur="140">Twitter/X</button>
          <button class="preset-btn" data-ratio="16:9" data-quality="best" data-maxdur="600">LinkedIn</button>
        </div>

        <div class="row">
          <div>
            <label for="quality">Quality</label>
            <select id="quality">
              <option value="best">Best available</option>
              <option value="1080">1080p</option>
              <option value="720">720p</option>
              <option value="480">480p</option>
              <option value="audio">Audio only (mp3)</option>
            </select>
          </div>
          <div>
            <label for="ratio">Aspect ratio</label>
            <select id="ratio">
              <option value="original">Original</option>
              <option value="16:9">16:9 — Landscape</option>
              <option value="9:16">9:16 — Portrait</option>
              <option value="1:1">1:1 — Square</option>
              <option value="4:3">4:3 — Classic</option>
            </select>
          </div>
        </div>

        <div class="options-row">
          <div class="burn-row" id="burnRow">
            <label class="tgl" title="Burn captions into video">
              <input type="checkbox" id="burnCaptions">
              <span class="tgl-sl"></span>
            </label>
            <span class="burn-lbl" onclick="document.getElementById('burnCaptions').click()">Burn captions</span>
          </div>
        </div>

        <button class="cut" id="cut">Cut clip</button>

        <div class="hint">mm:ss or hh:mm:ss · multiple segments = bulk cut</div>
        <div class="err" id="err"></div>
        <div class="results" id="results"></div>
      </div>
    </div>

  </div><!-- /workspace -->

  </div><!-- /page-clipper -->

  <!-- ══ Page: Video ══ -->
  <div class="page" id="page-video">
    <div class="card video-url-card">
      <label for="videoUrl">Video URL</label>
      <div class="script-input-row">
        <input id="videoUrl" type="text" placeholder="https://www.youtube.com/watch?v=…" autocomplete="off" spellcheck="false">
        <button class="cut" id="videoFetchBtn">Load video</button>
      </div>
      <div class="preview" id="videoPreview">
        <img class="thumb" id="videoThumb" alt="">
        <div class="meta">
          <div class="ttl" id="videoTitle"></div>
          <div class="sub" id="videoSub"></div>
        </div>
      </div>
    </div>

    <!-- ── Sub-nav ── -->
    <nav class="subnav">
      <button class="subnav-tab active" data-section="script">&#128221; Video to Script</button>
      <button class="subnav-tab" data-section="edit">&#9998; Video to Edit</button>
    </nav>

    <!-- ── Section: Video to Script ── -->
    <div class="video-section active" id="section-script">
    <div class="card script-card">
      <div class="script-toolbar">
        <span class="tscript-status" id="scriptStatus"></span>
        <div class="script-toolbar-right">
          <div class="script-view-toggle" id="scriptViewToggle">
            <button class="preset-btn active" data-view="raw">Raw</button>
            <button class="preset-btn" data-view="clean" id="viewCleanBtn" style="display:none">AI cleaned</button>
            <button class="preset-btn" data-view="chapters" id="viewChaptersBtn" style="display:none">Chapters</button>
          </div>
          <label class="script-ts-toggle" id="scriptTsToggleWrap">
            <input type="checkbox" id="scriptTsToggle" style="margin:0">
            Show timestamps
          </label>
          <button class="suggest-btn" id="scriptCleanBtn" disabled>&#10024; Clean up with AI</button>
          <button class="suggest-btn" id="scriptChaptersBtn" disabled>&#128278; Chapters</button>
          <button class="script-action" id="scriptCopyBtn" disabled>Copy</button>
          <button class="script-action" id="scriptDownloadBtn" disabled>Download .txt</button>
        </div>
      </div>

      <div class="script-summary" id="scriptSummary"></div>

      <textarea id="scriptText" readonly placeholder="The pulled script will show up here…"></textarea>
    </div>
    </div><!-- /section-script -->

    <!-- ── Section: Video to Edit (full download for editing elsewhere) ── -->
    <div class="video-section" id="section-edit">
    <div class="card settings-card">
      <label for="dlQuality">Download quality</label>
      <select id="dlQuality">
        <option value="best">Best available</option>
        <option value="1080">1080p</option>
        <option value="720">720p</option>
        <option value="480">480p</option>
        <option value="audio">Audio only (mp3)</option>
      </select>
      <button class="cut" id="dlBtn">Download video</button>
      <div class="hint">Downloads the full video — no cutting — ready to bring into your editor of choice.</div>

      <div class="clip-card" id="dlCard" style="display:none;margin-top:14px">
        <div class="clip-label"><span class="clip-dot" style="background:#ffb13c"></span><span id="dlLabel">Downloading full video</span></div>
        <div class="cbar indet" id="dlBar"><i></i></div>
        <div class="cphase" id="dlPhase">Starting…</div>
        <div class="cdl-row" id="dlRow">
          <a class="cdl" id="dlLink" href="#">Download</a>
          <span class="cfname" id="dlFname"></span>
        </div>
        <div class="cerr" id="dlErr"></div>
      </div>
    </div>
    </div><!-- /section-edit -->
  </div><!-- /page-video -->

  <footer>runs locally · yt-dlp + ffmpeg</footer>
</div>

<script>
const $ = id => document.getElementById(id);
let duration = null, heatmapData = null;

const SEG_COLORS = [
  {bg:"rgba(255,177,60,.22)",  border:"#ffb13c"},
  {bg:"rgba(86,211,100,.22)",  border:"#56d364"},
  {bg:"rgba(74,158,255,.22)",  border:"#4a9eff"},
  {bg:"rgba(188,140,255,.22)", border:"#bc8cff"},
  {bg:"rgba(247,109,109,.22)", border:"#f76d6d"},
];

// ── Heatmap canvas ─────────────────────────────────────────────────
function drawHeatmap(){
  const cv=$("heatCanvas"); if(!cv) return;
  const track=cv.parentElement;
  const w=track?track.offsetWidth:cv.offsetWidth;
  const h=track?track.offsetHeight:cv.offsetHeight;
  if(!w||!h){requestAnimationFrame(drawHeatmap);return;}
  cv.width=w; cv.height=h;
  const ctx=cv.getContext("2d");
  ctx.clearRect(0,0,w,h);
  if(!heatmapData||!heatmapData.length||!duration) return;
  heatmapData.forEach(s=>{
    const x1=s.start_time/duration*w, x2=s.end_time/duration*w;
    ctx.fillStyle=`rgba(255,177,60,${(s.value*0.82).toFixed(3)})`;
    ctx.fillRect(x1,0,Math.max(1,x2-x1),h);
  });
}

function detectPeak(hm,dur){
  if(!hm||!hm.length||!dur) return null;
  const win=Math.min(60,Math.max(10,dur*0.12));
  const step=Math.max(0.5,dur/400);
  let best=-1,bS=0;
  for(let t=0;t+win<=dur;t+=step){
    let score=0,cov=0;
    hm.forEach(s=>{
      const ov=Math.min(s.end_time,t+win)-Math.max(s.start_time,t);
      if(ov>0){score+=s.value*ov;cov+=ov;}
    });
    const avg=cov>0?score/cov:0;
    if(avg>best){best=avg;bS=t;}
  }
  return {start:Math.round(bS),end:Math.min(Math.round(dur),Math.round(bS+win))};
}

// ── Segment overlays on track (with drag handles) ─────────────────
function drawSegmentOverlays(){
  const c=$("segOverlays"); if(!c||!duration) return;
  c.innerHTML="";
  getSegments().forEach((seg,i)=>{
    const s=toSecs(seg.start),e=toSecs(seg.end);
    if(s==null||e==null) return;
    const col=SEG_COLORS[i%SEG_COLORS.length];
    const a=Math.max(0,Math.min(s,duration))/duration*100;
    const b=Math.max(0,Math.min(e,duration))/duration*100;
    const ov=document.createElement("div");
    ov.style.cssText=`position:absolute;top:0;bottom:0;left:${Math.min(a,b)}%;`+
      `width:${Math.max(0,Math.abs(b-a))}%;background:${col.bg};`+
      `pointer-events:auto;cursor:move`;
    ov.dataset.segIdx=i;
    // left (start) handle
    const lh=document.createElement("div");
    lh.dataset.segIdx=i; lh.dataset.which="start";
    lh.style.cssText=`position:absolute;top:0;bottom:0;left:-1px;width:10px;`+
      `cursor:ew-resize;border-left:2px solid ${col.border}`;
    // right (end) handle
    const rh=document.createElement("div");
    rh.dataset.segIdx=i; rh.dataset.which="end";
    rh.style.cssText=`position:absolute;top:0;bottom:0;right:-1px;width:10px;`+
      `cursor:ew-resize;border-right:2px solid ${col.border}`;
    ov.appendChild(lh); ov.appendChild(rh);
    c.appendChild(ov);
  });
}

// ── Timeline drag interactions ─────────────────────────────────────
let _drag=null;

function _trackPct(e){
  const r=document.querySelector(".track").getBoundingClientRect();
  return Math.max(0,Math.min(1,(e.clientX-r.left)/r.width));
}
function _setNearest(t){
  if(!$("segList").children.length) return;
  let best={dist:Infinity,idx:0,which:"start"};
  getSegments().forEach((seg,i)=>{
    const s=toSecs(seg.start)??0, e2=toSecs(seg.end)??duration;
    if(Math.abs(t-s)<best.dist) best={dist:Math.abs(t-s),idx:i,which:"start"};
    if(Math.abs(t-e2)<best.dist) best={dist:Math.abs(t-e2),idx:i,which:"end"};
  });
  const row=$("segList").children[best.idx];
  if(row){
    row.querySelector(`.seg-${best.which}`).value=tc(Math.round(Math.max(0,Math.min(duration,t))));
    drawSegmentOverlays(); updateCutBtn();
  }
}

document.querySelector(".track").addEventListener("mousedown",e=>{
  if(!duration) return;
  e.preventDefault();
  const t=_trackPct(e)*duration;
  const tgt=e.target;
  if(tgt.dataset.which){
    _drag={type:"handle",idx:+tgt.dataset.segIdx,which:tgt.dataset.which};
  } else if(tgt.dataset.segIdx!=null){
    const idx=+tgt.dataset.segIdx;
    const row=$("segList").children[idx];
    _drag={type:"move",idx,anchorT:t,
      origS:toSecs(row.querySelector(".seg-start").value)||0,
      origE:toSecs(row.querySelector(".seg-end").value)||0};
  } else {
    _setNearest(t);
  }
});
document.addEventListener("mousemove",e=>{
  if(!_drag||!duration) return;
  const t=Math.max(0,Math.min(duration,_trackPct(e)*duration));
  const row=$("segList").children[_drag.idx];
  if(!row) return;
  if(_drag.type==="handle"){
    row.querySelector(`.seg-${_drag.which}`).value=tc(Math.round(t));
  } else {
    const dt=t-_drag.anchorT;
    const ns=Math.max(0,_drag.origS+dt), ne=Math.min(duration,_drag.origE+dt);
    if(ne>ns){
      row.querySelector(".seg-start").value=tc(Math.round(ns));
      row.querySelector(".seg-end").value=tc(Math.round(ne));
    }
  }
  drawSegmentOverlays();
});
document.addEventListener("mouseup",()=>{if(_drag){updateCutBtn();_drag=null;}});

// ── Segment list ───────────────────────────────────────────────────
function makeSegRow(startSec, endSec){
  const idx=$("segList").children.length;
  const col=SEG_COLORS[idx%SEG_COLORS.length];
  const sv=startSec!=null?tc(startSec):"0:00";
  const ev=endSec!=null?tc(endSec):"0:30";
  const row=document.createElement("div");
  row.className="seg-row";
  row.innerHTML=`<span class="seg-dot" style="background:${col.border}"></span>`+
    `<div class="tfield"><label>Start</label>`+
    `<input type="text" class="seg-start" value="${sv}" placeholder="0:00"></div>`+
    `<div class="tfield"><label>End</label>`+
    `<input type="text" class="seg-end" value="${ev}" placeholder="0:30"></div>`+
    `<button class="seg-del" title="Remove">✕</button>`;
  row.querySelector(".seg-del").addEventListener("click",()=>{
    if($("segList").children.length<=1) return;
    row.remove(); reindexSegs(); drawSegmentOverlays(); updateCutBtn();
  });
  row.querySelectorAll("input").forEach(inp=>
    inp.addEventListener("input",()=>{drawSegmentOverlays();updateCutBtn();})
  );
  $("segList").appendChild(row);
  updateCutBtn(); drawSegmentOverlays();
}

function reindexSegs(){
  [...$("segList").children].forEach((row,i)=>{
    const dot=row.querySelector(".seg-dot");
    if(dot) dot.style.background=SEG_COLORS[i%SEG_COLORS.length].border;
  });
}

function getSegments(){
  return [...$("segList").children].map(row=>({
    start:row.querySelector(".seg-start").value.trim(),
    end:row.querySelector(".seg-end").value.trim(),
  }));
}

function updateCutBtn(){
  if($("cut").disabled) return;
  const n=$("segList").children.length;
  $("cut").textContent=n===1?"Cut clip":`Cut ${n} clips`;
}

// ── Utils ──────────────────────────────────────────────────────────
function toSecs(v){
  v=(v||"").trim(); if(!v) return null;
  const p=v.split(":").map(Number);
  if(p.some(isNaN)) return null;
  if(p.length===1) return p[0];
  if(p.length===2) return p[0]*60+p[1];
  if(p.length===3) return p[0]*3600+p[1]*60+p[2];
  return null;
}
function tc(s){
  s=Math.max(0,Math.round(s||0));
  const h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;
  return [h,m,x].map(n=>String(n).padStart(2,"0")).join(":");
}
function detailLine(d){
  const b=[];
  if(d.speed) b.push((d.speed/1e6).toFixed(1)+" MB/s");
  if(d.eta!=null) b.push("ETA "+d.eta+"s");
  if(d.pct!=null) b.push(d.pct+"%");
  return b.join(" · ");
}

// ── Info fetch ─────────────────────────────────────────────────────
let infoTimer=null;
$("url").addEventListener("input",()=>{
  clearTimeout(infoTimer);
  $("err").classList.remove("show");
  infoTimer=setTimeout(fetchInfo,500);
});

async function fetchInfo(){
  const url=$("url").value.trim();
  if(!url){
    $("preview").classList.remove("show");
    $("timeline").classList.remove("show");
    $("peakBadge").classList.remove("show");
    $("suggestBar").classList.remove("show");
    $("suggestCards").classList.remove("show");
    heatmapData=null; return;
  }
  try{
    const r=await fetch("/info?url="+encodeURIComponent(url));
    const d=await r.json();
    if(d.error){showErr(d.error);return;}
    $("thumb").src=d.thumbnail||"";
    $("ptitle").textContent=d.title||"Untitled";
    duration=d.duration||null;
    $("psub").textContent=(d.uploader?d.uploader+" · ":"")+(duration?tc(duration):"");
    $("dur").textContent=duration?tc(duration):"--:--:--";
    $("preview").classList.add("show");
    $("timeline").classList.add("show");
    heatmapData=(d.heatmap&&d.heatmap.length)?d.heatmap:null;
    requestAnimationFrame(()=>{drawHeatmap();drawSegmentOverlays();});
    const peak=detectPeak(heatmapData,duration);
    if(peak){
      const first=$("segList").children[0];
      if(first){
        first.querySelector(".seg-start").value=tc(peak.start);
        first.querySelector(".seg-end").value=tc(peak.end);
      }
      $("peakInfo").textContent=tc(peak.start)+" → "+tc(peak.end);
      $("peakBadge").classList.add("show");
      drawSegmentOverlays();
    } else {
      $("peakBadge").classList.remove("show");
    }
    // show suggest bar + fetch transcript asynchronously
    $("suggestBar").classList.add("show");
    $("suggestStatus").textContent="";
    $("suggestCards").classList.remove("show");
    _cues=[];
    $("transcriptWrap").classList.remove("show");
    $("burnRow").classList.remove("show");
    $("transcriptList").innerHTML="";
    fetchTranscript(url);
  }catch(e){/* info is best-effort */}
}

window.addEventListener("load",()=>{checkGoogleStatus();checkTikTokStatus();});

window.addEventListener("resize",()=>{drawHeatmap();drawSegmentOverlays();});
$("addSeg").addEventListener("click",()=>makeSegRow());

// ── Bulk cut ───────────────────────────────────────────────────────
$("cut").addEventListener("click", startBulkClip);

async function startBulkClip(){
  const segs=getSegments();
  const url=$("url").value.trim();
  if(!url||!segs.length) return;
  $("err").classList.remove("show");
  $("results").innerHTML="";
  $("cut").disabled=true;
  $("cut").textContent=segs.length===1?"Cutting…":`Cutting ${segs.length}…`;

  const cards=segs.map((seg,i)=>{
    const col=SEG_COLORS[i%SEG_COLORS.length];
    const el=document.createElement("div");
    el.className="clip-card";
    el.innerHTML=
      `<div class="clip-label"><span class="clip-dot" style="background:${col.border}"></span>`+
      `Clip ${i+1} &nbsp;·&nbsp; ${seg.start} → ${seg.end}</div>`+
      `<div class="cbar indet"><i></i></div>`+
      `<div class="cphase">Starting…</div>`+
      `<div class="cdl-row">`+
        `<a class="cdl" href="#">Download</a>`+
        `<span class="cfname"></span>`+
      `</div>`+
      `<div class="push-row">`+
        `<button class="gdrive-btn" disabled>&#8593; Drive</button>`+
        `<button class="yt-btn" disabled>&#9654; YouTube</button>`+
        `<button class="tt-btn" disabled>&#9650; TikTok</button>`+
      `</div>`+
      `<div class="push-links"></div>`+
      `<video class="clip-preview" controls preload="none"></video>`+
      `<div class="cerr"></div>`;
    $("results").appendChild(el);
    return el;
  });

  const quality=$("quality").value, ratio=$("ratio").value;
  const burn_captions=$("burnCaptions").checked;
  await Promise.all(segs.map((seg,i)=>runOneClip(cards[i],{url,...seg,quality,ratio,burn_captions})));
  $("cut").disabled=false;
  updateCutBtn();
}

async function runOneClip(card, payload){
  try{
    const r=await fetch("/clip",{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
    const d=await r.json();
    if(d.error){clipErr(card,d.error);return;}
    await streamClip(card,d.job_id);
  }catch(e){clipErr(card,"Couldn't reach the server.");}
}

function streamClip(card,jobId){
  return new Promise(resolve=>{
    const bar=card.querySelector(".cbar");
    const fill=card.querySelector(".cbar>i");
    const phase=card.querySelector(".cphase");
    const es=new EventSource("/progress/"+jobId);
    es.onmessage=ev=>{
      const d=JSON.parse(ev.data);
      const dl=detailLine(d);
      if(d.phase==="downloading"){
        phase.textContent="Downloading"+(dl?" · "+dl:"");
        bar.classList.toggle("indet",d.pct==null);
        if(d.pct!=null) fill.style.width=d.pct+"%";
      } else if(d.phase==="processing"){phase.textContent="Processing";bar.classList.add("indet");}
      else if(d.phase==="merging")  {phase.textContent="Merging audio + video";bar.classList.add("indet");}
      else if(d.phase==="cropping") {phase.textContent="Cropping to ratio";bar.classList.add("indet");}
      else if(d.phase==="starting") {phase.textContent="Fetching stream info";bar.classList.add("indet");}
      else if(d.phase==="captions"){phase.textContent="Fetching captions…";bar.classList.add("indet");}
      else if(d.phase==="done"){
        es.close();fill.style.width="100%";bar.classList.remove("indet");
        phase.textContent="Done ✓";
        const dlRow=card.querySelector(".cdl-row");
        dlRow.querySelector(".cdl").href="/download/"+jobId;
        dlRow.querySelector(".cfname").textContent=d.name||"";
        dlRow.classList.add("show");
        const pushRow=card.querySelector(".push-row");
        const clipTitle=(d.name||"clip").replace(/\.[^.]+$/,"");
        const drvBtn=pushRow.querySelector(".gdrive-btn");
        if(drvBtn){drvBtn.disabled=false;drvBtn.addEventListener("click",()=>uploadToDrive(jobId,drvBtn,card));}
        const ytBtn=pushRow.querySelector(".yt-btn");
        if(ytBtn){ytBtn.disabled=false;ytBtn.addEventListener("click",()=>uploadToYouTube(jobId,clipTitle,ytBtn,card));}
        const ttBtn=pushRow.querySelector(".tt-btn");
        if(ttBtn){ttBtn.disabled=false;ttBtn.addEventListener("click",()=>uploadToTikTok(jobId,clipTitle,ttBtn,card));}
        const preview=card.querySelector(".clip-preview");
        preview.src="/stream/"+jobId;
        preview.classList.add("show");
        resolve();
      } else if(d.phase==="error"){
        es.close();clipErr(card,d.message||"Something went wrong.");resolve();
      }
    };
    es.onerror=()=>{es.close();resolve();};
  });
}

function clipErr(card,msg){
  card.querySelector(".cbar").style.display="none";
  card.querySelector(".cphase").style.display="none";
  const e=card.querySelector(".cerr");e.textContent=msg;e.classList.add("show");
}
function showErr(msg){$("err").textContent=msg;$("err").classList.add("show");}

// ── AI clip suggester ──────────────────────────────────────────────
$("suggestBtn").addEventListener("click", runSuggest);

async function runSuggest(){
  const url=$("url").value.trim();
  if(!url){showErr("Paste a video URL first.");return;}
  const plat=$("suggestPlat").value;
  $("suggestBtn").disabled=true;
  $("suggestStatus").textContent="Thinking…";
  $("suggestCards").innerHTML="";
  $("suggestCards").classList.remove("show");
  closePreviewPanel();
  try{
    const r=await fetch(`/suggest?url=${encodeURIComponent(url)}&platform=${encodeURIComponent(plat)}&n=10`);
    const d=await r.json();
    if(d.error){$("suggestStatus").textContent="Error: "+d.error;return;}
    $("suggestStatus").textContent=`${d.clips.length} suggestions for ${d.platform}`;
    renderSuggestions(d.clips);
    // sync preset dropdown to chosen platform
    const matchBtn=[...$("suggestPlat").closest(".card").querySelectorAll(".preset-btn")]
      .find(b=>b.textContent===plat);
    if(matchBtn) matchBtn.click();
  }catch(e){
    $("suggestStatus").textContent="Request failed.";
  }finally{
    $("suggestBtn").disabled=false;
  }
}

function renderSuggestions(clips){
  const box=$("suggestCards");
  box.innerHTML="";
  clips.forEach((c,i)=>{
    const div=document.createElement("div");
    div.className="scard";
    div.innerHTML=
      `<span class="scard-num">${i+1}</span>`+
      `<div class="scard-body">`+
        `<div class="scard-title">${c.title||"Clip "+(i+1)}</div>`+
        `<div class="scard-tc">${tc(c.start)} → ${tc(c.end)} &nbsp;·&nbsp; ${c.end-c.start}s</div>`+
        `<div class="scard-reason">${c.reason||""}</div>`+
      `</div>`+
      `<div class="scard-actions">`+
        `<button class="scard-preview">&#128065; Preview</button>`+
        `<span class="scard-add">+ Add</span>`+
      `</div>`;
    div.querySelector(".scard-preview").addEventListener("click",(e)=>{
      e.stopPropagation();
      openPreviewPanel(c, div);
    });
    div.querySelector(".scard-add").addEventListener("click",(e)=>{
      e.stopPropagation();
      makeSegRow(c.start,c.end);
      drawSegmentOverlays();updateCutBtn();
      div.style.borderColor="var(--in)";
      div.querySelector(".scard-add").textContent="✓ Added";
    });
    div.addEventListener("click",()=>openPreviewPanel(c, div));
    box.appendChild(div);
  });
  box.classList.add("show");
}

// ── Suggestion preview panel ───────────────────────────────────────
let _previewClip=null, _activePreviewCard=null;

function _ytVideoId(url){
  if(!url) return null;
  const m=url.match(/(?:v=|youtu\.be\/|\/embed\/|\/shorts\/)([A-Za-z0-9_-]{11})/);
  return m?m[1]:null;
}

function _loadPreviewEmbed(start, end){
  const embed=$("previewEmbed");
  embed.innerHTML="";
  const vid=_ytVideoId($("url").value.trim());
  if(vid){
    const iframe=document.createElement("iframe");
    iframe.src=`https://www.youtube.com/embed/${vid}?start=${Math.floor(start)}&end=${Math.ceil(end)}&rel=0`;
    iframe.allow="autoplay; encrypted-media; fullscreen";
    iframe.setAttribute("allowfullscreen","");
    embed.appendChild(iframe);
  } else {
    embed.innerHTML=`<div class="preview-fallback">`+
      `<span>&#9654; Preview unavailable for this platform</span>`+
      `<span style="font-size:11px;opacity:.7">Adjust timestamps below then add to segments</span>`+
      `</div>`;
  }
}

function _updatePreviewDur(){
  const s=toSecs($("previewStart").value)||0;
  const e=toSecs($("previewEnd").value)||0;
  $("previewDur").textContent=e>s?`${e-s}s`:"";
}

function openPreviewPanel(clip, cardEl){
  if(_activePreviewCard && _activePreviewCard!==cardEl)
    _activePreviewCard.querySelector(".scard-preview")?.classList.remove("active");
  _activePreviewCard=cardEl;
  cardEl.querySelector(".scard-preview")?.classList.add("active");
  _previewClip={...clip};
  $("previewTitle").textContent=clip.title||`${tc(clip.start)} → ${tc(clip.end)}`;
  $("previewStart").value=tc(clip.start);
  $("previewEnd").value=tc(clip.end);
  _updatePreviewDur();
  _loadPreviewEmbed(clip.start, clip.end);
  $("previewAddBtn").textContent="+ Add to segments";
  $("previewPanel").classList.add("show");
  $("previewPanel").scrollIntoView({behavior:"smooth",block:"nearest"});
}

function closePreviewPanel(){
  const embed=$("previewEmbed");
  const iframe=embed.querySelector("iframe");
  if(iframe) iframe.src="";   // stops playback
  embed.innerHTML="";
  $("previewPanel").classList.remove("show");
  if(_activePreviewCard){
    _activePreviewCard.querySelector(".scard-preview")?.classList.remove("active");
    _activePreviewCard=null;
  }
  _previewClip=null;
}

$("previewClose").addEventListener("click", closePreviewPanel);

["previewStart","previewEnd"].forEach(id=>{
  $( id).addEventListener("input", _updatePreviewDur);
  $(id).addEventListener("blur",()=>{
    // Reload iframe with adjusted timestamps so user can re-preview the trimmed segment
    const s=toSecs($("previewStart").value)||(_previewClip?_previewClip.start:0);
    const e=toSecs($("previewEnd").value)||(_previewClip?_previewClip.end:30);
    _loadPreviewEmbed(s, e);
  });
});

$("previewAddBtn").addEventListener("click",()=>{
  const s=toSecs($("previewStart").value)||(_previewClip?_previewClip.start:0);
  const e=toSecs($("previewEnd").value)||(_previewClip?_previewClip.end:30);
  makeSegRow(s, e);
  drawSegmentOverlays(); updateCutBtn();
  $("previewAddBtn").textContent="✓ Added";
  setTimeout(()=>{ $("previewAddBtn").textContent="+ Add to segments"; }, 2000);
});

// ── Platform presets ───────────────────────────────────────────────
let _activePreset=null;
document.querySelectorAll(".preset-btn").forEach(btn=>{
  btn.addEventListener("click",()=>{
    const isActive=btn.classList.contains("active");
    document.querySelectorAll(".preset-btn").forEach(b=>b.classList.remove("active"));
    if(isActive){_activePreset=null;return;}
    btn.classList.add("active");_activePreset=btn;
    $("ratio").value=btn.dataset.ratio;
    $("quality").value=btn.dataset.quality;
    const maxd=btn.dataset.maxdur?parseInt(btn.dataset.maxdur):null;
    if(maxd){
      [...$("segList").children].forEach(row=>{
        const s=toSecs(row.querySelector(".seg-start").value)||0;
        const e=toSecs(row.querySelector(".seg-end").value)||(s+maxd);
        if(e-s>maxd) row.querySelector(".seg-end").value=tc(Math.round(s+maxd));
      });
      drawSegmentOverlays();
    }
  });
});

// ── Transcript ─────────────────────────────────────────────────────
let _cues=[];

async function fetchTranscript(url){
  $("transcriptStatus").textContent="Loading…";
  try{
    const r=await fetch("/transcript?url="+encodeURIComponent(url));
    const d=await r.json();
    if(d.error||!d.cues||!d.cues.length){
      $("transcriptStatus").textContent="No transcript";
      return;
    }
    _cues=d.cues;
    $("transcriptStatus").textContent=_cues.length+" cues";
    $("transcriptWrap").classList.add("show");
    $("burnRow").classList.add("show");
    renderTranscript("");
  }catch(e){
    $("transcriptStatus").textContent="Unavailable";
  }
}

function renderTranscript(q){
  const list=$("transcriptList");
  list.innerHTML="";
  const lq=q.toLowerCase().trim();
  const src=lq?_cues.filter(c=>c.text.toLowerCase().includes(lq)):_cues;
  src.slice(0,150).forEach(c=>{
    const div=document.createElement("div");
    div.className="tcue";
    const t=tc(Math.floor(c.start));
    let txt=c.text;
    if(lq){
      const esc=lq.replace(/[.*+?^${}()|[\]\\]/g,"\\$&");
      txt=txt.replace(new RegExp(esc,"gi"),m=>`<mark>${m}</mark>`);
    }
    div.innerHTML=`<span class="ts">${t}</span><span class="txt">${txt}</span>`;
    div.addEventListener("click",()=>{
      const s=Math.max(0,c.start-1);
      const e=Math.min(duration||c.end+10,c.end+1);
      makeSegRow(Math.round(s),Math.round(e));
      drawSegmentOverlays();updateCutBtn();
    });
    list.appendChild(div);
  });
}

$("transcriptSearch").addEventListener("input",e=>renderTranscript(e.target.value));

// ── Google auth bar ────────────────────────────────────────────────
let _googleAuthed=false;

function _renderAuthConnected(channel){
  const area=$("driveArea");
  area.innerHTML=
    `<span class="auth-pill ok"><span class="picon">&#9782;</span> Drive</span>`+
    `<span class="auth-pill ok"><span class="picon">&#9654;</span> ${channel||"YouTube"}</span>`+
    `<button class="auth-reconnect" id="authReconnect">Reconnect</button>`;
  $("authReconnect").addEventListener("click",connectGoogle);
}

async function checkGoogleStatus(){
  try{
    const r=await fetch("/google/status");
    const d=await r.json();
    const area=$("driveArea");
    if(!d.libs){
      area.innerHTML='<span class="auth-pill warn">&#9888; Google libs missing — run: pip install google-api-python-client google-auth-oauthlib</span>';
    }else if(!d.creds){
      area.innerHTML='<span class="auth-pill warn">&#9888; Add credentials.json to app folder</span>';
    }else if(!d.authed){
      area.innerHTML='<button class="auth-connect-btn" id="authConnectBtn">Connect Google Drive &amp; YouTube</button>';
      $("authConnectBtn").addEventListener("click",connectGoogle);
    }else{
      _googleAuthed=true;
      _renderAuthConnected(d.channel);
    }
  }catch(e){}
}

async function connectGoogle(){
  try{
    const r=await fetch("/google/auth");
    const d=await r.json();
    if(d.error){alert(d.error);return;}
    window.open(d.auth_url,"_blank","width=520,height=660");
    $("driveArea").innerHTML='<span class="auth-pill">Waiting for Google sign-in…</span>';
    const poll=setInterval(async()=>{
      const r2=await fetch("/google/status");
      const d2=await r2.json();
      if(d2.authed){
        clearInterval(poll);
        _googleAuthed=true;
        _renderAuthConnected(d2.channel);
      }
    },2000);
    setTimeout(()=>clearInterval(poll),120000);
  }catch(e){}
}

async function uploadToDrive(jobId,btn,card){
  if(!_googleAuthed){connectGoogle();return;}
  btn.textContent="Uploading…";
  btn.classList.add("uploading");
  try{
    const r=await fetch("/gdrive/upload/"+jobId,{method:"POST"});
    const d=await r.json();
    if(d.error){
      btn.textContent="&#8593; Drive";btn.classList.remove("uploading");
      alert("Drive upload failed: "+d.error);return;
    }
    btn.textContent="&#8593; Saved";btn.classList.remove("uploading");
    const links=card.querySelector(".push-links");
    links.innerHTML+=`<a class="gdrive-link" href="${d.link}" target="_blank">&#8599; View on Drive</a>`;
  }catch(e){
    btn.textContent="&#8593; Drive";btn.classList.remove("uploading");
  }
}

async function uploadToYouTube(jobId,title,btn,card){
  if(!_googleAuthed){connectGoogle();return;}
  btn.textContent="Uploading…";
  btn.classList.add("uploading");
  try{
    const r=await fetch("/youtube/upload/"+jobId,{
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({title, privacy:"private"}),
    });
    const d=await r.json();
    if(d.error){
      btn.textContent="&#9654; YouTube";btn.classList.remove("uploading");
      alert("YouTube upload failed: "+d.error);return;
    }
    btn.textContent="&#9654; Published";btn.classList.remove("uploading");
    const links=card.querySelector(".push-links");
    links.innerHTML+=`<a class="yt-link" href="${d.url}" target="_blank">&#8599; View on YouTube (private)</a>`;
  }catch(e){
    btn.textContent="&#9654; YouTube";btn.classList.remove("uploading");
  }
}

// ── TikTok ─────────────────────────────────────────────────────────
let _tiktokAuthed=false;

function _renderTikTokConnected(username){
  const area=$("tiktokArea");
  area.innerHTML=
    `<span class="auth-pill ok"><span class="picon">&#9650;</span> ${username||"TikTok"}</span>`+
    `<button class="auth-reconnect" id="ttReconnect">Reconnect</button>`;
  $("ttReconnect").addEventListener("click",connectTikTok);
}

async function checkTikTokStatus(){
  try{
    const r=await fetch("/tiktok/status");
    const d=await r.json();
    const area=$("tiktokArea");
    if(!d.configured){
      area.innerHTML='<span class="auth-pill warn">&#9888; Add TIKTOK_CLIENT_KEY to .env</span>';
    }else if(!d.authed){
      area.innerHTML='<button class="auth-connect-btn" id="ttConnectBtn" style="background:linear-gradient(135deg,#010101 0%,#2a2a2a 100%);border-color:rgba(255,255,255,.15);color:#fff">Connect TikTok</button>';
      $("ttConnectBtn").addEventListener("click",connectTikTok);
    }else{
      _tiktokAuthed=true;
      _renderTikTokConnected(d.username);
    }
  }catch(e){}
}

async function connectTikTok(){
  try{
    const r=await fetch("/tiktok/auth");
    const d=await r.json();
    if(d.error){alert(d.error);return;}
    window.open(d.auth_url,"_blank","width=520,height=700");
    $("tiktokArea").innerHTML='<span class="auth-pill">Waiting for TikTok sign-in…</span>';
    const poll=setInterval(async()=>{
      const r2=await fetch("/tiktok/status");
      const d2=await r2.json();
      if(d2.authed){
        clearInterval(poll);
        _tiktokAuthed=true;
        _renderTikTokConnected(d2.username);
      }
    },2000);
    setTimeout(()=>clearInterval(poll),120000);
  }catch(e){}
}

async function uploadToTikTok(jobId,title,btn,card){
  if(!_tiktokAuthed){connectTikTok();return;}
  btn.textContent="Uploading…";
  btn.classList.add("uploading");
  try{
    const r=await fetch("/tiktok/upload/"+jobId,{
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({title}),
    });
    const d=await r.json();
    if(d.error){
      btn.textContent="&#9650; TikTok";btn.classList.remove("uploading");
      alert("TikTok upload failed: "+d.error);return;
    }
    btn.textContent="&#9650; Sent";btn.classList.remove("uploading");
    const links=card.querySelector(".push-links");
    links.innerHTML+=`<span class="tt-note" style="font-size:11px;color:#69c9d0;font-family:'Space Mono',monospace">&#10003; In TikTok inbox — open TikTok app to review &amp; post</span>`;
  }catch(e){
    btn.textContent="&#9650; TikTok";btn.classList.remove("uploading");
  }
}

// ── Menu bar / pages ────────────────────────────────────────────────
document.querySelectorAll(".menu-tab").forEach(btn=>{
  btn.addEventListener("click",()=>{
    document.querySelectorAll(".menu-tab").forEach(b=>b.classList.remove("active"));
    document.querySelectorAll(".page").forEach(p=>p.classList.remove("active"));
    btn.classList.add("active");
    $("page-"+btn.dataset.page).classList.add("active");
  });
});

// ── Sub-nav (Video to Script / Video to Edit) ───────────────────────
document.querySelectorAll(".subnav-tab").forEach(btn=>{
  btn.addEventListener("click",()=>{
    document.querySelectorAll(".subnav-tab").forEach(b=>b.classList.remove("active"));
    document.querySelectorAll(".video-section").forEach(s=>s.classList.remove("active"));
    btn.classList.add("active");
    $("section-"+btn.dataset.section).classList.add("active");
  });
});

// ── Video page: load (Script + Edit share this URL) ─────────────────
let _scriptCues=[], _cleanedScript=null, _chaptersData=null, _knownChapters=[], _scriptView="raw";

let videoInfoTimer=null;
$("videoUrl").addEventListener("input",()=>{
  clearTimeout(videoInfoTimer);
  videoInfoTimer=setTimeout(loadVideoTab,500);
});
$("videoFetchBtn").addEventListener("click", loadVideoTab);
$("videoUrl").addEventListener("keydown", e=>{ if(e.key==="Enter") loadVideoTab(); });

async function loadVideoTab(){
  const url=$("videoUrl").value.trim();

  _scriptCues=[];
  _cleanedScript=null;
  _chaptersData=null;
  _knownChapters=[];
  _scriptView="raw";
  $("scriptSummary").classList.remove("show");
  $("scriptSummary").textContent="";
  $("scriptViewToggle").classList.remove("show");
  $("viewCleanBtn").style.display="none";
  $("viewChaptersBtn").style.display="none";
  document.querySelectorAll("#scriptViewToggle .preset-btn").forEach(b=>b.classList.toggle("active",b.dataset.view==="raw"));
  $("scriptTsToggleWrap").style.display="flex";
  $("scriptText").value="";
  $("scriptStatus").textContent="";
  updateScriptActionBtns();

  if(!url){
    $("videoPreview").classList.remove("show");
    return;
  }

  $("scriptStatus").textContent="Loading…";
  try{
    const [infoR, transR] = await Promise.all([
      fetch("/info?url="+encodeURIComponent(url)),
      fetch("/transcript?url="+encodeURIComponent(url)),
    ]);
    const info=await infoR.json();
    const trans=await transR.json();

    if(!info.error){
      $("videoThumb").src=info.thumbnail||"";
      $("videoTitle").textContent=info.title||"Untitled";
      $("videoSub").textContent=(info.uploader?info.uploader+" · ":"")+(info.duration?tc(info.duration):"");
      $("videoPreview").classList.add("show");
      _knownChapters=(info.chapters||[]).map(c=>({start:Math.round(c.start_time||0), title:c.title||""}));
    }

    if(trans.error || !trans.cues || !trans.cues.length){
      $("scriptStatus").textContent="No captions/transcript available for this video.";
      return;
    }
    _scriptCues=trans.cues;
    const total=_scriptCues[_scriptCues.length-1].end;
    $("scriptStatus").textContent=`${_scriptCues.length} caption cues · ${tc(total)} total`;
    renderScript();
  }catch(e){
    $("scriptStatus").textContent="Request failed — check the server console.";
  }finally{
    updateScriptActionBtns();
  }
}

// ── Download (full video, no cutting) ───────────────────────────────
$("dlBtn").addEventListener("click", startVideoDownload);

async function startVideoDownload(){
  const url=$("videoUrl").value.trim();
  if(!url){ $("scriptStatus").textContent="Paste a video URL first."; return; }
  const quality=$("dlQuality").value;

  $("dlBtn").disabled=true;
  $("dlCard").style.display="block";
  $("dlBar").style.display="block";
  $("dlPhase").style.display="block";
  $("dlBar").classList.add("indet");
  $("dlBar").querySelector("i").style.width="0%";
  $("dlPhase").textContent="Starting…";
  $("dlRow").classList.remove("show");
  $("dlErr").classList.remove("show");
  $("dlErr").textContent="";

  try{
    const r=await fetch("/download_video",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({url, quality})});
    const d=await r.json();
    if(d.error){ dlErr(d.error); return; }
    await streamDownloadJob(d.job_id);
  }catch(e){
    dlErr("Couldn't reach the server.");
  }finally{
    $("dlBtn").disabled=false;
  }
}

function dlErr(msg){
  $("dlBar").style.display="none";
  $("dlPhase").style.display="none";
  $("dlErr").textContent=msg;
  $("dlErr").classList.add("show");
}

function streamDownloadJob(jobId){
  return new Promise(resolve=>{
    const bar=$("dlBar"), fill=bar.querySelector("i"), phase=$("dlPhase");
    const es=new EventSource("/progress/"+jobId);
    es.onmessage=ev=>{
      const d=JSON.parse(ev.data);
      const dl=detailLine(d);
      if(d.phase==="downloading"){
        phase.textContent="Downloading"+(dl?" · "+dl:"");
        bar.classList.toggle("indet", d.pct==null);
        if(d.pct!=null) fill.style.width=d.pct+"%";
      } else if(d.phase==="merging"){ phase.textContent="Merging audio + video"; bar.classList.add("indet"); }
      else if(d.phase==="starting"){ phase.textContent="Fetching stream info"; bar.classList.add("indet"); }
      else if(d.phase==="done"){
        es.close(); fill.style.width="100%"; bar.classList.remove("indet");
        phase.textContent="Done ✓";
        $("dlLink").href="/download/"+jobId;
        $("dlFname").textContent=d.name||"";
        $("dlRow").classList.add("show");
        resolve();
      } else if(d.phase==="error"){
        es.close(); dlErr(d.message||"Something went wrong."); resolve();
      }
    };
    es.onerror=()=>{es.close();resolve();};
  });
}

// ── Script (Raw / AI cleaned / Chapters) ────────────────────────────
$("scriptTsToggle").addEventListener("change", renderScript);
$("scriptCopyBtn").addEventListener("click", copyScript);
$("scriptDownloadBtn").addEventListener("click", downloadScript);
$("scriptCleanBtn").addEventListener("click", cleanScriptWithAI);
$("scriptChaptersBtn").addEventListener("click", generateChapters);
document.querySelectorAll("#scriptViewToggle .preset-btn").forEach(btn=>{
  btn.addEventListener("click",()=>setScriptView(btn.dataset.view));
});

function renderScript(){
  if(!_scriptCues.length){ $("scriptText").value=""; return; }
  if($("scriptTsToggle").checked){
    $("scriptText").value=_scriptCues.map(c=>`[${tc(Math.floor(c.start))}] ${c.text}`).join("\n");
  } else {
    // Flowing paragraphs — break where a gap of 3s+ between cues suggests a pause/scene change
    const paras=[]; let para=[]; let prevEnd=null;
    _scriptCues.forEach(c=>{
      if(prevEnd!=null && c.start-prevEnd>3 && para.length){
        paras.push(para.join(" ")); para=[];
      }
      para.push(c.text); prevEnd=c.end;
    });
    if(para.length) paras.push(para.join(" "));
    $("scriptText").value=paras.join("\n\n");
  }
}

function updateScriptActionBtns(){
  const has=_scriptCues.length>0;
  $("scriptCopyBtn").disabled=!has;
  $("scriptDownloadBtn").disabled=!has;
  $("scriptCleanBtn").disabled=!has;
  $("scriptChaptersBtn").disabled=!has;
}

function chaptersToText(chapters){
  return chapters.map(ch=>`# ${tc(ch.start)} — ${ch.title}\n\n${ch.text}`).join("\n\n\n");
}

function setScriptView(view){
  _scriptView=view;
  document.querySelectorAll("#scriptViewToggle .preset-btn").forEach(b=>
    b.classList.toggle("active", b.dataset.view===view));
  $("scriptTsToggleWrap").style.display = view==="raw" ? "flex" : "none";
  if(view==="clean"){
    $("scriptText").value=_cleanedScript||"";
  } else if(view==="chapters"){
    $("scriptText").value=chaptersToText(_chaptersData||[]);
  } else {
    renderScript();
  }
}

async function cleanScriptWithAI(){
  if(!_scriptCues.length) return;
  $("scriptCleanBtn").disabled=true;
  $("scriptCleanBtn").textContent="Cleaning…";
  try{
    const r=await fetch("/script/clean",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({cues:_scriptCues, title:$("videoTitle").textContent||""})});
    const d=await r.json();
    if(d.error){ $("scriptStatus").textContent="AI cleanup failed: "+d.error; return; }
    _cleanedScript=d.script||"";
    if(d.summary){
      $("scriptSummary").textContent=d.summary;
      $("scriptSummary").classList.add("show");
    }
    $("scriptViewToggle").classList.add("show");
    $("viewCleanBtn").style.display="inline-block";
    setScriptView("clean");
  }catch(e){
    $("scriptStatus").textContent="AI cleanup request failed.";
  }finally{
    $("scriptCleanBtn").disabled=false;
    $("scriptCleanBtn").textContent="✨ Clean up with AI";
  }
}

async function generateChapters(){
  if(!_scriptCues.length) return;
  $("scriptChaptersBtn").disabled=true;
  $("scriptChaptersBtn").textContent="Generating…";
  try{
    const r=await fetch("/script/chapters",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({cues:_scriptCues, title:$("videoTitle").textContent||"", known_chapters:_knownChapters})});
    const d=await r.json();
    if(d.error){ $("scriptStatus").textContent="Chapter generation failed: "+d.error; return; }
    _chaptersData=d.chapters||[];
    $("scriptViewToggle").classList.add("show");
    $("viewChaptersBtn").style.display="inline-block";
    setScriptView("chapters");
  }catch(e){
    $("scriptStatus").textContent="Chapter generation request failed.";
  }finally{
    $("scriptChaptersBtn").disabled=false;
    $("scriptChaptersBtn").textContent="🔖 Chapters";
  }
}

async function copyScript(){
  try{
    await navigator.clipboard.writeText($("scriptText").value);
  }catch(e){
    $("scriptText").select();
    document.execCommand("copy");
  }
  $("scriptCopyBtn").textContent="Copied ✓";
  setTimeout(()=>{ $("scriptCopyBtn").textContent="Copy"; },1500);
}

function downloadScript(){
  const blob=new Blob([$("scriptText").value],{type:"text/plain"});
  const a=document.createElement("a");
  a.href=URL.createObjectURL(blob);
  const base=($("videoTitle").textContent||"script").replace(/[^\w\- ]+/g,"").trim().replace(/\s+/g,"_");
  a.download=(base||"script").slice(0,80)+".txt";
  a.click();
  URL.revokeObjectURL(a.href);
}

// ── Init ───────────────────────────────────────────────────────────
makeSegRow(0,30);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    if not shutil.which("ffmpeg"):
        print("\n  WARNING: ffmpeg was not found on PATH.")
        print("  Install it first: brew install ffmpeg  /  apt install ffmpeg  /  choco install ffmpeg\n")
    if _js_runtime:
        print(f"  JS runtime : {list(_js_runtime.keys())[0]}")
    else:
        print("  WARNING: No JS runtime found. Install nodejs: sudo apt install nodejs")
    if os.path.exists(COOKIES_FILE):
        print(f"  Cookies   : {COOKIES_FILE}  ✓")
    else:
        print(f"  Cookies   : not found — heatmap unavailable without cookies.txt")
        print(f"  Export from Firefox: install 'cookies.txt' extension → export → save as {COOKIES_FILE}")
    import socket
    local_ip = socket.gethostbyname(socket.gethostname())
    print(f"  Local :  http://127.0.0.1:5000")
    print(f"  Network: http://{local_ip}:5000\n")
    app.run(host="0.0.0.0", port=5000, threaded=True, debug=False)
