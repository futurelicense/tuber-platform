#!/usr/bin/env python3
"""YT Production — Script → Audio → User Media Sections → Video"""

import os, sys, re, json, math, time, uuid, queue, shutil, asyncio
import threading, traceback, subprocess
import requests as _requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Ensure user site-packages visible when running as root
_user_site = os.path.expanduser(
    "~emc2/.local/lib/python{}.{}/site-packages".format(*sys.version_info[:2])
)
if os.path.isdir(_user_site) and _user_site not in sys.path:
    sys.path.insert(0, _user_site)

from flask import Flask, request, Response, send_file, jsonify

# ── .env loader ──────────────────────────────────────────────────────
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _ef:
        for _line in _ef:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip())

try:
    import edge_tts
    _EDGE_TTS = True
except ImportError:
    _EDGE_TTS = False

try:
    from PIL import Image, ImageDraw, ImageFont
    _PIL = True
except ImportError:
    _PIL = False

APP_DIR    = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.environ.get("YTPROD_OUTPUT_DIR") or os.path.join(APP_DIR, "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)
print(f"  [ytprod] OUTPUT_DIR={OUTPUT_DIR}", flush=True)

_AI_ENDPOINT = os.environ.get("AI_ENDPOINT", "https://api.groq.com/openai/v1/chat/completions")
_DEFAULT_AI_MODEL = "openai/gpt-oss-120b"
# Groq free/developer keys no longer serve these — they 404 model_not_found
# (llama-3.x is Enterprise-only). Remap at call time so a stale Render
# AI_MODEL=llama-3.1-8b-instant does not break jobs after redeploy.
_RETIRED_GROQ_MODELS = frozenset({
    "llama-3.1-8b-instant",
    "llama-3.3-70b-versatile",
    "llama-3.1-70b-versatile",
    "llama3-8b-8192",
    "llama3-70b-8192",
})


def _resolve_ai_model() -> str:
    model = (os.environ.get("AI_MODEL") or _DEFAULT_AI_MODEL).strip()
    if not model or model in _RETIRED_GROQ_MODELS:
        return _DEFAULT_AI_MODEL
    return model


# See vendor/youtube-clipper/app.py's identical comment: gpt-oss reasoning
# models can burn max_tokens on a hidden <think> block before the real
# answer, so pin low effort + hidden reasoning on Groq specifically.
_GROQ_REASONING_KWARGS = (
    {"reasoning_effort": "low", "reasoning_format": "hidden"}
    if "groq.com" in _AI_ENDPOINT else {}
)

JOBS: dict = {}
app = Flask(__name__)

# ─── helpers ─────────────────────────────────────────────────────────

def _push(q, data: dict):
    q.put(json.dumps(data))


def _iter_sse(q, heartbeat: float = 15.0):
    """SSE from a job queue, with comment keepalives while the queue is idle.

    Narration and ffmpeg often go minutes without a status event. A proxy
    that sees a silent response closes it, and the page used to treat that
    drop as a failed job even though work was still running.
    """
    while True:
        try:
            item = q.get(timeout=heartbeat)
        except queue.Empty:
            # Pad past typical proxy buffers so the comment is actually flushed.
            # A 2-byte comment can sit unsent while the idle timer still fires.
            yield ": keepalive " + (" " * 2048) + "\n\n"
            continue
        if item is None:
            break
        yield f"data: {item}\n\n"


_JOB_PERSIST_KEYS = (
    "status", "script", "voice", "title", "description", "hashtags",
    "sections", "chapters", "error_message", "xfade", "music_vol",
    "clip_prefill", "voice_rate", "voice_pitch",
    "viz_style", "viz_placement", "viz_opacity",
)


_VOICE_RE = re.compile(r"^[a-z]{2,3}-[A-Z]{2}-[A-Za-z]+Neural$")
_RATE_RE  = re.compile(r"^[+-]\d{1,2}%$")
_PITCH_RE = re.compile(r"^[+-]\d{1,2}Hz$")


def _clean_prosody(rate, pitch) -> dict:
    """edge-tts rate ("+10%") and pitch ("-5Hz"); anything malformed → neutral."""
    rate = str(rate or "").strip()
    pitch = str(pitch or "").strip()
    return {
        "voice_rate": rate if _RATE_RE.match(rate) else "+0%",
        "voice_pitch": pitch if _PITCH_RE.match(pitch) else "+0Hz",
    }


def _tts_prosody(job: dict) -> dict:
    p = _clean_prosody(job.get("voice_rate"), job.get("voice_pitch"))
    return {"rate": p["voice_rate"], "pitch": p["voice_pitch"]}


def _job_dir(job_id: str) -> str:
    return os.path.join(OUTPUT_DIR, job_id)


def _job_state_path(job_id: str) -> str:
    return os.path.join(_job_dir(job_id), "state.json")


def _find_section_media(job_dir: str, idx: int):
    """Locate section media on disk (manual upload or auto-extract)."""
    if not os.path.isdir(job_dir):
        return None, None
    # Prefer explicit upload name, then auto-extract, then any section_NNN* match.
    preferred = []
    others = []
    try:
        names = os.listdir(job_dir)
    except OSError:
        return None, None
    for name in names:
        m = re.match(rf"section_{idx:03d}(_auto)?(\.[A-Za-z0-9]+)$", name)
        if not m:
            continue
        path = os.path.join(job_dir, name)
        if not os.path.isfile(path):
            continue
        ext = m.group(2).lower()
        if m.group(1):
            others.append((path, ext))
        else:
            preferred.append((path, ext))
    if preferred:
        return preferred[0]
    if others:
        return others[0]
    return None, None


def _find_narration(job_dir: str):
    """Return path to narration audio in job_dir, if any."""
    if not os.path.isdir(job_dir):
        return None
    for name in ("narration.mp3", "narration.wav", "narration.m4a", "narration.aac"):
        path = os.path.join(job_dir, name)
        if os.path.isfile(path):
            return path
    try:
        for name in os.listdir(job_dir):
            low = name.lower()
            if low.startswith("narration.") or low.startswith("_upload_narration"):
                path = os.path.join(job_dir, name)
                if os.path.isfile(path):
                    return path
    except OSError:
        pass
    return None


def _find_music(job_dir: str):
    if not os.path.isdir(job_dir):
        return None
    try:
        for name in os.listdir(job_dir):
            if name.startswith("music.") and os.path.isfile(os.path.join(job_dir, name)):
                return os.path.join(job_dir, name)
    except OSError:
        pass
    return None


def _rebuild_job_from_disk(job_id: str, sections_payload=None):
    """Last-resort job recovery for assemble/upload after worker recycle.

    Uses narration + section_* files on disk, optionally guided by the
    browser's section timings from the assemble POST body.
    """
    if not job_id or not re.fullmatch(r"[0-9a-fA-F]{32}", job_id):
        return None
    job_id = job_id.lower()
    job_dir = _job_dir(job_id)
    audio_path = _find_narration(job_dir)
    if not audio_path:
        print(f"  [ytprod] rebuild failed — no narration in {job_dir} "
              f"(OUTPUT_DIR={OUTPUT_DIR})", flush=True)
        return None

    actual_dur = _audio_duration(audio_path) or 1.0
    sections = []

    if sections_payload:
        # Preserve the producer's in-browser section count/timings.
        by_idx = {}
        for cs in sections_payload:
            try:
                idx = int(cs.get("idx", -1))
            except (TypeError, ValueError):
                continue
            if idx < 0:
                continue
            by_idx[idx] = cs
        if by_idx:
            n = max(by_idx) + 1
            n = max(2, min(20, n))
            # Start from equal slices, then overlay client timings.
            sections = _equal_sections(n, actual_dur)
            for idx, cs in by_idx.items():
                if idx >= len(sections):
                    continue
                try:
                    start = float(cs.get("start", sections[idx]["start"]))
                    end = float(cs.get("end", sections[idx]["end"]))
                except (TypeError, ValueError):
                    continue
                sections[idx]["start"] = max(0.0, start)
                sections[idx]["end"] = max(sections[idx]["start"] + 0.5, end)
                if isinstance(cs.get("text"), str) and cs["text"].strip():
                    sections[idx]["text"] = cs["text"].strip()

    if not sections:
        media_idxs = set()
        try:
            for name in os.listdir(job_dir):
                m = re.match(r"section_(\d+)", name)
                if m:
                    media_idxs.add(int(m.group(1)))
        except OSError:
            pass
        n = max(media_idxs) + 1 if media_idxs else 6
        n = max(2, min(20, n))
        sections = _equal_sections(n, actual_dur)

    for s in sections:
        media, ext = _find_section_media(job_dir, s["idx"])
        if media:
            s["media"] = media
            s["media_ext"] = ext

    video_path = os.path.join(job_dir, "video.mp4")
    if not os.path.isfile(video_path):
        video_path = None

    job = {
        "status": "audio_ready",
        "queue": queue.Queue(),
        "script": "",
        "voice": "en-US-GuyNeural",
        "title": "My Video",
        "description": "",
        "hashtags": [],
        "sections": sections,
        "audio_path": audio_path,
        "video_path": video_path,
        "music_path": _find_music(job_dir),
        "chapters": None,
        "error_message": None,
        "xfade": True,
        "music_vol": 0.15,
        "clip_prefill": None,
    }
    JOBS[job_id] = job
    _persist_job(job_id)
    print(f"  [ytprod] rebuilt job {job_id} from disk "
          f"({len(sections)} sections, audio={os.path.basename(audio_path)})",
          flush=True)
    return job


def _persist_job(job_id: str) -> None:
    """Write a JSON snapshot so /produce can resume after a browser refresh
    (and after a process restart, as long as OUTPUT_DIR is intact).
    Queues are omitted — they are rebuilt empty on hydrate.
    """
    job = JOBS.get(job_id)
    if not job:
        return
    job_dir = _job_dir(job_id)
    os.makedirs(job_dir, exist_ok=True)

    def _rel(path):
        if not path:
            return None
        # Prefer basename so the snapshot is portable within OUTPUT_DIR.
        if os.path.isabs(path) and path.startswith(job_dir + os.sep):
            return os.path.basename(path)
        return os.path.basename(path) if os.path.dirname(path) == job_dir else path

    sections = []
    for s in job.get("sections") or []:
        sec = dict(s)
        if sec.get("media"):
            sec["media"] = _rel(sec["media"])
        sections.append(sec)

    payload = {k: job.get(k) for k in _JOB_PERSIST_KEYS}
    payload["sections"] = sections
    payload["audio_file"] = _rel(job.get("audio_path")) or (
        "narration.mp3" if os.path.exists(os.path.join(job_dir, "narration.mp3")) else None
    )
    payload["video_file"] = _rel(job.get("video_path")) or (
        "video.mp4" if os.path.exists(os.path.join(job_dir, "video.mp4")) else None
    )
    payload["music_file"] = _rel(job.get("music_path"))
    try:
        with open(_job_state_path(job_id), "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"  [ytprod] persist failed for {job_id}: {e}", flush=True)


def _hydrate_job(job_id: str):
    """Load a previously persisted job into JOBS. Returns the job dict or None."""
    if not job_id or not re.fullmatch(r"[0-9a-fA-F]{32}", job_id):
        return None
    job_id = job_id.lower()
    path = _job_state_path(job_id)
    job_dir = _job_dir(job_id)

    data = None
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as e:
            print(f"  [ytprod] hydrate failed for {job_id}: {e}", flush=True)

    def _abs(name):
        if not name:
            return None
        if os.path.isabs(name):
            return name if os.path.exists(name) else None
        cand = os.path.join(job_dir, name)
        return cand if os.path.exists(cand) else None

    # No state.json — still recover if narration exists so uploads/assemble
    # keep working after a worker recycle (audio URL can succeed without state).
    if data is None:
        return _rebuild_job_from_disk(job_id)

    sections = []
    for i, s in enumerate(data.get("sections") or []):
        sec = dict(s)
        idx = int(sec.get("idx", i))
        sec["idx"] = idx
        media = _abs(sec.get("media"))
        if not media:
            media, ext = _find_section_media(job_dir, idx)
            if media:
                sec["media_ext"] = ext
        if media:
            sec["media"] = media
            if not sec.get("media_ext"):
                sec["media_ext"] = os.path.splitext(media)[1].lower()
        else:
            sec["media"] = None
        sections.append(sec)

    audio_path = (
        _abs(data.get("audio_file"))
        or _find_narration(job_dir)
    )
    video_path = _abs(data.get("video_file")) or _abs("video.mp4")
    music_path = _abs(data.get("music_file")) or _find_music(job_dir)

    status = data.get("status") or "audio_ready"
    # A hydrated job never has a live SSE queue from the prior process —
    # treat mid-flight statuses as the last durable checkpoint.
    if status in ("running", "assembling"):
        status = "audio_ready" if audio_path else "error"

    if not sections and audio_path:
        actual_dur = _audio_duration(audio_path) or 1.0
        sections = _equal_sections(6, actual_dur)
        for s in sections:
            media, ext = _find_section_media(job_dir, s["idx"])
            if media:
                s["media"] = media
                s["media_ext"] = ext

    if not audio_path:
        # state.json alone isn't enough to assemble — try full disk rebuild.
        return _rebuild_job_from_disk(job_id)

    job = {
        "status": status if status != "error" else ("audio_ready" if audio_path else "error"),
        "queue": queue.Queue(),
        "script": data.get("script") or "",
        "voice": data.get("voice") or "en-US-GuyNeural",
        "title": data.get("title"),
        "description": data.get("description"),
        "hashtags": data.get("hashtags") or [],
        "sections": sections,
        "audio_path": audio_path,
        "video_path": video_path,
        "music_path": music_path,
        "chapters": data.get("chapters"),
        "error_message": data.get("error_message"),
        "xfade": data.get("xfade", True),
        "music_vol": data.get("music_vol", 0.15),
        "clip_prefill": data.get("clip_prefill"),
        "voice_rate": data.get("voice_rate") or "+0%",
        "voice_pitch": data.get("voice_pitch") or "+0Hz",
        "viz_style": data.get("viz_style") or "none",
        "viz_placement": data.get("viz_placement") or "full",
        "viz_opacity": data.get("viz_opacity", 0.9),
    }
    # If we only had an early "running" stub, promote once narration exists.
    if job["status"] not in ("audio_ready", "done", "error_assembly") and audio_path:
        job["status"] = "audio_ready"
    JOBS[job_id] = job
    print(f"  [ytprod] hydrated job {job_id} ({job['status']})", flush=True)
    return job


def _get_job(job_id: str, sections_hint=None):
    """In-memory job, hydrate from state.json, or rebuild from disk artifacts."""
    if not job_id:
        return None
    job_id = job_id.strip().lower()
    job = JOBS.get(job_id)
    if job:
        return job
    job = _hydrate_job(job_id)
    if job:
        return job
    return _rebuild_job_from_disk(job_id, sections_hint)


def _ensure_section_slot(job: dict, idx: int, audio_dur: float | None = None) -> None:
    """Grow job['sections'] so idx is valid (upload to §N must not no-op)."""
    sections = job.setdefault("sections", [])
    if idx < len(sections):
        return
    if audio_dur is None:
        ap = job.get("audio_path")
        audio_dur = _audio_duration(ap) if ap and os.path.exists(ap) else 0.0
    audio_dur = max(float(audio_dur or 0), 1.0)
    while len(sections) <= idx:
        i = len(sections)
        # Append a short slot at the end; user can retune times in the UI.
        start = sections[-1]["end"] if sections else 0.0
        end = min(audio_dur, start + max(audio_dur / max(idx + 1, 1), 2.0))
        if end <= start:
            end = audio_dur
        sections.append({
            "idx": i,
            "text": f"Section {i + 1}",
            "start": round(start, 2),
            "end": round(end, 2),
            "media": None,
            "media_ext": None,
        })
    sections[-1]["end"] = round(audio_dur, 2)
    for i, s in enumerate(sections):
        s["idx"] = i

def _make_groq_session() -> _requests.Session:
    s = _requests.Session()
    # Retry on connection errors and 5xx; backoff 1s, 2s, 4s
    retry = Retry(total=4, backoff_factor=1,
                  status_forcelist=[500, 502, 503, 504],
                  allowed_methods=["POST"],
                  raise_on_status=False)
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s

_groq_session: _requests.Session = _make_groq_session()

def _groq(prompt: str, system: str = "", max_tokens: int = 3000) -> str:
    ai_key = os.environ.get("AI_KEY", "").strip()
    if not ai_key:
        raise RuntimeError("Set AI_KEY in .env")
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    payload = {"model": _resolve_ai_model(), "messages": msgs,
               "max_tokens": max_tokens, "temperature": 0.4,
               **_GROQ_REASONING_KWARGS}
    headers = {"Authorization": f"Bearer {ai_key}",
               "User-Agent": "Mozilla/5.0 (compatible; YTProd/1.0)"}
    last_err = None
    for attempt in range(4):
        if attempt:
            wait = 2 ** attempt
            print(f"  [ytprod] Groq retry {attempt}/3 in {wait}s…", flush=True)
            time.sleep(wait)
        try:
            r = _groq_session.post(_AI_ENDPOINT, json=payload,
                                   headers=headers, timeout=90)
            if r.status_code in (413, 429):
                # Free-tier tokens-per-minute exhaustion — e.g. this call
                # landing right after the chapter calls spent the window.
                # Wait for the minute to refill instead of failing the job.
                last_err = RuntimeError(f"Groq API {r.status_code}: {r.text[:300]}")
                print(f"  [ytprod] Groq rate-limited ({r.status_code}); waiting for the "
                      f"TPM window to refill…", flush=True)
                time.sleep(20)
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"Groq API {r.status_code}: {r.text[:300]}")
            return r.json()["choices"][0]["message"]["content"].strip()
        except RuntimeError:
            raise
        except Exception as e:
            last_err = e
            print(f"  [ytprod] Groq error (attempt {attempt+1}): {e}", flush=True)

    # Primary provider exhausted — one shot at the Hugging Face router before
    # failing the job. HF free credits are too small to be a workhorse; this
    # is strictly an overflow valve, active only when HF_API_KEY is set.
    hf_key = os.environ.get("HF_API_KEY", "").strip()
    if hf_key and hf_key != ai_key:
        fb_model = os.environ.get("FALLBACK_AI_MODEL", "meta-llama/Llama-3.3-70B-Instruct")
        fb_endpoint = os.environ.get(
            "FALLBACK_AI_ENDPOINT", "https://router.huggingface.co/v1/chat/completions")
        try:
            r = _groq_session.post(
                fb_endpoint, json={**payload, "model": fb_model},
                headers={"Authorization": f"Bearer {hf_key}",
                         "User-Agent": headers["User-Agent"]},
                timeout=120)
            if r.status_code < 400:
                print(f"  [ytprod] primary AI exhausted — served by fallback ({fb_model})",
                      flush=True)
                return r.json()["choices"][0]["message"]["content"].strip()
            print(f"  [ytprod] fallback AI {r.status_code}: {r.text[:200]}", flush=True)
        except Exception as e:
            print(f"  [ytprod] fallback AI failed too: {e}", flush=True)
    raise RuntimeError(f"Groq API unreachable after 4 attempts: {last_err}")

def _parse_meta_block(content: str):
    """Extract TITLE/DESCRIPTION/HASHTAGS from a plain-text ### META block.

    Deliberately not JSON: a 150-250 word AI-written description routinely
    contains quotes, apostrophes, and colons that a low-effort reasoning
    model fails to escape correctly, breaking json.loads in ways no generic
    repair can reliably undo. Same rationale as clipper's _parse_chapter_blocks
    for chapter bodies — free text goes in a plain-text field, not a JSON string.
    """
    m = re.search(
        r"TITLE:\s*(.*?)\s*"
        r"DESCRIPTION:\s*(.*?)\s*"
        r"HASHTAGS:\s*(.*?)\s*(?:###|\Z)",
        content, re.DOTALL | re.IGNORECASE,
    )
    if not m:
        raise ValueError(f"Could not parse AI metadata: {content[:300]}")
    title = m.group(1).strip().split("\n", 1)[0].strip()
    description = m.group(2).strip()
    hashtags = [h.strip().lstrip("#") for h in re.split(r"[,\n]+", m.group(3)) if h.strip()]
    return title, description, hashtags


def _word_windows(words: list, n: int) -> list:
    """Contiguous word slices of length n, covering every word in order."""
    n = max(1, int(n))
    if not words:
        return [f"Section {i + 1}" for i in range(n)]
    if len(words) < n:
        return [words[min(i, len(words) - 1)] for i in range(n)]
    base, rem = divmod(len(words), n)
    out = []
    idx = 0
    for i in range(n):
        take = base + (1 if i < rem else 0)
        out.append(" ".join(words[idx:idx + take]))
        idx += take
    return out


def _pack_to_n(units: list, n: int) -> list:
    """Pack consecutive text units into exactly n sections, balanced by words."""
    n = max(1, int(n))
    units = [u.strip() for u in units if u and u.strip()]
    if len(units) < n:
        return _word_windows(" ".join(units).split(), n)
    if len(units) == n:
        return units
    weights = [max(len(u.split()), 1) for u in units]
    target = sum(weights) / n
    buckets: list = []
    cur: list = []
    cur_w = 0
    buckets_left = n
    for i, (unit, weight) in enumerate(zip(units, weights)):
        cur.append(unit)
        cur_w += weight
        units_left = len(units) - i - 1
        must_close = units_left == buckets_left - 1
        reached = cur_w >= target and units_left >= buckets_left - 1
        if buckets_left > 1 and cur and (must_close or reached):
            buckets.append(cur)
            cur = []
            cur_w = 0
            buckets_left -= 1
    if cur:
        buckets.append(cur)
    if len(buckets) != n:
        return _word_windows(" ".join(units).split(), n)
    return ["\n\n".join(b) for b in buckets]


def _split_script_sections(script: str, n: int) -> list:
    """Split a script into exactly n sections without calling the model.

    Paragraph breaks win when there are enough of them; otherwise sentences,
    then even word windows. The model path used to be the only splitter, so a
    413, a truncated reply, or a count mismatch aborted the job before any
    narration was generated.
    """
    n = max(2, min(20, int(n)))
    text = re.sub(r"\r\n?", "\n", (script or "").strip())
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(paragraphs) >= n:
        chunks = _pack_to_n(paragraphs, n)
    else:
        sentences = []
        for para in paragraphs or ([text] if text else []):
            parts = re.split(r"(?<=[.!?])\s+", para)
            sentences.extend(s.strip() for s in parts if s.strip())
        if len(sentences) >= n:
            chunks = _pack_to_n(sentences, n)
        else:
            chunks = _word_windows(text.split(), n)
    if len(chunks) != n:
        chunks = _word_windows(text.split(), n)
    out = []
    for i, chunk in enumerate(chunks):
        chunk = (chunk or "").strip() or f"Section {i + 1}"
        out.append({"idx": i, "word_count": len(chunk.split()) or 1, "text": chunk})
    return out


def _parse_section_blocks(content: str):
    """Parse ### SECTION blocks (see _parse_meta_block for why plain text, not JSON)."""
    sections = []
    parts = re.split(r"###\s*SECTION\b", content, flags=re.IGNORECASE)
    for block in parts:
        block = block.strip().lstrip(":").strip()
        if not block:
            continue
        m = re.search(
            r"IDX:\s*(\d+)\s*"
            r"WORDCOUNT:\s*(\d+)\s*"
            r"TEXT:\s*(.*)",
            block, re.DOTALL | re.IGNORECASE,
        )
        if not m:
            continue
        text = m.group(3).strip()
        # If the model glued the next section on without a ### marker, cut it off.
        nxt = re.search(r"\n\s*IDX:\s*\d+\s*WORDCOUNT:", text, re.IGNORECASE)
        if nxt:
            text = text[: nxt.start()].strip()
        if not text:
            continue
        sections.append({
            "idx": int(m.group(1)),
            "word_count": int(m.group(2)),
            "text": text,
        })
    return sections


def _ai_generate_metadata(script: str, title: str):
    """Generate title/description/hashtags only — no sections. Used by the
    video-to-sections flow, where sections already come from Clipper's chapter
    proposal rather than this app's own script-splitting step (run_generation's
    analysis_prompt). A trimmed version of that same prompt, minus the
    "sections" key.
    """
    # Title/description/hashtags only need the gist, not the whole script —
    # sending it all blew Groq's free-tier 6K tokens-per-minute cap on long
    # videos (413 "Request too large"). Head + tail keeps the intro (what the
    # video is about) and the close (usually the call-to-action) within the
    # same per-request budget the Clipper's chapter chunking uses.
    if len(script) > 6000:
        script = script[:4800] + "\n[…]\n" + script[-1200:]

    prompt = f"""Based on this video script (excerpted if long), generate YouTube metadata.

Respond with ONLY the following plain-text format, nothing else — no markdown, no JSON, no extra commentary:
### META
TITLE: <compelling video title based on: '{title or 'auto-generate'}'>
DESCRIPTION: <YouTube description, 150-250 words, engaging>
HASHTAGS: tag1, tag2, tag3 (3 to 20 relevant tags, comma-separated, no # prefix)

SCRIPT:
{script}"""

    raw = _groq(prompt, system="You are a video production AI. Respond in the exact "
                "plain-text format requested, nothing else.", max_tokens=1000)
    vid_title, description, hashtags = _parse_meta_block(raw)
    return vid_title or (title or "My Video"), description, hashtags


def _duration_from_probe(data: dict) -> float:
    """Prefer container duration. Some m4a/mp3 files leave stream duration empty."""
    if not isinstance(data, dict):
        return 0.0
    fmt = (data.get("format") or {}).get("duration")
    candidates = [fmt]
    candidates.extend((s or {}).get("duration") for s in data.get("streams") or [])
    for dur in candidates:
        if dur in (None, "", "N/A"):
            continue
        try:
            value = float(dur)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0.0


def _audio_duration(path: str) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_format", "-show_streams", path],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return 0.0
    try:
        return _duration_from_probe(json.loads(result.stdout))
    except Exception:
        return 0.0


def _rescale_sections(sections: list, actual_dur: float) -> None:
    """Stretch/shrink section start/end to match the real narration length."""
    if actual_dur <= 0 or not sections:
        return
    est_total = sections[-1].get("end") or 0
    if est_total <= 0:
        # Equal slices when timings were never estimated (e.g. empty replace).
        n = len(sections)
        step = actual_dur / n
        for i, s in enumerate(sections):
            s["start"] = round(i * step, 2)
            s["end"] = round((i + 1) * step, 2)
        sections[-1]["end"] = round(actual_dur, 2)
        return
    ratio = actual_dur / est_total
    for s in sections:
        s["start"] = round(s["start"] * ratio, 2)
        s["end"] = round(s["end"] * ratio, 2)
    sections[-1]["end"] = round(actual_dur, 2)


_AUDIO_UPLOAD_EXTS = (".mp3", ".wav", ".aac", ".m4a", ".ogg", ".flac", ".wma")


def _save_narration_upload(file_storage, job_dir: str) -> str:
    """Persist an uploaded narration file as narration.mp3 (ffmpeg convert/copy)."""
    os.makedirs(job_dir, exist_ok=True)
    orig_name = file_storage.filename or "upload.mp3"
    ext = os.path.splitext(orig_name)[1].lower() or ".mp3"
    if ext not in _AUDIO_UPLOAD_EXTS:
        raise ValueError(f"Unsupported audio format: {ext}")
    raw_path = os.path.join(job_dir, f"_upload_narration{ext}")
    audio_path = os.path.join(job_dir, "narration.mp3")
    file_storage.save(raw_path)
    # Always land on narration.mp3 — the UI and assemble path expect that name.
    cmd = [
        "ffmpeg", "-y", "-i", raw_path,
        "-vn", "-c:a", "libmp3lame", "-q:a", "2",
        audio_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        os.remove(raw_path)
    except OSError:
        pass
    if result.returncode != 0 or not os.path.exists(audio_path):
        err = (result.stderr or result.stdout or "ffmpeg failed")[-400:]
        raise RuntimeError(f"Could not convert audio: {err}")
    return audio_path

def _make_segment(media_path: str, duration: float, out_path: str, is_image: bool):
    vf = "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1"
    if is_image:
        cmd = [
            "ffmpeg", "-y", "-loop", "1", "-t", str(duration),
            "-i", media_path,
            "-vf", vf, "-r", "25",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-pix_fmt", "yuv420p",
            out_path,
        ]
    else:
        cmd = [
            "ffmpeg", "-y", "-t", str(duration),
            "-i", media_path,
            "-vf", vf, "-r", "25",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-pix_fmt", "yuv420p",
            "-an", out_path,
        ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Segment {out_path}: {result.stderr[-300:]}")

def _make_placeholder(duration: float, idx: int, out_path: str, black: bool = False):
    colours = ["0x1a1a2e","0x16213e","0x0f3460","0x533483","0x2c2c54","0x1a3a4a","0x2d1b2e","0x1b2d2e"]
    c = "black" if black else colours[idx % len(colours)]
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c={c}:size=1920x1080:rate=25",
        "-t", str(duration),
        "-vf", "setsar=1",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-pix_fmt", "yuv420p",
        out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Placeholder {idx}: {result.stderr[-300:]}")

def _video_duration(path: str) -> float:
    """Get encoded video stream duration via ffprobe."""
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_streams", "-select_streams", "v", path],
        capture_output=True, text=True
    )
    try:
        data = json.loads(result.stdout)
        for s in data.get("streams", []):
            if s.get("duration"):
                return float(s["duration"])
    except Exception:
        pass
    return 0.0

def _ffmpeg_xfade_concat(seg_paths: list, seg_durations: list, out_path: str, xfade_dur: float = 0.5):
    """Concat video segments with smooth crossfade transitions."""
    if len(seg_paths) == 1:
        shutil.copy(seg_paths[0], out_path)
        return
    inputs = []
    for p in seg_paths:
        inputs += ["-i", p]
    parts = []
    prev_label = "[0:v]"
    cumulative = seg_durations[0]
    for i in range(1, len(seg_paths)):
        offset = max(0.01, cumulative - i * xfade_dur)
        out_label = "[vout]" if i == len(seg_paths) - 1 else f"[v{i}]"
        parts.append(
            f"{prev_label}[{i}:v]xfade=transition=fade"
            f":duration={xfade_dur:.2f}:offset={offset:.3f}{out_label}"
        )
        prev_label = out_label
        cumulative += seg_durations[i]
    cmd = [
        "ffmpeg", "-y", *inputs,
        "-filter_complex", ";".join(parts),
        "-map", "[vout]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-r", "25", out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg xfade: {result.stderr[-500:]}")

def _build_thumbnail(src_path: str, title: str, out_path: str, is_video: bool = False):
    """Build a 1280×720 thumbnail with title overlay."""
    base = src_path
    if is_video:
        base = out_path.replace(".jpg", "_frame.jpg")
        subprocess.run(
            ["ffmpeg", "-y", "-i", src_path, "-frames:v", "1", "-q:v", "2", base],
            capture_output=True
        )
    if not os.path.exists(base):
        raise RuntimeError("Source image not found")
    if _PIL:
        img = Image.open(base).convert("RGB").resize((1280, 720), Image.LANCZOS)
        w, h = img.size
        draw = ImageDraw.Draw(img, "RGBA")
        for row in range(h // 3):
            alpha = int(215 * row / (h // 3))
            draw.rectangle([(0, h - h//3 + row), (w, h - h//3 + row + 1)],
                           fill=(0, 0, 0, alpha))
        font_size = max(52, w // 13)
        font = None
        for fp in [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        ]:
            if os.path.exists(fp):
                try: font = ImageFont.truetype(fp, font_size); break
                except: pass
        if not font:
            font = ImageFont.load_default()
        draw = ImageDraw.Draw(img)
        draw.text((w // 2, h - h // 8), title[:60], font=font, fill="white",
                  anchor="mm", align="center", stroke_width=3, stroke_fill="black")
        img.save(out_path, "JPEG", quality=95)
    else:
        safe = title[:50].replace("'", "").replace(":", "").replace("\\", "")
        vf = (
            "scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720,"
            f"drawbox=x=0:y=ih*3/4:w=iw:h=ih/4:color=black@0.75:t=fill,"
            f"drawtext=text='{safe}':fontsize=56:fontcolor=white:"
            "x=(w-text_w)/2:y=h*7/8-text_h/2:shadowcolor=black:shadowx=2:shadowy=2"
        )
        subprocess.run(
            ["ffmpeg", "-y", "-i", base, "-vf", vf, "-frames:v", "1", out_path],
            capture_output=True
        )


# ─── generation pipeline (audio only) ────────────────────────────────

def run_generation(job_id: str, script: str, voice: str, title: str, n_sections: int,
                   uploaded_audio_src: str | None = None):
    job     = JOBS[job_id]
    q       = job["queue"]
    push    = lambda d: _push(q, d)
    job_dir = os.path.join(OUTPUT_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    try:
        # Step 1: section cards are a local split so a Groq 413 / bad parse
        # cannot abort the job before narration. Metadata stays best-effort.
        push({"phase": "analysing", "msg": "Splitting script into sections…"})
        sections_raw = _split_script_sections(script, n_sections)
        vid_title = title or "My Video"
        description = ""
        hashtags: list = []

        # Short scripts still ask the model for topic-aware section text plus
        # metadata in one call. A long script is not sent whole — that request
        # 413s on Groq's per-minute cap and the truncated reply used to fail
        # the job. Metadata for long scripts uses the already-truncated excerpt.
        ai_sections = None
        groq_down = False
        if len(script) <= 6000:
            analysis_prompt = f"""Split this video script into exactly {n_sections} sections for a video.

Respond with ONLY the following plain-text format, nothing else — no markdown, no JSON, no extra commentary:
### META
TITLE: <compelling video title based on: '{title or 'auto-generate'}'>
DESCRIPTION: <YouTube description, 150-250 words, engaging>
HASHTAGS: tag1, tag2, tag3 (3 to 20 relevant tags, comma-separated, no # prefix)
### SECTION
IDX: 0
WORDCOUNT: <word count for this section's TEXT>
TEXT:
<script lines for this section>
### SECTION
IDX: 1
WORDCOUNT: <word count for this section's TEXT>
TEXT:
<script lines for this section>
(repeat for all {n_sections} sections, IDX running from 0 to {n_sections - 1},
covering the entire script start to finish with no gaps)

Split at natural paragraph or topic breaks.

SCRIPT:
{script}"""
            try:
                raw = _groq(analysis_prompt,
                            system="You are a video production AI. Respond in the exact "
                            "plain-text format requested, nothing else.", max_tokens=3000)
                vid_title, description, hashtags = _parse_meta_block(raw)
                parsed = _parse_section_blocks(raw)
                if len(parsed) == n_sections:
                    ai_sections = parsed
                else:
                    print(f"  [ytprod] AI returned {len(parsed)} sections, wanted "
                          f"{n_sections} — using local split", flush=True)
            except Exception as e:
                # _groq already retried. A second metadata call would repeat
                # that wait and still leave the producer with no narration.
                groq_down = True
                print(f"  [ytprod] AI sectioning skipped: {e}", flush=True)

        if ai_sections is None and not groq_down:
            try:
                # Avoid a second Groq call when the combined prompt already
                # filled metadata, even if its section count was unusable.
                if not description and not hashtags:
                    vid_title, description, hashtags = _ai_generate_metadata(script, title)
            except Exception as e:
                print(f"  [ytprod] metadata skipped, continuing with narration: {e}",
                      flush=True)
        if ai_sections is not None:
            sections_raw = ai_sections
        vid_title = vid_title or (title or "My Video")

        # Build estimated timings from word counts (~140 wpm)
        total_words = sum(s.get("word_count", len(s.get("text","").split())) for s in sections_raw)
        total_est   = max(total_words / 140 * 60, 10.0)

        sections = []
        cursor = 0.0
        for i, s in enumerate(sections_raw[:n_sections]):
            wc  = s.get("word_count", len(s.get("text", "").split()))
            dur = max((wc / max(total_words, 1)) * total_est, 2.0)
            sections.append({
                "idx": i,
                "text": s.get("text", ""),
                "start": round(cursor, 2),
                "end":   round(cursor + dur, 2),
                "media":     None,
                "media_ext": None,
            })
            cursor += dur

        job.update({
            "title": vid_title,
            "description": description,
            "hashtags": hashtags,
            "sections": sections,
        })
        push({"phase": "analysed", "msg": f"Script split into {len(sections)} sections",
              "section_count": len(sections)})

        # Step 2: Narration — uploaded file or edge-tts
        audio_path = os.path.join(job_dir, "narration.mp3")
        if uploaded_audio_src:
            push({"phase": "audio", "msg": "Loading uploaded narration…"})
            if not os.path.exists(uploaded_audio_src):
                raise RuntimeError("Uploaded audio missing on server")
            # Convert/copy into the canonical narration.mp3 path.
            cmd = [
                "ffmpeg", "-y", "-i", uploaded_audio_src,
                "-vn", "-c:a", "libmp3lame", "-q:a", "2",
                audio_path,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            try:
                os.remove(uploaded_audio_src)
            except OSError:
                pass
            if result.returncode != 0 or not os.path.exists(audio_path):
                err = (result.stderr or result.stdout or "ffmpeg failed")[-400:]
                raise RuntimeError(f"Could not convert uploaded audio: {err}")
            print(f"  [ytprod] uploaded narration ready: {os.path.getsize(audio_path):,} bytes",
                  flush=True)
        else:
            push({"phase": "audio", "msg": "Generating audio narration…"})
            if not _EDGE_TTS:
                raise RuntimeError("edge-tts not installed — run: pip install edge-tts")

            async def _tts():
                communicate = edge_tts.Communicate(
                    script, voice, receive_timeout=300, **_tts_prosody(job))
                with open(audio_path, "wb") as af:
                    async for chunk in communicate.stream():
                        if chunk.get("type") == "audio":
                            af.write(chunk["data"])
                size = os.path.getsize(audio_path)
                print(f"  [ytprod] TTS done: {size:,} bytes", flush=True)

            asyncio.run(_tts())
        job["audio_path"] = audio_path

        # Step 3: Rescale timings to actual audio duration
        actual_dur = _audio_duration(audio_path)
        _rescale_sections(sections, actual_dur)

        job["status"] = "audio_ready"
        _persist_job(job_id)
        push({
            "phase": "audio_done",
            "msg": "Audio ready — upload your media for each section",
            "audio_dur": round(actual_dur, 2),
            "sections": sections,
            "title": vid_title,
            "description": description,
            "hashtags": hashtags,
        })

    except Exception as e:
        traceback.print_exc()
        job["status"] = "error"
        job["error_message"] = str(e)
        _persist_job(job_id)
        push({"phase": "error", "msg": str(e)})
    finally:
        q.put(None)


def run_generation_from_sections(job_id: str, sections_text: list, script_for_tts: str,
                                  voice: str, vid_title: str, description: str, hashtags: list):
    """Parallel to run_generation — skips the AI script-split step (Step 1)
    entirely. sections_text is a producer-approved, possibly hand-edited list
    of section strings from the video-to-sections review flow (platform-side,
    not this app). run_generation itself is untouched by this addition, so the
    existing typed-script path can't regress from this change.
    """
    job     = JOBS[job_id]
    q       = job["queue"]
    push    = lambda d: _push(q, d)
    job_dir = os.path.join(OUTPUT_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    try:
        # Word counts are computed fresh from sections_text here — never carried
        # over from proposal generation, since the producer may have edited the
        # text and a stale count would throw off the pre-rescale duration estimate.
        total_words = sum(len(t.split()) for t in sections_text) or 1
        total_est = max(total_words / 140 * 60, 10.0)

        sections = []
        cursor = 0.0
        for i, text in enumerate(sections_text):
            wc = len(text.split())
            dur = max((wc / total_words) * total_est, 2.0)
            sections.append({
                "idx": i,
                "text": text,
                "start": round(cursor, 2),
                "end":   round(cursor + dur, 2),
                "media":     None,
                "media_ext": None,
            })
            cursor += dur

        job.update({
            "title": vid_title,
            "description": description,
            "hashtags": hashtags,
            "sections": sections,
        })
        # Synthetic "analysed" event — the existing frontend's phase-handling/
        # onAudioReady() logic expects this before "audio"/"audio_done", even
        # though this path skipped the AI-split step that normally produces it.
        push({"phase": "analysed", "msg": f"Using {len(sections)} reviewed sections",
              "section_count": len(sections)})

        # Step 2: audio — identical to run_generation's Step 2. Narrates the full
        # joined script as ONE continuous file; sections are time-boundaries
        # within it, not separate TTS calls (matches existing behavior exactly).
        push({"phase": "audio", "msg": "Generating audio narration…"})
        if not _EDGE_TTS:
            raise RuntimeError("edge-tts not installed — run: pip install edge-tts")

        audio_path = os.path.join(job_dir, "narration.mp3")

        async def _tts():
            communicate = edge_tts.Communicate(
                script_for_tts, voice, receive_timeout=300, **_tts_prosody(job))
            with open(audio_path, "wb") as af:
                async for chunk in communicate.stream():
                    if chunk.get("type") == "audio":
                        af.write(chunk["data"])
            size = os.path.getsize(audio_path)
            print(f"  [ytprod] TTS done: {size:,} bytes", flush=True)

        asyncio.run(_tts())
        job["audio_path"] = audio_path

        # Step 3: rescale — identical to run_generation's Step 3.
        actual_dur = _audio_duration(audio_path)
        _rescale_sections(sections, actual_dur)

        job["status"] = "audio_ready"
        _persist_job(job_id)
        push({
            "phase": "audio_done",
            "msg": "Audio ready — upload your media for each section",
            "audio_dur": round(actual_dur, 2),
            "sections": sections,
            "title": vid_title,
            "description": description,
            "hashtags": hashtags,
        })

    except Exception as e:
        traceback.print_exc()
        job["status"] = "error"
        # run_generation doesn't persist this (only pushes it via the SSE queue,
        # which drains once consumed) — stored here too so /job-state can report
        # a real message on resume rather than just "status: error" with no detail.
        # Only added to this new function; run_generation itself is untouched.
        job["error_message"] = str(e)
        _persist_job(job_id)
        push({"phase": "error", "msg": str(e)})
    finally:
        q.put(None)


# ─── assembly pipeline ────────────────────────────────────────────────

def _fmt_ch(sec: float) -> str:
    s = int(sec); m = s // 60; sc = s % 60
    return f"{m}:{sc:02d}"


def _assembly_durations(sections: list, use_xfade: bool, xfade_dur: float = 0.5):
    """Per-segment encode lengths.

    Each crossfade shortens the timeline by xfade_dur, and the final merge
    uses -shortest, so an uncompensated crossfade cuts the end of the
    narration. The last segment is extended by the total fade time plus a
    small slack so the video outlasts the audio and -shortest keeps the voice.
    Very short sections get a shorter fade, or no fade, so ffmpeg doesn't
    reject the filter graph.
    """
    natural = [max(float(s.get("end", 0)) - float(s.get("start", 0)), 0.5) for s in sections]
    if not natural:
        return [], 0.0, False
    if use_xfade and len(natural) > 1:
        shortest = min(natural)
        if shortest < xfade_dur * 2:
            xfade_dur = round(max(0.0, shortest * 0.35), 3)
        if xfade_dur < 0.12:
            use_xfade = False
            xfade_dur = 0.0
    durs = list(natural)
    extra = 0.25
    if use_xfade and len(durs) > 1:
        extra += (len(durs) - 1) * xfade_dur
    durs[-1] = round(durs[-1] + extra, 3)
    return durs, xfade_dur, use_xfade


def _ffmpeg_concat(seg_paths: list, job_dir: str, out_path: str):
    concat_txt = os.path.join(job_dir, "concat.txt")
    with open(concat_txt, "w") as f:
        for p in seg_paths:
            f.write(f"file '{p}'\n")
    result = subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_txt, "-c", "copy", out_path],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg concat: {result.stderr[-400:]}")


# ─── audio visualizer ────────────────────────────────────────────────
# Audio-reactive glow layer rendered from the final mix and screen-blended
# over the assembled video. Screen over black is a no-op, so the layer only
# lights up where the wave is; sections without media fall back to black so
# the visualizer becomes their whole background.
VIZ_STYLES     = ("none", "neon", "bars", "aurora")
VIZ_PLACEMENTS = ("full", "band")
_VIZ_W, _VIZ_H = 960, 540          # rendered small, upscaled — glow hides it
_VIZ_SR        = 24000             # 960 samples per 25fps frame = 1 px per sample


def _viz_gradient(stops: list, vertical: bool, label: str) -> str:
    """One-frame RGB gradient (looped), evenly spaced stops of (r, g, b)."""
    axis = "Y/H" if vertical else "X/W"
    n = len(stops) - 1

    def chan(c):
        expr = str(stops[-1][c])
        for i in range(n - 1, -1, -1):
            a, b = stops[i][c], stops[i + 1][c]
            seg = f"{a}+({b}-{a})*({axis}*{n}-{i})"
            expr = f"if(lt({axis}\\,{(i + 1) / n:.4f})\\,{seg}\\,{expr})"
        return expr

    return (f"color=c=black:s={_VIZ_W}x{_VIZ_H}:r=25:d=1,format=rgb24,"
            f"geq=r='{chan(0)}':g='{chan(1)}':b='{chan(2)}',"
            f"loop=loop=-1:size=1,format=rgba{label}")


def _viz_carrier_waves(src: str, carriers: list, tag: str, height: int) -> list:
    """Smooth flowing strands: the narration's loudness envelope drives the
    amplitude of slow synthetic carriers, so the shape stays a clean curve
    instead of raw speech jitter. Returns filter chains ending in [{tag}w]."""
    env = (f"{src}aformat=channel_layouts=mono,aresample=8000,aeval=abs(val(0)),"
           f"lowpass=f=8,aresample=200,lowpass=f=4,lowpass=f=4,"
           f"aresample={_VIZ_SR},volume=4.5,alimiter=limit=0.6,"
           f"asplit={len(carriers)}" + "".join(f"[{tag}e{i}]" for i in range(len(carriers))))
    parts = [env]
    for i, (expr, vol) in enumerate(carriers):
        parts.append(f"aevalsrc=exprs='{expr}':s={_VIZ_SR}:c=mono[{tag}c{i}]")
        parts.append(
            f"[{tag}e{i}][{tag}c{i}]amultiply,volume={vol},"
            f"showwaves=s={_VIZ_W}x{height}:mode=p2p:rate=25:draw=full:colors=white,"
            f"format=gray[{tag}s{i}]")
    prev = f"[{tag}s0]"
    for i in range(1, len(carriers)):
        out = f"[{tag}m{i}]"
        parts.append(f"{prev}[{tag}s{i}]blend=all_mode=lighten{out}")
        prev = out
    parts.append(f"{prev}null[{tag}w]")
    return parts


def _viz_filter(style: str, placement: str, out_w: int = 1920, out_h: int = 1080) -> str:
    """Filter chains that read [vizin] (audio) and emit [vizout], an RGB
    layer of out_w×out_h on black, ready to screen-blend over the video."""
    W, H = _VIZ_W, _VIZ_H
    parts = []
    if style == "neon":
        # Orange → violet → blue strands with trails (layered silk waves).
        parts += _viz_carrier_waves("[vizin]", [
            ("0.5*sin(2*PI*50.3*t)+0.3*sin(2*PI*75.9*t+1)+0.2*sin(2*PI*125.6*t+2)", 1.0),
            ("0.4*sin(2*PI*50.7*t+2)+0.35*sin(2*PI*100.4*t)+0.2*sin(2*PI*151.2*t+1)", 0.8),
            ("0.6*sin(2*PI*49.6*t+4)+0.25*sin(2*PI*74.7*t+3)", 1.2),
        ], "n", H)
        parts.append("[nw]lagfun=decay=0.92,split[vm][vg]")
        parts.append("[vg]gblur=sigma=8,lutyuv=y=val*1.6[vgl]")
        parts.append("[vm][vgl]blend=all_mode=addition[vmask]")
        parts.append(_viz_gradient([(255, 90, 31), (176, 60, 255), (30, 144, 255)], False, "[vgrad]"))
    elif style == "aurora":
        # Stacked mesh of strands, lime → magenta → violet → cyan top to bottom.
        parts.append("[vizin]asplit=3[viza0][viza1][viza2]")
        for k, (tag, y, ph) in enumerate((("a", 20, 0), ("b", 140, 2), ("c", 250, 4))):
            parts += _viz_carrier_waves(f"[viza{k}]", [
                (f"0.55*sin(2*PI*50.2*t+{ph})+0.3*sin(2*PI*99.7*t+{ph + 1})", 1.0),
                (f"0.5*sin(2*PI*49.8*t+{ph + 2})+0.3*sin(2*PI*75.3*t)", 0.7),
            ], tag, H // 2)
            parts.append(f"[{tag}w]pad={W}:{H}:0:{y}[{tag}p]")
        parts.append("[ap][bp]blend=all_mode=lighten[abp]")
        parts.append("[abp][cp]blend=all_mode=lighten,lagfun=decay=0.96,split[vm][vg]")
        parts.append("[vg]gblur=sigma=5,lutyuv=y=val*1.4[vgl]")
        parts.append("[vm][vgl]blend=all_mode=addition[vmask]")
        parts.append(_viz_gradient(
            [(212, 255, 58), (255, 63, 164), (122, 60, 255), (42, 168, 255)], True, "[vgrad]"))
    else:  # bars
        # Mirrored LED equalizer with a floor reflection, violet ↔ blue.
        parts.append(
            f"[vizin]aformat=channel_layouts=mono,aresample=11025,dynaudnorm,volume=2,"
            f"showfreqs=s=32x54:mode=bar:ascale=sqrt:fscale=lin:win_size=64:averaging=2:"
            f"colors=white:rate=25,format=gray,"
            f"scale={W // 2}:{H // 2}:flags=neighbor,drawgrid=w=15:h=5:t=3:c=black,"
            # drawgrid's "black" is limited-range luma 16; clamp the noise floor
            # and the grid to true 0 so no faint box shows through the blend.
            f"lutyuv=y='if(lt(val\\,40)\\,0\\,val)',split[bl][br]")
        parts.append("[br]hflip[bf]")
        parts.append("[bf][bl]hstack,split[bt][bb]")
        parts.append("[bb]vflip,lutyuv=y=val*0.3[brf]")
        parts.append("[bt][brf]vstack,split[vm][vg]")
        parts.append("[vg]gblur=sigma=8[vgl]")
        parts.append("[vm][vgl]blend=all_mode=screen[vmask]")
        parts.append(_viz_gradient([(210, 75, 255), (47, 123, 255), (210, 75, 255)], True, "[vgrad]"))

    if placement == "band":
        band_h = out_h // 3
        place = (f"scale={out_w}:{band_h}:flags=bicubic,"
                 f"pad={out_w}:{out_h}:0:{out_h - band_h}")
    else:
        place = f"scale={out_w}:{out_h}:flags=bicubic"
    parts.append("[vmask]format=gray[vmg]")
    parts.append("[vgrad][vmg]alphamerge[vcol]")
    parts.append(f"color=c=black:s={W}x{H}:r=25[vbg]")
    parts.append(f"[vbg][vcol]overlay=shortest=1,{place},format=gbrp[vizout]")
    return ";".join(parts)


def _merge_with_visualizer(video: str, audio: str, out_path: str,
                           style: str, placement: str, opacity: float,
                           total_dur: float = 0.0, on_progress=None):
    """Final merge that also renders the visualizer from the mixed audio.
    It re-encodes the whole video (~realtime), so report percent as it goes."""
    graph = (
        "[1:a]asplit=2[aout][vizin];"
        + _viz_filter(style, placement) + ";"
        + "[0:v]scale=1920:1080,setsar=1,format=gbrp[base];"
        + f"[base][vizout]blend=all_mode=screen:all_opacity={opacity:.2f},"
          "format=yuv420p[vout]"
    )
    proc = subprocess.Popen([
        "ffmpeg", "-y", "-nostats", "-progress", "pipe:1",
        "-i", video, "-i", audio,
        "-filter_complex", graph,
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest", "-movflags", "+faststart", out_path,
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    # Drain stderr on a thread so a chatty encoder can't fill the pipe and stall.
    err_tail = []
    def _drain():
        for line in proc.stderr:
            err_tail.append(line)
            del err_tail[:-40]
    t = threading.Thread(target=_drain, daemon=True)
    t.start()
    last_pct = -1
    for line in proc.stdout:
        if not (on_progress and total_dur > 0 and line.startswith("out_time_us=")):
            continue
        try:
            pct = int(min(99, int(line.split("=", 1)[1]) / 1e6 / total_dur * 100))
        except ValueError:
            continue
        if pct >= last_pct + 2:
            last_pct = pct
            on_progress(pct)
    proc.wait()
    t.join(timeout=5)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg visualizer merge: {''.join(err_tail)[-500:]}")


def run_assembly(job_id: str):
    job       = JOBS[job_id]
    q         = job["assemble_queue"]
    push      = lambda d: _push(q, d)
    job_dir   = os.path.join(OUTPUT_DIR, job_id)
    sections  = job["sections"]
    total     = len(sections)
    use_xfade = job.get("xfade", True)
    xfade_dur = 0.5
    viz_style = job.get("viz_style") or "none"
    viz_full  = viz_style != "none" and job.get("viz_placement", "full") == "full"

    try:
        seg_encode_durs, xfade_dur, use_xfade = _assembly_durations(
            sections, use_xfade, xfade_dur)
        push({"phase": "assembling", "msg": f"Building {total} video segments…"})
        seg_paths = []
        seg_durations = []

        for s, dur in zip(sections, seg_encode_durs):
            idx     = s["idx"]
            media   = s.get("media")
            ext     = (s.get("media_ext") or "").lower()
            seg_out = os.path.join(job_dir, f"seg_{idx:03d}.mp4")

            push({"phase": "seg_progress", "msg": f"Segment {idx+1}/{total}…",
                  "current": idx+1, "total": total})

            if media and os.path.exists(media):
                is_image = ext in (".jpg", ".jpeg", ".png", ".gif", ".webp")
                _make_segment(media, dur, seg_out, is_image)
            else:
                _make_placeholder(dur, idx, seg_out, black=viz_full)

            actual_dur = _video_duration(seg_out) or dur
            seg_paths.append(seg_out)
            seg_durations.append(actual_dur)

        push({"phase": "concat", "msg": "Joining segments…"})
        merged = os.path.join(job_dir, "merged.mp4")

        if use_xfade and len(seg_paths) > 1:
            try:
                _ffmpeg_xfade_concat(seg_paths, seg_durations, merged, xfade_dur)
            except Exception as e:
                print(f"  [ytprod] xfade failed, joining cuts directly: {e}", flush=True)
                _ffmpeg_concat(seg_paths, job_dir, merged)
        else:
            _ffmpeg_concat(seg_paths, job_dir, merged)

        # Mix music if provided
        narration  = job["audio_path"]
        audio_dur  = _audio_duration(narration)
        music_path = job.get("music_path")
        music_vol  = float(job.get("music_vol", 0.15))

        if music_path and os.path.exists(music_path):
            push({"phase": "audio_merge", "msg": "Mixing music + narration…"})
            mixed = os.path.join(job_dir, "mixed_audio.aac")
            fade_st = max(audio_dur - 3.0, 0.0)
            r = subprocess.run([
                "ffmpeg", "-y",
                "-i", narration,
                "-stream_loop", "-1", "-i", music_path,
                "-filter_complex",
                f"[1:a]volume={music_vol:.2f},afade=t=out:st={fade_st:.1f}:d=3[mus];"
                f"[0:a][mus]amix=inputs=2:duration=first[aout]",
                "-map", "[aout]",
                "-c:a", "aac", "-b:a", "192k",
                "-t", str(audio_dur),
                mixed,
            ], capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(f"Music mix: {r.stderr[-400:]}")
            final_audio = mixed
        else:
            push({"phase": "audio_merge", "msg": "Adding audio track…"})
            final_audio = narration

        video_path = os.path.join(job_dir, "video.mp4")
        if viz_style != "none":
            push({"phase": "visualizer",
                  "msg": "Rendering audio visualizer — about as long as the video…"})
            _merge_with_visualizer(
                merged, final_audio, video_path, viz_style,
                job.get("viz_placement", "full"), float(job.get("viz_opacity", 0.9)),
                total_dur=audio_dur,
                on_progress=lambda pct: push({
                    "phase": "visualizer", "pct": pct,
                    "msg": f"Rendering audio visualizer… {pct}%"}))
        else:
            r = subprocess.run(
                ["ffmpeg", "-y", "-i", merged, "-i", final_audio,
                 "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                 "-shortest", "-movflags", "+faststart", video_path],
                capture_output=True, text=True
            )
            if r.returncode != 0:
                raise RuntimeError(f"ffmpeg final merge: {r.stderr[-400:]}")

        # Chapter markers
        chapters = "\n".join(
            f"{_fmt_ch(s['start'])} Section {s['idx']+1}"
            for s in sections
        )
        job["chapters"]   = chapters
        job["video_path"] = video_path
        job["status"]     = "done"
        _persist_job(job_id)
        push({"phase": "done", "msg": "Video ready!", "chapters": chapters})

    except Exception as e:
        traceback.print_exc()
        job["status"] = "error_assembly"
        job["error_message"] = str(e)
        _persist_job(job_id)
        push({"phase": "error", "msg": str(e)})
    finally:
        q.put(None)


# ─── routes ──────────────────────────────────────────────────────────

@app.route("/favicon.ico")
def favicon():
    return Response(status=204)


@app.route("/")
def index():
    return Response(PAGE, mimetype="text/html")


@app.route("/generate", methods=["POST"])
def generate():
    data       = request.get_json(silent=True) or {}
    script     = data.get("script", "").strip()
    voice      = data.get("voice", "en-US-GuyNeural")
    title      = data.get("title", "").strip()
    try:
        n_sections = int(data.get("sections", 6))
    except (TypeError, ValueError):
        n_sections = 6
    n_sections = max(2, min(20, n_sections))

    if not script:
        return jsonify({"error": "No script provided"}), 400
    if len(script) < 20:
        return jsonify({"error": "Script too short"}), 400

    job_id = uuid.uuid4().hex
    JOBS[job_id] = {
        "status": "running",
        "queue": queue.Queue(),
        "script": script,
        "voice": voice,
        **_clean_prosody(data.get("rate"), data.get("pitch")),
        "sections": [],
        "audio_path": None,
        "video_path": None,
    }
    _persist_job(job_id)
    threading.Thread(target=run_generation,
                     args=(job_id, script, voice, title, n_sections), daemon=True).start()
    return jsonify({"job_id": job_id})


def _start_generation_from_sections(sections_text, voice, title, description, hashtags,
                                    rate=None, pitch=None):
    """Create a job and spawn run_generation_from_sections. Callable directly
    in-process (no Flask request context needed — the platform's producer_scout
    blueprint calls this straight from sys.modules, same pattern as the
    suggest-agent CLI calling into Clipper's functions) or via the
    /generate-from-sections HTTP route below, which is a thin wrapper around it.
    """
    sections_text = [s.strip() for s in sections_text if isinstance(s, str) and s.strip()]
    if not sections_text:
        raise ValueError("No sections provided")
    script_for_tts = "\n\n".join(sections_text)
    if len(script_for_tts) < 20:
        raise ValueError("Script too short")

    job_id = uuid.uuid4().hex
    JOBS[job_id] = {
        "status": "running",
        "queue": queue.Queue(),
        "script": script_for_tts,
        "voice": voice,
        **_clean_prosody(rate, pitch),
        "sections": [],
        "audio_path": None,
        "video_path": None,
    }
    _persist_job(job_id)
    threading.Thread(
        target=run_generation_from_sections,
        args=(job_id, sections_text, script_for_tts, voice, title or "My Video", description, hashtags),
        daemon=True,
    ).start()
    return job_id


@app.route("/generate-from-sections", methods=["POST"])
def generate_from_sections():
    data = request.get_json(silent=True) or {}
    try:
        job_id = _start_generation_from_sections(
            sections_text=data.get("sections", []),
            voice=data.get("voice", "en-US-GuyNeural"),
            title=(data.get("title") or "").strip(),
            description=data.get("description", ""),
            hashtags=data.get("hashtags", []),
            rate=data.get("rate"),
            pitch=data.get("pitch"),
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"job_id": job_id})


_PREVIEW_LINE = ("Here's a quick preview of how this voice will sound "
                 "narrating your next video.")


@app.route("/voice-preview", methods=["POST"])
def voice_preview():
    """Short TTS sample so producers can audition a voice before generating."""
    data = request.get_json(silent=True) or {}
    voice = (data.get("voice") or "").strip()
    if not _VOICE_RE.match(voice):
        return jsonify({"error": "Unknown voice"}), 400
    if not _EDGE_TTS:
        return jsonify({"error": "edge-tts not installed"}), 500
    pros = _clean_prosody(data.get("rate"), data.get("pitch"))

    async def _tts():
        buf = bytearray()
        communicate = edge_tts.Communicate(
            _PREVIEW_LINE, voice, receive_timeout=60,
            rate=pros["voice_rate"], pitch=pros["voice_pitch"])
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio":
                buf += chunk["data"]
        return bytes(buf)

    try:
        audio = asyncio.run(_tts())
    except Exception as e:
        return jsonify({"error": f"Preview failed: {e}"}), 502
    if not audio:
        return jsonify({"error": "Preview returned no audio"}), 502
    resp = Response(audio, mimetype="audio/mpeg")
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


@app.route("/job-state/<job_id>")
def job_state(job_id):
    """Lightweight snapshot of a job's current state (no queue) — lets the
    frontend resume a job whose SSE stream has already been fully drained
    (the sentinel already consumed means reconnecting to /progress hangs
    forever with no way to ever see that job's state again). Used by the
    ?job= deep-link resume path. Hydrates from disk state.json when the
    in-memory JOBS entry was lost (page refresh after process recycle, etc.).
    """
    job = _get_job(job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404
    audio_dur = None
    if job.get("audio_path") and os.path.exists(job["audio_path"]):
        audio_dur = round(_audio_duration(job["audio_path"]), 2)
    return jsonify({
        "status": job.get("status"),
        "script": job.get("script") or "",
        "voice": job.get("voice"),
        "title": job.get("title"),
        "description": job.get("description"),
        "hashtags": job.get("hashtags"),
        "sections": job.get("sections", []),
        "audio_dur": audio_dur,
        "has_video": bool(job.get("video_path") and os.path.exists(job["video_path"] or "")),
        "has_music": bool(job.get("music_path") and os.path.exists(job["music_path"] or "")),
        "music_vol": job.get("music_vol", 0.15),
        "voice_rate": job.get("voice_rate", "+0%"),
        "voice_pitch": job.get("voice_pitch", "+0Hz"),
        "viz_style": job.get("viz_style", "none"),
        "viz_placement": job.get("viz_placement", "full"),
        "viz_opacity": job.get("viz_opacity", 0.9),
        "chapters": job.get("chapters"),
        "error_message": job.get("error_message"),
        # Written by the platform's producer_scout clip-extraction thread
        # ({status, done, total}); absent entirely for typed-script jobs.
        # The frontend polls this to live-fill section previews as each
        # auto-extracted clip lands, instead of requiring a page refresh.
        "clip_prefill": job.get("clip_prefill"),
    })


@app.route("/progress/<job_id>")
def progress(job_id):
    job = _get_job(job_id)
    if not job:
        return "Not found", 404
    # Hydrated jobs have an empty queue — clients should use /job-state instead.
    if job.get("status") != "running" or job.get("queue") is None:
        return "Not found", 404

    return Response(_iter_sse(job["queue"]), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _ensure_upload_job(job_id: str, min_idx: int = 0):
    """Guarantee a JOBS entry for section media uploads.

    After a worker recycle the browser still has ?job=<id> but memory is empty.
    Prefer disk rebuild (narration + section_*); otherwise create a stub so the
    uploaded file is not rejected — producer can reload audio onto the same id
    via replace, or assemble once narration is back on disk.
    """
    if not job_id or not re.fullmatch(r"[0-9a-fA-F]{32}", job_id):
        return None
    job_id = job_id.lower()
    n = max(2, min(20, int(min_idx) + 1))
    hint = [{"idx": i} for i in range(n)]

    job = _get_job(job_id, sections_hint=hint)
    if job:
        return job

    job_dir = _job_dir(job_id)
    os.makedirs(job_dir, exist_ok=True)
    job = _rebuild_job_from_disk(job_id, hint)
    if job:
        return job

    # No narration on disk — keep the session alive so media uploads stick.
    sections = _equal_sections(n, 60.0)
    for s in sections:
        media, ext = _find_section_media(job_dir, s["idx"])
        if media:
            s["media"] = media
            s["media_ext"] = ext
    job = {
        "status": "audio_ready",
        "queue": queue.Queue(),
        "script": "",
        "voice": "en-US-GuyNeural",
        "title": "My Video",
        "description": "",
        "hashtags": [],
        "sections": sections,
        "audio_path": None,
        "video_path": None,
        "music_path": _find_music(job_dir),
        "chapters": None,
        "error_message": None,
        "xfade": True,
        "music_vol": 0.15,
        "clip_prefill": None,
    }
    JOBS[job_id] = job
    _persist_job(job_id)
    print(f"  [ytprod] stub job {job_id} for uploads ({n} sections, "
          f"OUTPUT_DIR={OUTPUT_DIR})", flush=True)
    return job


@app.route("/upload-section/<job_id>/<int:idx>", methods=["POST"])
def upload_section(job_id, idx):
    job_id = (job_id or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        return jsonify({"error": "Invalid job id"}), 400
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400
    f = request.files["file"]
    ext = os.path.splitext(f.filename or "")[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".webm"):
        return jsonify({"error": "Unsupported file type"}), 400

    # Persist the file FIRST so a missing in-memory job cannot discard a long upload.
    job_dir = _job_dir(job_id)
    os.makedirs(job_dir, exist_ok=True)
    media_path = os.path.join(job_dir, f"section_{idx:03d}{ext}")
    try:
        f.save(media_path)
    except OSError as e:
        return jsonify({"error": f"Could not save upload: {e}"}), 500

    job = _ensure_upload_job(job_id, idx)
    if not job:
        return jsonify({"error": "Job not found — reload the page or load audio again."}), 404

    _ensure_section_slot(job, idx)
    if 0 <= idx < len(job["sections"]):
        job["sections"][idx]["media"] = media_path
        job["sections"][idx]["media_ext"] = ext
    if job.get("audio_path") and os.path.exists(job["audio_path"]):
        if job.get("status") not in ("audio_ready", "done", "error_assembly", "assembling"):
            job["status"] = "audio_ready"
    elif job.get("status") in ("done", "error_assembly", None):
        job["status"] = "audio_ready"
    _persist_job(job_id)
    is_video = ext in (".mp4", ".mov", ".webm")
    return jsonify({
        "ok": True,
        "preview": f"/result/{job_id}/section_{idx:03d}{ext}",
        "is_video": is_video,
    })


@app.route("/assemble/<job_id>", methods=["POST"])
def assemble(job_id):
    job_id = (job_id or "").strip().lower()
    data = request.get_json(silent=True) or {}
    sections_payload = data.get("sections") or []

    job = _get_job(job_id, sections_hint=sections_payload)
    if not job:
        print(f"  [ytprod] assemble 404 job={job_id} OUTPUT_DIR={OUTPUT_DIR} "
              f"dir_exists={os.path.isdir(_job_dir(job_id))}", flush=True)
        return jsonify({
            "error": "Job not found — the server lost this session. "
                     "Reload the page (your audio may still be there), then Assemble again.",
        }), 404

    # Promote recoverable statuses so a hydrated "running" stub with narration
    # can still be assembled.
    if job.get("audio_path") and os.path.exists(job["audio_path"]):
        if job.get("status") not in ("audio_ready", "done", "error_assembly", "assembling"):
            job["status"] = "audio_ready"
    if job["status"] == "assembling":
        return jsonify({
            "error": "Assembly is already running. Wait for it to finish, or reload the page.",
        }), 409
    if job["status"] not in ("audio_ready", "done", "error_assembly"):
        return jsonify({"error": "Audio not ready yet — wait for narration, then try again."}), 400

    for cs in sections_payload:
        idx = cs.get("idx", -1)
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            continue
        _ensure_section_slot(job, idx)
        if 0 <= idx < len(job["sections"]):
            job["sections"][idx]["start"] = float(cs.get("start", job["sections"][idx]["start"]))
            job["sections"][idx]["end"]   = float(cs.get("end",   job["sections"][idx]["end"]))
        # Re-bind media from disk in case paths went stale after recycle.
        media, ext = _find_section_media(_job_dir(job_id), idx)
        if media and 0 <= idx < len(job["sections"]):
            job["sections"][idx]["media"] = media
            job["sections"][idx]["media_ext"] = ext

    job["xfade"]          = bool(data.get("xfade", True))
    job["music_vol"]      = float(data.get("music_vol", 0.15))
    style = (data.get("viz_style") or "none").strip().lower()
    placement = (data.get("viz_placement") or "full").strip().lower()
    job["viz_style"]      = style if style in VIZ_STYLES else "none"
    job["viz_placement"]  = placement if placement in VIZ_PLACEMENTS else "full"
    try:
        job["viz_opacity"] = max(0.2, min(1.0, float(data.get("viz_opacity", 0.9))))
    except (TypeError, ValueError):
        job["viz_opacity"] = 0.9
    if not job.get("music_path"):
        job["music_path"] = _find_music(_job_dir(job_id))
    job["status"]         = "assembling"
    job["assemble_queue"] = queue.Queue()
    _persist_job(job_id)
    threading.Thread(target=run_assembly, args=(job_id,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/upload-music/<job_id>", methods=["POST"])
def upload_music(job_id):
    job = _get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400
    f   = request.files["file"]
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in (".mp3", ".wav", ".aac", ".m4a", ".ogg", ".flac"):
        return jsonify({"error": "Unsupported audio format"}), 400
    job_dir    = os.path.join(OUTPUT_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    music_path = os.path.join(job_dir, f"music{ext}")
    f.save(music_path)
    job["music_path"] = music_path
    _persist_job(job_id)
    return jsonify({"ok": True, "filename": f.filename})


def _equal_sections(n: int, duration: float, texts: list | None = None) -> list:
    """Split narration into n equal timed sections (optional per-section text)."""
    n = max(2, min(20, int(n)))
    duration = max(float(duration or 0), 1.0)
    step = duration / n
    out = []
    for i in range(n):
        text = ""
        if texts and i < len(texts) and isinstance(texts[i], str):
            text = texts[i].strip()
        if not text:
            text = f"Section {i + 1}"
        out.append({
            "idx": i,
            "text": text,
            "start": round(i * step, 2),
            "end": round(duration if i == n - 1 else (i + 1) * step, 2),
            "media": None,
            "media_ext": None,
        })
    return out


@app.route("/load-audio", methods=["POST"])
def load_audio():
    """Load narration audio and build section cards.

    Modes (form field ``mode``):
      replace — keep existing sections; swap narration and rescale timings.
      split   — (default) load audio and generate ``sections`` count of cards.
                Script optional: with a long enough script, AI-splits text into
                that many sections; otherwise equal time slices.
    """
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400
    f = request.files["file"]
    if not f or not f.filename:
        return jsonify({"error": "No file"}), 400

    mode = (request.form.get("mode") or "split").strip().lower()
    job_id = (request.form.get("job_id") or "").strip()
    job = _get_job(job_id) if job_id else None
    script = (request.form.get("script") or "").strip()
    voice = request.form.get("voice") or "en-US-GuyNeural"
    title = (request.form.get("title") or "").strip()
    try:
        n_sections = max(2, min(20, int(request.form.get("sections") or 6)))
    except (TypeError, ValueError):
        n_sections = 6

    # ── Replace narration only (keep section count / media) ───────────
    if mode == "replace":
        if not job or not job.get("sections"):
            return jsonify({"error": "No existing job to replace audio on"}), 400
        if job.get("status") == "running":
            return jsonify({"error": "Wait for the current generation to finish"}), 400
        try:
            audio_path = _save_narration_upload(f, os.path.join(OUTPUT_DIR, job_id))
        except Exception as e:
            return jsonify({"error": str(e)}), 400
        job["audio_path"] = audio_path
        actual_dur = _audio_duration(audio_path)
        _rescale_sections(job["sections"], actual_dur)
        if job.get("status") in ("done", "error_assembly", "audio_ready", None):
            job["status"] = "audio_ready"
        _persist_job(job_id)
        return jsonify({
            "ok": True,
            "mode": "replace",
            "job_id": job_id,
            "audio_dur": round(actual_dur, 2),
            "sections": job["sections"],
            "title": job.get("title"),
            "description": job.get("description"),
            "hashtags": job.get("hashtags") or [],
        })

    # ── Split: load audio + generate N section cards ──────────────────
    # Prefer AI text split when a real script is present; otherwise equal slices.
    use_ai = len(script) >= 20

    if use_ai:
        job_id = uuid.uuid4().hex
        job_dir = os.path.join(OUTPUT_DIR, job_id)
        os.makedirs(job_dir, exist_ok=True)
        try:
            ext = os.path.splitext(f.filename)[1].lower() or ".mp3"
            if ext not in _AUDIO_UPLOAD_EXTS:
                return jsonify({"error": f"Unsupported audio format: {ext}"}), 400
            staged = os.path.join(job_dir, f"_upload_narration{ext}")
            f.save(staged)
        except Exception as e:
            return jsonify({"error": str(e)}), 400

        JOBS[job_id] = {
            "status": "running",
            "queue": queue.Queue(),
            "script": script,
            "voice": voice,
            "sections": [],
            "audio_path": None,
            "video_path": None,
        }
        _persist_job(job_id)
        threading.Thread(
            target=run_generation,
            args=(job_id, script, voice, title, n_sections, staged),
            daemon=True,
        ).start()
        return jsonify({"ok": True, "mode": "bootstrap", "job_id": job_id,
                        "sections_requested": n_sections})

    # No script — sync equal-duration sections from the uploaded audio.
    job_id = uuid.uuid4().hex
    job_dir = os.path.join(OUTPUT_DIR, job_id)
    try:
        audio_path = _save_narration_upload(f, job_dir)
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    actual_dur = _audio_duration(audio_path)
    if actual_dur <= 0:
        return jsonify({"error": "Could not read audio duration"}), 400
    sections = _equal_sections(n_sections, actual_dur)
    vid_title = title or "My Video"
    JOBS[job_id] = {
        "status": "audio_ready",
        "queue": queue.Queue(),
        "script": script,
        "voice": voice,
        "title": vid_title,
        "description": "",
        "hashtags": [],
        "sections": sections,
        "audio_path": audio_path,
        "video_path": None,
    }
    _persist_job(job_id)
    return jsonify({
        "ok": True,
        "mode": "split",
        "job_id": job_id,
        "audio_dur": round(actual_dur, 2),
        "sections": sections,
        "title": vid_title,
        "description": "",
        "hashtags": [],
    })


@app.route("/thumbnail/<job_id>", methods=["POST"])
def make_thumbnail(job_id):
    job = _get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    data    = request.get_json(silent=True) or {}
    sec_idx = int(data.get("section_idx", 0))
    title   = (data.get("title") or job.get("title") or "")[:80]
    sections = job.get("sections", [])
    if sec_idx >= len(sections):
        return jsonify({"error": "Invalid section"}), 400
    section  = sections[sec_idx]
    media    = section.get("media")
    ext      = (section.get("media_ext") or "").lower()
    if not media or not os.path.exists(media):
        return jsonify({"error": "No media uploaded for this section"}), 400
    job_dir  = os.path.join(OUTPUT_DIR, job_id)
    thumb_out = os.path.join(job_dir, "thumbnail.jpg")
    try:
        _build_thumbnail(media, title, thumb_out, is_video=ext in (".mp4", ".mov", ".webm"))
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    if not os.path.exists(thumb_out):
        return jsonify({"error": "Thumbnail generation failed"}), 500
    return jsonify({"ok": True, "preview": f"/result/{job_id}/thumbnail.jpg?t={int(time.time())}"})


@app.route("/assemble-progress/<job_id>")
def assemble_progress(job_id):
    job = _get_job(job_id)
    if not job:
        return "Not found", 404

    q = job.get("assemble_queue")
    if not q:
        return "Not found", 404

    return Response(_iter_sse(q), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/result/<job_id>/<file>")
def result_file(job_id, file):
    # Serving a job file is enough evidence the job dir exists — hydrate so
    # a later assemble/upload on this worker finds it in JOBS.
    _get_job(job_id)
    safe = re.sub(r"[^a-zA-Z0-9_.\-]", "", file)
    path = os.path.join(OUTPUT_DIR, job_id, safe)
    if not os.path.exists(path):
        return "File not found", 404
    return send_file(path)


@app.route("/download/<job_id>/<file>")
def download_file(job_id, file):
    _get_job(job_id)
    safe = re.sub(r"[^a-zA-Z0-9_.\-]", "", file)
    path = os.path.join(OUTPUT_DIR, job_id, safe)
    if not os.path.exists(path):
        return "File not found", 404
    return send_file(path, as_attachment=True)


# ─── frontend ─────────────────────────────────────────────────────────

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>YT Production</title>
<style>
:root{
  --ink:#0f1117;--panel:#181b22;--panel2:#1e2229;--panel3:#23272f;
  --line:#2a2e38;--lineb:#363b47;
  --text:#eae8e2;--muted:#8b909c;--faint:#4f5461;
  --amber:#ffb13c;--amber2:#7a5a22;
  --green:#56d364;--red:#f76d6d;
  --purple:#7c3aed;--purpled:#4c1d95;--purplel:#a78bfa;
  --blue:#2563eb;
  --r:12px;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;overflow:hidden;background:var(--ink);
  color:var(--text);font-family:system-ui,-apple-system,sans-serif;
  font-size:14px;-webkit-font-smoothing:antialiased}
body{display:flex;flex-direction:column}

/* ── layout ── */
.wrap{flex:1;display:flex;flex-direction:column;padding:10px 10px 0;min-height:0}
header{flex:none;display:flex;align-items:center;gap:10px;margin-bottom:10px}
.logo-mark{width:28px;height:28px;border:2px solid var(--amber);border-radius:6px;
  display:grid;place-items:center;color:var(--amber);font-size:13px;font-weight:700;flex:none}
h1{font-size:20px;font-weight:700;letter-spacing:-.02em}
.tag{margin-left:auto;color:var(--muted);font-size:11px;font-family:monospace}

.workspace{flex:1;min-height:0;display:grid;
  grid-template-columns:30fr 38fr 32fr;gap:8px;padding-bottom:10px}

.col{display:flex;flex-direction:column;background:var(--panel);
  border:1px solid var(--line);border-radius:var(--r);overflow:hidden;min-height:0}
.col-head{flex:none;padding:8px 14px;font-size:10px;letter-spacing:.08em;
  text-transform:uppercase;color:var(--muted);font-weight:700;
  border-bottom:1px solid var(--line);background:rgba(255,255,255,.02);
  display:flex;align-items:center;gap:6px}
.col-body{flex:1;overflow-y:auto;padding:12px;min-height:0;
  scrollbar-width:thin;scrollbar-color:var(--lineb) transparent}

/* fixed top + bottom zones in col2 */
.col2-audio{flex:none;padding:12px;border-bottom:1px solid var(--line)}
.col2-audio .audio-player-wrap{display:none}
.col2-audio.has-audio .audio-player-wrap{display:block}
.col2-audio.has-audio .audio-load-panel{display:none}
.audio-load-panel{display:flex;flex-direction:column;gap:8px}
.audio-load-row{display:flex;align-items:center;gap:8px}
.audio-load-row label{margin:0;flex:none}
.audio-load-row select{flex:1;margin:0;padding:7px 26px 7px 10px;font-size:12px}
.audio-load{border:1.5px dashed var(--lineb);border-radius:8px;padding:14px 12px;
  font-size:12px;color:var(--faint);cursor:pointer;text-align:center;
  transition:border-color .15s,background .15s;line-height:1.5}
.audio-load:hover,.audio-load.drag-over{border-color:var(--amber);
  background:rgba(255,177,60,.06);color:var(--amber)}
.audio-load input[type=file]{display:none}
.audio-replace{display:inline-flex;align-items:center;gap:5px;margin-top:7px;margin-left:8px;
  background:none;border:1px solid var(--lineb);color:var(--muted);
  border-radius:7px;padding:4px 10px;font-size:11px;font-family:monospace;
  cursor:pointer;transition:all .15s}
.audio-replace:hover{border-color:var(--amber);color:var(--amber)}
.col2-sections{flex:1;overflow-y:auto;padding:10px;min-height:0;
  scrollbar-width:thin;scrollbar-color:var(--lineb) transparent}
.col2-foot{flex:none;padding:10px 12px;border-top:1px solid var(--line);
  background:rgba(255,255,255,.015)}

footer{flex:none;text-align:center;font-size:10px;color:var(--faint);
  padding:5px 0 8px;font-family:monospace}

/* ── form controls ── */
label{display:block;font-size:10px;letter-spacing:.06em;text-transform:uppercase;
  color:var(--muted);margin-bottom:5px;font-weight:500}
input[type=text],select,textarea{width:100%;background:var(--ink);
  border:1px solid var(--lineb);color:var(--text);border-radius:8px;
  padding:8px 10px;font-size:13px;font-family:inherit;outline:none;
  transition:border-color .15s}
input[type=text]:focus,select:focus,textarea:focus{border-color:var(--amber)}
input::placeholder,textarea::placeholder{color:var(--faint)}
textarea{resize:vertical;min-height:200px;line-height:1.6}
select{appearance:none;cursor:pointer;
  background-image:linear-gradient(45deg,transparent 50%,var(--muted) 50%),
    linear-gradient(135deg,var(--muted) 50%,transparent 50%);
  background-position:calc(100% - 12px) center,calc(100% - 7px) center;
  background-size:4px 4px,4px 4px;background-repeat:no-repeat;padding-right:26px}
.field{margin-bottom:12px}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:8px}

/* ── buttons ── */
.btn-primary{width:100%;background:linear-gradient(135deg,var(--purpled),var(--purple));
  color:#e0d9ff;border:1px solid rgba(139,92,246,.4);border-radius:8px;
  padding:11px;font-size:13px;font-family:inherit;font-weight:700;
  cursor:pointer;transition:filter .15s;letter-spacing:.01em}
.btn-primary:hover{filter:brightness(1.15)}
.btn-primary:disabled{background:var(--panel3);color:var(--faint);
  border-color:var(--line);cursor:not-allowed;filter:none}
.btn-assemble{width:100%;background:linear-gradient(135deg,#065f46,#059669);
  color:#d1fae5;border:1px solid rgba(5,150,105,.4);border-radius:8px;
  padding:11px;font-size:13px;font-family:inherit;font-weight:700;
  cursor:pointer;transition:filter .15s;letter-spacing:.01em}
.btn-assemble:hover{filter:brightness(1.15)}
.btn-assemble:disabled{background:var(--panel3);color:var(--faint);
  border-color:var(--line);cursor:not-allowed;filter:none}
.dl-btn{display:inline-flex;align-items:center;gap:5px;margin-top:7px;
  background:var(--panel3);border:1px solid var(--lineb);color:var(--muted);
  border-radius:7px;padding:5px 12px;font-size:12px;font-family:monospace;
  text-decoration:none;transition:all .15s}
.dl-btn:hover{border-color:var(--amber);color:var(--amber)}
.copy-btn{background:none;border:1px solid var(--lineb);color:var(--muted);
  border-radius:6px;padding:3px 9px;font-size:11px;font-family:inherit;
  cursor:pointer;transition:all .15s;margin-top:5px;display:inline-block}
.copy-btn:hover{border-color:var(--amber);color:var(--amber)}

/* ── progress ── */
.progress-wrap{margin-top:12px;display:none}
.progress-wrap.show{display:block}
.prog-bar{height:4px;border-radius:3px;background:var(--ink);
  border:1px solid var(--line);overflow:hidden;margin-bottom:7px}
.prog-bar>i{display:block;height:100%;width:0;
  background:linear-gradient(90deg,var(--purple),var(--purplel));
  border-radius:3px;transition:width .4s}
.prog-bar.indet>i{width:35%;animation:slide 1.1s ease-in-out infinite}
@keyframes slide{0%{margin-left:-35%}100%{margin-left:100%}}
.prog-steps{display:flex;flex-direction:column;gap:4px;margin-top:6px}
.step{display:flex;align-items:center;gap:7px;font-size:11px;
  font-family:monospace;color:var(--faint);transition:color .2s}
.step.active{color:var(--amber)}
.step.done{color:var(--green)}
.step-ico{width:16px;text-align:center;flex:none}
.err-msg{color:var(--red);font-size:12px;font-family:monospace;
  margin-top:7px;display:none;line-height:1.5}
.err-msg.show{display:block}

/* ── audio area ── */
audio{width:100%;border-radius:7px;outline:none;margin-top:5px;margin-bottom:8px}

/* ── timeline bar ── */
.timeline{display:flex;height:32px;border-radius:7px;overflow:hidden;
  border:1px solid var(--line);cursor:pointer;margin-bottom:10px}
.tl-seg{height:100%;display:flex;align-items:center;justify-content:center;
  font-family:monospace;font-size:9px;color:rgba(255,255,255,.75);
  border-right:1px solid rgba(0,0,0,.25);transition:opacity .15s;
  min-width:10px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;
  padding:0 4px;user-select:none}
.tl-seg:last-child{border-right:none}
.tl-seg:hover{opacity:.8}

/* ── section cards ── */
.sec-card{background:var(--panel2);border:1px solid var(--line);
  border-radius:9px;margin-bottom:7px;overflow:hidden}
.sec-head{display:flex;align-items:center;gap:8px;padding:7px 10px;
  border-bottom:1px solid var(--line);background:rgba(255,255,255,.02)}
.sec-num{font-family:monospace;font-size:10px;font-weight:700;flex:none}
.sec-times{display:flex;align-items:center;gap:4px;margin-left:auto;font-family:monospace}
.time-inp{width:52px;background:var(--ink);border:1px solid var(--lineb);
  color:var(--text);border-radius:5px;padding:2px 4px;font-family:monospace;
  font-size:11px;text-align:center;outline:none;transition:border-color .15s}
.time-inp:focus{border-color:var(--amber)}
.sec-arrow{color:var(--faint);font-size:11px}
.sec-text{padding:6px 10px;font-size:11px;color:var(--muted);line-height:1.5;
  border-bottom:1px solid var(--line);max-height:44px;overflow:hidden;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.sec-upload{padding:8px 10px}
.drop-zone{border:1.5px dashed var(--lineb);border-radius:7px;
  padding:12px 8px;text-align:center;color:var(--faint);font-size:11px;
  cursor:pointer;transition:border-color .15s,background .15s;line-height:1.6;
  display:block;width:100%;box-sizing:border-box}
.drop-zone:hover,.drop-zone.drag-over{border-color:var(--amber);
  background:rgba(255,177,60,.06);color:var(--amber)}
.drop-zone input[type=file]{display:none}
.sec-preview-wrap{position:relative;border-radius:6px;overflow:hidden}
.sec-preview{width:100%;max-height:96px;object-fit:cover;display:block;border-radius:6px}
.sec-preview-badge{position:absolute;top:4px;right:4px;background:rgba(0,0,0,.7);
  color:#fff;font-size:9px;font-family:monospace;border-radius:4px;padding:1px 5px}
.sec-preview-change{display:block;margin-top:4px;font-size:10px;color:var(--faint);
  text-align:center;cursor:pointer;transition:color .15s}
.sec-preview-change:hover{color:var(--amber)}
.sec-ready{border-color:var(--green) !important}
.upload-err{color:var(--red);font-size:10px;margin-top:3px;display:none}
.upload-err.show{display:block}

/* ── assembly progress ── */
.assemble-prog{margin-top:8px;display:none}
.assemble-prog.show{display:block}
.assemble-bar{height:4px;border-radius:3px;background:var(--ink);
  border:1px solid var(--line);overflow:hidden;margin-bottom:5px}
.assemble-bar>i{display:block;height:100%;width:0;
  background:linear-gradient(90deg,#059669,#34d399);
  border-radius:3px;transition:width .4s}
.assemble-bar.indet>i{width:35%;animation:slide 1.1s ease-in-out infinite}
.assemble-msg{font-size:11px;font-family:monospace;color:var(--muted)}

/* ── col3 preview / publish ── */
.placeholder{color:var(--faint);font-size:12px;font-family:monospace;
  text-align:center;padding:40px 16px;line-height:1.9}
video{width:100%;border-radius:8px;background:#000;display:block}
.section-hidden{display:none}
.res-label{font-size:10px;letter-spacing:.07em;text-transform:uppercase;
  color:var(--muted);font-weight:700;margin-bottom:5px;margin-top:14px}
.res-text{background:var(--ink);border:1px solid var(--line);border-radius:8px;
  padding:9px 11px;font-size:12px;line-height:1.6;color:var(--text);
  white-space:pre-wrap;word-break:break-word;max-height:180px;overflow-y:auto}
.htag-row{display:flex;flex-wrap:wrap;gap:5px;margin-top:5px}
.htag{background:rgba(124,58,237,.15);border:1px solid rgba(124,58,237,.3);
  color:var(--purplel);border-radius:20px;padding:2px 9px;font-size:11px;
  font-family:monospace;cursor:pointer;transition:all .15s}
.htag:hover{background:rgba(124,58,237,.3)}

/* ── music zone ── */
.music-zone{margin-bottom:7px}
.music-drop{border:1.5px dashed var(--lineb);border-radius:7px;padding:6px 10px;
  font-size:11px;color:var(--faint);cursor:pointer;text-align:center;
  transition:border-color .15s,background .15s}
.music-drop:hover,.music-drop.drag-over{border-color:var(--amber);
  background:rgba(255,177,60,.06);color:var(--amber)}
.music-drop.ready{border-color:var(--green);border-style:solid;color:var(--green)}
.music-controls{display:none;align-items:center;gap:6px;margin-top:5px;
  padding:4px 8px;background:rgba(86,211,100,.06);border-radius:6px;
  border:1px solid rgba(86,211,100,.2)}
.music-controls.show{display:flex}
.music-controls input[type=range]{-webkit-appearance:none;height:3px;
  background:var(--lineb);border-radius:2px;outline:none;cursor:pointer;flex:1}
.music-controls input[type=range]::-webkit-slider-thumb{
  -webkit-appearance:none;width:12px;height:12px;border-radius:50%;
  background:var(--amber);cursor:pointer}

/* ── voice picker ── */
.voice-pick{display:flex;gap:5px}
.voice-pick select{flex:1;min-width:0}
.voice-play{flex:none;width:30px;background:var(--panel3);border:1px solid var(--lineb);
  color:var(--muted);border-radius:6px;cursor:pointer;font-size:11px}
.voice-play:hover{color:var(--amber);border-color:var(--amber)}
.voice-play.busy{opacity:.5;cursor:wait}

/* ── visualizer ── */
.viz-zone{margin-bottom:8px}
.viz-head{font-size:11px;color:var(--muted);margin-bottom:5px}
.viz-styles{display:flex;gap:5px;flex-wrap:wrap}
.viz-chip{display:flex;align-items:center;gap:5px;background:var(--ink);
  border:1px solid var(--lineb);color:var(--muted);border-radius:6px;
  padding:4px 8px;font-size:11px;cursor:pointer}
.viz-chip:hover{border-color:var(--purplel)}
.viz-chip.on{border-color:var(--purple);color:#fff;background:rgba(124,58,237,.18)}
.viz-chip .sw{display:inline-block;width:14px;height:10px;border-radius:2px}
.sw-neon{background:linear-gradient(90deg,#ff5a1f,#b03cff,#1e90ff)}
.sw-bars{background:repeating-linear-gradient(90deg,#d24bff 0 2px,transparent 2px 4px),
  linear-gradient(#2f7bff,#d24bff)}
.sw-aurora{background:linear-gradient(#d4ff3a,#ff3fa4,#7a3cff,#2aa8ff)}
.viz-opts{display:none;align-items:center;gap:6px;margin-top:6px}
.viz-opts.show{display:flex}
.viz-opts select{width:auto;flex:none;padding:3px 24px 3px 7px;font-size:11px}
.viz-opts input[type=range]{flex:1;accent-color:var(--purple)}
.viz-note{display:none;font-size:10px;color:var(--faint);margin-top:5px;line-height:1.45}
.viz-note.show{display:block}

/* ── xfade row ── */
.xfade-row{display:flex;align-items:center;gap:7px;margin-bottom:8px;
  font-size:11px;color:var(--muted);cursor:pointer;user-select:none}
.xfade-row input[type=checkbox]{width:13px;height:13px;accent-color:var(--green);cursor:pointer}

/* ── thumbnail builder ── */
.thumb-picker{display:flex;gap:5px;flex-wrap:wrap;min-height:24px}
.tpick{width:64px;height:42px;border-radius:5px;cursor:pointer;object-fit:cover;
  border:2px solid transparent;transition:border-color .15s;flex:none}
.tpick.sel{border-color:var(--amber)}
.tpick:hover{border-color:var(--muted)}
.btn-thumb{width:100%;background:var(--panel3);border:1px solid var(--lineb);
  color:var(--muted);border-radius:7px;padding:8px;font-size:12px;
  font-family:inherit;cursor:pointer;transition:all .15s;margin-top:4px}
.btn-thumb:hover{border-color:var(--amber);color:var(--amber)}
.btn-thumb:disabled{opacity:.5;cursor:not-allowed}
</style>
</head>
<body>
<!-- Platform strip (vendored copy only): way back to the Tuber Platform when
     mounted at /produce/. These hrefs are platform routes, deliberately absent
     from the PrefixRewriteMiddleware whitelist so they stay root-relative. -->
<div style="display:flex;align-items:center;gap:16px;padding:8px 20px;background:#020308;border-bottom:1px solid #262a33;font-family:system-ui,sans-serif;font-size:13px">
  <a href="/" style="display:flex;align-items:center;gap:8px;color:#e9e7e1;text-decoration:none;font-weight:700"><img src="/static/brand/mark.png" alt="" style="height:20px"><span>Money<span style="color:#e5111c">Tuber</span></span></a>
  <span style="color:#fff;background:rgba(229,17,28,.18);padding:4px 10px;border-radius:8px;font-weight:600">Production Studio</span>
  <a href="/producer-scout/" style="color:#8b909c;text-decoration:none;font-weight:600">Video to Sections</a>
  <span style="flex:1"></span>
  <a href="/logout" style="color:#8b909c;text-decoration:none;font-weight:600">&#8594; Log out</a>
</div>
<div class="wrap">
<header>
  <div class="logo-mark">&#9654;</div>
  <h1>Production Studio</h1>
  <span class="tag">script &middot; audio &middot; sections &middot; video</span>
</header>

<div class="workspace">

<!-- ══════════ COL 1: SCRIPT ══════════ -->
<div class="col">
  <div class="col-head">&#9998; Script</div>
  <div class="col-body">
    <div class="field">
      <label>Video title (optional)</label>
      <input type="text" id="vidTitle" placeholder="Leave blank to auto-generate">
    </div>
    <div class="row2">
      <div class="field">
        <label>Voice</label>
        <div class="voice-pick">
        <select id="voice">
          <optgroup label="English (US) · featured">
            <option value="en-US-AndrewNeural">Andrew · M, warm & confident</option>
            <option value="en-US-BrianNeural">Brian · M, casual & sincere</option>
            <option value="en-US-AvaNeural">Ava · F, expressive & caring</option>
            <option value="en-US-EmmaNeural">Emma · F, cheerful & clear</option>
            <option value="en-US-GuyNeural" selected>Guy · M, passionate news</option>
            <option value="en-US-ChristopherNeural">Christopher · M, authority</option>
          </optgroup>
          <optgroup label="English (US) · more">
            <option value="en-US-AriaNeural">Aria · F, confident</option>
            <option value="en-US-JennyNeural">Jenny · F, friendly</option>
            <option value="en-US-MichelleNeural">Michelle · F, pleasant</option>
            <option value="en-US-EricNeural">Eric · M, rational</option>
            <option value="en-US-RogerNeural">Roger · M, lively</option>
            <option value="en-US-SteffanNeural">Steffan · M, rational</option>
            <option value="en-US-AnaNeural">Ana · F, child</option>
          </optgroup>
          <optgroup label="Multilingual (reads other languages too)">
            <option value="en-US-AndrewMultilingualNeural">Andrew · M, multilingual</option>
            <option value="en-US-BrianMultilingualNeural">Brian · M, multilingual</option>
            <option value="en-US-AvaMultilingualNeural">Ava · F, multilingual</option>
            <option value="en-US-EmmaMultilingualNeural">Emma · F, multilingual</option>
            <option value="en-AU-WilliamMultilingualNeural">William · M, Australian multilingual</option>
          </optgroup>
          <optgroup label="Africa">
            <option value="en-NG-AbeoNeural">Abeo · M, Nigerian</option>
            <option value="en-NG-EzinneNeural">Ezinne · F, Nigerian</option>
            <option value="en-KE-ChilembaNeural">Chilemba · M, Kenyan</option>
            <option value="en-KE-AsiliaNeural">Asilia · F, Kenyan</option>
            <option value="en-ZA-LukeNeural">Luke · M, South African</option>
            <option value="en-ZA-LeahNeural">Leah · F, South African</option>
            <option value="en-TZ-ElimuNeural">Elimu · M, Tanzanian</option>
            <option value="en-TZ-ImaniNeural">Imani · F, Tanzanian</option>
          </optgroup>
          <optgroup label="UK & Ireland">
            <option value="en-GB-RyanNeural">Ryan · M, British</option>
            <option value="en-GB-ThomasNeural">Thomas · M, British</option>
            <option value="en-GB-SoniaNeural">Sonia · F, British</option>
            <option value="en-GB-LibbyNeural">Libby · F, British</option>
            <option value="en-GB-MaisieNeural">Maisie · F, British child</option>
            <option value="en-IE-ConnorNeural">Connor · M, Irish</option>
            <option value="en-IE-EmilyNeural">Emily · F, Irish</option>
          </optgroup>
          <optgroup label="Americas, Asia & Pacific">
            <option value="en-CA-LiamNeural">Liam · M, Canadian</option>
            <option value="en-CA-ClaraNeural">Clara · F, Canadian</option>
            <option value="en-AU-NatashaNeural">Natasha · F, Australian</option>
            <option value="en-NZ-MitchellNeural">Mitchell · M, New Zealand</option>
            <option value="en-NZ-MollyNeural">Molly · F, New Zealand</option>
            <option value="en-IN-PrabhatNeural">Prabhat · M, Indian</option>
            <option value="en-IN-NeerjaExpressiveNeural">Neerja · F, Indian expressive</option>
            <option value="en-SG-WayneNeural">Wayne · M, Singaporean</option>
            <option value="en-SG-LunaNeural">Luna · F, Singaporean</option>
            <option value="en-PH-JamesNeural">James · M, Filipino</option>
            <option value="en-PH-RosaNeural">Rosa · F, Filipino</option>
            <option value="en-HK-SamNeural">Sam · M, Hong Kong</option>
            <option value="en-HK-YanNeural">Yan · F, Hong Kong</option>
          </optgroup>
        </select>
        <button type="button" class="voice-play" id="voicePlay" title="Preview this voice">&#9654;</button>
        </div>
      </div>
      <div class="field">
        <label>Sections</label>
        <select id="nsections">
          <option value="3">3 sections</option>
          <option value="4">4 sections</option>
          <option value="5">5 sections</option>
          <option value="6" selected>6 sections</option>
          <option value="7">7 sections</option>
          <option value="8">8 sections</option>
          <option value="9">9 sections</option>
          <option value="10">10 sections</option>
          <option value="12">12 sections</option>
          <option value="14">14 sections</option>
          <option value="16">16 sections</option>
          <option value="18">18 sections</option>
          <option value="20">20 sections</option>
        </select>
      </div>
    </div>
    <div class="row2">
      <div class="field">
        <label>Speed</label>
        <select id="voiceRate">
          <option value="-20%">Slow · −20%</option>
          <option value="-10%">Relaxed · −10%</option>
          <option value="+0%" selected>Normal</option>
          <option value="+10%">Brisk · +10%</option>
          <option value="+20%">Fast · +20%</option>
        </select>
      </div>
      <div class="field">
        <label>Pitch</label>
        <select id="voicePitch">
          <option value="-10Hz">Deeper</option>
          <option value="-5Hz">Slightly deeper</option>
          <option value="+0Hz" selected>Natural</option>
          <option value="+5Hz">Slightly higher</option>
          <option value="+10Hz">Higher</option>
        </select>
      </div>
    </div>
    <div class="field">
      <label>Script</label>
      <textarea id="script" placeholder="Paste your full video script here&#10;&#10;Each paragraph becomes a timed section. You'll upload your own image or video for each section."></textarea>
    </div>
    <button class="btn-primary" id="genBtn">&#9654; Generate Audio</button>
    <div class="progress-wrap" id="progressWrap">
      <div class="prog-bar indet" id="progressBar"><i></i></div>
      <div class="prog-steps" id="progressSteps"></div>
      <div class="err-msg" id="errMsg"></div>
    </div>
  </div>
</div>

<!-- ══════════ COL 2: AUDIO + SECTIONS ══════════ -->
<div class="col" id="col2">
  <div class="col-head">&#9670; Audio &amp; Sections</div>

  <!-- fixed audio + timeline zone — always visible so Load audio is available -->
  <div class="col2-audio" id="audioArea">
    <div class="audio-load-panel" id="audioLoadPanel">
      <div class="audio-load-row">
        <label for="loadSections">Sections</label>
        <select id="loadSections" title="How many section cards to generate for this audio">
          <option value="3">3 sections</option>
          <option value="4">4 sections</option>
          <option value="5">5 sections</option>
          <option value="6" selected>6 sections</option>
          <option value="7">7 sections</option>
          <option value="8">8 sections</option>
          <option value="9">9 sections</option>
          <option value="10">10 sections</option>
          <option value="12">12 sections</option>
          <option value="14">14 sections</option>
          <option value="16">16 sections</option>
          <option value="18">18 sections</option>
          <option value="20">20 sections</option>
        </select>
      </div>
      <div class="audio-load" id="audioLoad"
           onclick="var el=$('audioFile'); if(el) el.click()"
           ondragover="event.preventDefault();this.classList.add('drag-over')"
           ondragleave="this.classList.remove('drag-over')"
           ondrop="onAudioLoadDrop(event)">
        <input type="file" id="audioFile" accept=".mp3,.wav,.aac,.m4a,.ogg,.flac"
               onchange="loadNarrationAudio(this.files[0])">
        &#128266; Load narration audio<br>
        <span style="color:var(--faint);font-size:10px">Pick section count above, then drop MP3/WAV/M4A</span>
      </div>
    </div>
    <div class="audio-player-wrap" id="audioPlayerWrap">
      <audio id="audioPlayer" controls></audio>
      <div class="timeline" id="timeline"></div>
      <a class="dl-btn" id="audioDl" href="#" download style="font-size:11px">&#8595; Download MP3</a>
      <button type="button" class="audio-replace" id="audioReplaceBtn"
              onclick="var el=$('replaceAudioFile'); if(el) el.click()">&#8635; Replace audio</button>
      <input type="file" id="replaceAudioFile" accept=".mp3,.wav,.aac,.m4a,.ogg,.flac"
             onchange="replaceNarrationAudio(this.files[0])" style="display:none">
    </div>
    <div id="prefillNote" style="display:none;font-size:11px;color:var(--faint);margin-top:6px"></div>
  </div>

  <!-- scrollable section cards -->
  <div class="col2-sections" id="sectionsArea">
    <div class="placeholder" id="sec-placeholder">
      Load audio and choose a section count,<br>or generate audio from a script.
    </div>
  </div>

  <!-- sticky assemble footer -->
  <div class="col2-foot">
    <div class="music-zone">
      <div class="music-drop" id="musicDrop" onclick="$('musicFile').click()"
           ondragover="event.preventDefault();this.classList.add('drag-over')"
           ondragleave="this.classList.remove('drag-over')"
           ondrop="onMusicDrop(event)">
        <input type="file" id="musicFile" accept=".mp3,.wav,.aac,.m4a,.ogg"
               onchange="uploadMusic(this.files[0])" style="display:none">
        <span id="musicLabel">&#127925; Background music (optional) — drop MP3/WAV</span>
      </div>
      <div class="music-controls" id="musicControls">
        <span style="font-size:10px;color:var(--muted);flex:none">Vol</span>
        <input type="range" id="musicVol" min="5" max="40" value="15"
               oninput="$('musicVolPct').textContent=this.value+'%'">
        <span id="musicVolPct" style="font-size:10px;font-family:monospace;color:var(--muted);width:28px;flex:none">15%</span>
        <span onclick="clearMusic()" style="cursor:pointer;color:var(--faint);font-size:13px;flex:none;padding:0 2px" title="Remove music">&#10005;</span>
      </div>
    </div>
    <div class="viz-zone">
      <div class="viz-head">&#127926; Audio visualizer</div>
      <div class="viz-styles" id="vizStyles">
        <button type="button" class="viz-chip on" data-style="none">Off</button>
        <button type="button" class="viz-chip" data-style="neon"><i class="sw sw-neon"></i>Neon waves</button>
        <button type="button" class="viz-chip" data-style="bars"><i class="sw sw-bars"></i>LED bars</button>
        <button type="button" class="viz-chip" data-style="aurora"><i class="sw sw-aurora"></i>Aurora</button>
      </div>
      <div class="viz-opts" id="vizOpts">
        <select id="vizPlacement" title="Where the visualizer sits">
          <option value="full">Full frame</option>
          <option value="band">Bottom band</option>
        </select>
        <span style="font-size:10px;color:var(--muted);flex:none">Glow</span>
        <input type="range" id="vizOpacity" min="30" max="100" value="90"
               oninput="$('vizOpacityPct').textContent=this.value+'%'">
        <span id="vizOpacityPct" style="font-size:10px;font-family:monospace;color:var(--muted);width:30px;flex:none">90%</span>
      </div>
      <div class="viz-note" id="vizNote">Driven by your narration + music. Sections without media show the visualizer on black. Adds roughly the video's length to assembly time.</div>
    </div>
    <label class="xfade-row">
      <input type="checkbox" id="xfadeToggle" checked>
      <span>Crossfade transitions between sections</span>
    </label>
    <button class="btn-assemble" id="assembleBtn" disabled>&#9654; Assemble Final Video</button>
    <div class="assemble-prog" id="assembleProgWrap">
      <div class="assemble-bar indet" id="assembleBar"><i></i></div>
      <div class="assemble-msg" id="assembleMsg">Starting…</div>
    </div>
  </div>
</div>

<!-- ══════════ COL 3: PREVIEW & EXPORT ══════════ -->
<div class="col">
  <div class="col-head">&#9654; Preview &amp; Export</div>
  <div class="col-body">
    <div class="placeholder" id="previewPlaceholder">
      Assemble your video to preview and download it here.
    </div>

    <div id="videoSection" class="section-hidden">
      <video id="videoPlayer" controls></video>
      <a class="dl-btn" id="videoDl" href="#" download>&#8595; Download MP4</a>
    </div>

    <div id="chaptersSection" class="section-hidden">
      <div class="res-label">&#128214; YouTube Chapters</div>
      <div class="res-text" id="chaptersOut" style="font-family:monospace;font-size:11px;line-height:1.8"></div>
      <button class="copy-btn" onclick="copyEl('chaptersOut',this)">Copy chapters</button>
    </div>

    <div id="thumbnailSection" class="section-hidden">
      <div class="res-label">&#128247; Thumbnail Builder</div>
      <div class="thumb-picker" id="thumbPicker"></div>
      <div class="field" style="margin-top:6px">
        <input type="text" id="thumbTitle" placeholder="Thumbnail title text…">
      </div>
      <button class="btn-thumb" id="thumbBtn">&#128247; Build Thumbnail</button>
      <div id="thumbPreviewWrap" style="display:none;margin-top:8px">
        <img id="thumbImg" style="width:100%;border-radius:7px;border:1px solid var(--line);display:block">
        <a class="dl-btn" id="thumbDl" href="#" download>&#8595; Download Thumbnail</a>
      </div>
    </div>

    <div id="publishSection" class="section-hidden">
      <div class="res-label">&#127916; Video Title</div>
      <div class="res-text" id="titleOut"></div>
      <button class="copy-btn" onclick="copyEl('titleOut',this)">Copy title</button>

      <div class="res-label">&#128203; Description</div>
      <div class="res-text" id="descOut"></div>
      <button class="copy-btn" onclick="copyEl('descOut',this)">Copy description</button>

      <div class="res-label">&#35; Hashtags</div>
      <div class="htag-row" id="hashRow"></div>
      <button class="copy-btn" id="copyHashBtn" style="margin-top:8px">Copy all hashtags</button>
    </div>
  </div>
</div>

</div><!-- /workspace -->
<footer>runs locally &middot; groq &middot; edge-tts &middot; ffmpeg &middot; your media</footer>
</div>

<script>
const $ = id => document.getElementById(id);
// Inline onclick handlers resolve against window — expose $ so
// $('fi-0').click() etc. work reliably (const alone is not on window).
window.$ = $;

// ── section colours ─────────────────────────────────────────────────
const SEC_COLS = [
  '#7c3aed','#2563eb','#059669','#d97706','#dc2626','#0891b2',
  '#7c3aed','#2563eb','#059669','#d97706',
];

// ── state ───────────────────────────────────────────────────────────
let _jobId      = null;
let _sections   = [];  // [{idx, text, start, end, media_ready}]
let _audioDur   = 0;
let _hashtags   = [];

// ── time helpers ─────────────────────────────────────────────────────
function fmtTime(s) {
  s = Math.max(0, Math.round(s));
  const m = Math.floor(s / 60), sec = s % 60;
  return `${m}:${String(sec).padStart(2,'0')}`;
}
function parseTime(str) {
  str = str.trim();
  if (str.includes(':')) {
    const [m,s] = str.split(':').map(Number);
    return (m||0)*60 + (s||0);
  }
  return parseFloat(str) || 0;
}

// ── progress steps ───────────────────────────────────────────────────
const GEN_STEPS = [
  {key:'analysing', ico:'&#129302;', label:'Analysing script'},
  {key:'audio',     ico:'&#128266;', label:'Generating audio'},
];

function buildGenSteps() {
  const wrap = $('progressSteps');
  wrap.innerHTML = '';
  GEN_STEPS.forEach(s => {
    const d = document.createElement('div');
    d.className = 'step'; d.id = 'gstep_'+s.key;
    d.innerHTML = `<span class="step-ico">${s.ico}</span>${s.label}`;
    wrap.appendChild(d);
  });
}

function markStep(key, done=false) {
  GEN_STEPS.forEach(s => {
    const el = $('gstep_'+s.key);
    if (!el) return;
    el.classList.toggle('active', s.key===key && !done);
    if (done && s.key===key) el.classList.add('done');
  });
}

function showErr(msg) {
  $('errMsg').textContent = 'Error: '+msg;
  $('errMsg').classList.add('show');
  $('progressBar').classList.remove('indet');
  $('genBtn').disabled = false;
}

function copyEl(id, btn) {
  navigator.clipboard.writeText($(id).textContent);
  const orig = btn.textContent;
  btn.textContent = 'Copied ✓';
  setTimeout(() => btn.textContent = orig, 1500);
}

// ── job persistence (URL + localStorage) ─────────────────────────────
const YTPROD_JOB_KEY = 'ytprod_last_job';

function rememberJob(jobId) {
  if (!jobId) return;
  _jobId = jobId;
  try { localStorage.setItem(YTPROD_JOB_KEY, jobId); } catch (e) {}
  try {
    const url = new URL(window.location.href);
    if (url.searchParams.get('job') !== jobId) {
      url.searchParams.set('job', jobId);
      history.replaceState(null, '', url.pathname + url.search + url.hash);
    }
  } catch (e) {}
}

function clearRememberedJob() {
  try { localStorage.removeItem(YTPROD_JOB_KEY); } catch (e) {}
  try {
    const url = new URL(window.location.href);
    if (url.searchParams.has('job')) {
      url.searchParams.delete('job');
      history.replaceState(null, '', url.pathname + url.search + url.hash);
    }
  } catch (e) {}
}

function restoreFormFromJob(d) {
  if (d.script && !$('script').value.trim()) $('script').value = d.script;
  if (d.title && !$('vidTitle').value.trim()) $('vidTitle').value = d.title;
  if (d.voice) {
    const v = $('voice');
    if ([...v.options].some(o => o.value === d.voice)) v.value = d.voice;
  }
  for (const [id, val] of [['voiceRate', d.voice_rate], ['voicePitch', d.voice_pitch],
                           ['vizPlacement', d.viz_placement]]) {
    const el = $(id);
    if (val && [...el.options].some(o => o.value === val)) el.value = val;
  }
  if (d.viz_style) setVizStyle(d.viz_style);
  if (d.viz_opacity != null) {
    $('vizOpacity').value = Math.round(Number(d.viz_opacity) * 100);
    $('vizOpacityPct').textContent = $('vizOpacity').value + '%';
  }
  if (d.sections && d.sections.length) {
    const n = String(d.sections.length);
    const sel = $('nsections');
    if ([...sel.options].some(o => o.value === n)) sel.value = n;
    const loadSel = $('loadSections');
    if (loadSel && [...loadSel.options].some(o => o.value === n)) loadSel.value = n;
  }
  if (d.has_music) {
    _musicReady = true;
    $('musicLabel').textContent = '♪ Background music loaded';
    $('musicDrop').classList.add('ready');
    $('musicControls').classList.add('show');
    if (d.music_vol != null) {
      const pct = Math.round(Number(d.music_vol) * 100);
      $('musicVol').value = Math.max(5, Math.min(40, pct));
      $('musicVolPct').textContent = $('musicVol').value + '%';
    }
  }
}

// ── generate audio ───────────────────────────────────────────────────
$('genBtn').addEventListener('click', async () => {
  const script = $('script').value.trim();
  if (!script) { alert('Paste your script first.'); return; }

  // reset
  clearRememberedJob();
  _jobId = null; _sections = []; _audioDur = 0; _hashtags = [];
  $('genBtn').disabled = true;
  $('progressWrap').classList.add('show');
  $('errMsg').classList.remove('show');
  $('progressBar').classList.add('indet');
  $('audioArea').classList.remove('has-audio');
  const _ph = $('sec-placeholder');
  $('sectionsArea').innerHTML = '';
  _ph.style.display = 'block';
  $('sectionsArea').appendChild(_ph);
  $('assembleBtn').disabled = true;
  $('assembleProgWrap').classList.remove('show');
  $('previewPlaceholder').style.display = 'block';
  $('videoSection').classList.add('section-hidden');
  $('publishSection').classList.add('section-hidden');
  buildGenSteps();

  try {
    const r = await fetch('/generate', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        script,
        voice:    $('voice').value,
        rate:     $('voiceRate').value,
        pitch:    $('voicePitch').value,
        title:    $('vidTitle').value.trim(),
        sections: $('nsections').value,
      }),
    });
    const d = await r.json();
    if (d.error) { showErr(d.error); return; }
    rememberJob(d.job_id);
    streamGenProgress(d.job_id);
  } catch(e) { showErr('Server error: '+e.message); }
});

// Live progress (SSE) goes quiet for minutes during narration and ffmpeg.
// Proxies close that idle connection. Polling /job-state picks the job back
// up instead of treating the drop as a failure and inviting a second run.
function pollJob(jobId, opts) {
  let tries = 0;
  const maxTries = opts.maxTries || 180;
  const timer = setInterval(async () => {
    tries++;
    let d = null;
    let status = 0;
    try {
      const r = await fetch('/job-state/'+jobId);
      status = r.status;
      d = await r.json();
    } catch (e) {
      d = null;
    }
    if (d && opts.failed(d)) {
      clearInterval(timer);
      const msg = opts.failMessage
        ? opts.failMessage(d)
        : (d.error_message || 'This job failed.');
      opts.onFail(msg);
      return;
    }
    if (d && opts.ready(d)) {
      clearInterval(timer);
      opts.onReady(d);
      return;
    }
    if ((!d || d.error) && status === 404 && tries > 6) {
      clearInterval(timer);
      opts.onFail('Job not found — reload the page and try again.');
      return;
    }
    if (tries >= maxTries) {
      clearInterval(timer);
      opts.onFail('Still working, but this page lost live updates. Reload to resume.');
    }
  }, 5000);
}

function streamGenProgress(jobId) {
  let settled = false;
  const es = new EventSource('/progress/'+jobId);
  const finishOk = (d) => {
    if (settled) return;
    settled = true;
    try { es.close(); } catch (e) {}
    markStep('audio', true);
    $('progressBar').classList.remove('indet');
    $('progressBar').querySelector('i').style.width = '100%';
    $('errMsg').classList.remove('show');
    $('genBtn').disabled = false;
    onAudioReady(jobId, d);
  };
  const finishErr = (msg) => {
    if (settled) return;
    settled = true;
    try { es.close(); } catch (e) {}
    showErr(msg || 'Unknown error');
  };
  es.onmessage = ev => {
    let d;
    try { d = JSON.parse(ev.data); } catch (e) { return; }
    if (d.phase === 'analysing') markStep('analysing');
    else if (d.phase === 'analysed') markStep('analysing', true);
    else if (d.phase === 'audio') markStep('audio');
    else if (d.phase === 'audio_done') finishOk(d);
    else if (d.phase === 'error') finishErr(d.msg || 'Unknown error');
  };
  es.onerror = () => {
    if (settled || es._polling) return;
    es._polling = true;
    try { es.close(); } catch (e) {}
    $('errMsg').textContent = 'Connection dropped — checking whether audio is still generating…';
    $('errMsg').classList.add('show');
    pollJob(jobId, {
      ready: (d) => d.status === 'audio_ready' || d.status === 'done',
      failed: (d) => d.status === 'error' || d.status === 'error_assembly',
      onReady: finishOk,
      onFail: finishErr,
    });
  };
}

// ── after audio ready ────────────────────────────────────────────────
function onAudioReady(jobId, data) {
  rememberJob(jobId);
  restoreFormFromJob(data);
  _audioDur   = data.audio_dur || 0;
  // media_ready/ext reflect whatever the server already has for this section —
  // e.g. pre-filled by the producer_scout video-to-sections flow, which cuts
  // a matching clip from the source video before narration even finishes.
  // Manually-typed-script jobs still start every section with media: null,
  // so this is a no-op for that flow (media_ready stays false as before).
  _sections   = (data.sections || []).map(s => ({
    ...s,
    media_ready: !!s.media,
    ext: s.media_ext || null,
  }));
  _hashtags   = data.hashtags  || [];
  JOBS_title  = data.title || '';

  // audio player
  $('audioPlayer').src = `/result/${jobId}/narration.mp3?t=${Date.now()}`;
  $('audioDl').href    = `/download/${jobId}/narration.mp3`;
  $('audioArea').classList.add('has-audio');

  // timeline
  buildTimeline(_sections, _audioDur);

  // section cards — clear first so replace/reload doesn't duplicate
  const box = $('sectionsArea');
  box.innerHTML = '';
  const _phEl = document.createElement('div');
  _phEl.className = 'placeholder';
  _phEl.id = 'sec-placeholder';
  _phEl.style.display = 'none';
  box.appendChild(_phEl);
  _sections.forEach(s => buildSectionCard(jobId, s));

  // publish panel meta (shown later after assemble)
  $('titleOut').textContent = data.title       || '';
  $('descOut').textContent  = data.description || '';
  buildHashtags(data.hashtags || []);

  $('assembleBtn').disabled = false;

  // Clips extracted from a source video (producer_scout flow) usually finish
  // AFTER the narration audio does — keep syncing them in live rather than
  // making the producer refresh. First poll no-ops and stops for jobs with
  // no prefill running (typed-script flow).
  pollClipPrefill(jobId);
}

// ── load / replace narration audio ───────────────────────────────────
// Keep Script-column and Audio-column section counts in sync.
(function syncSectionSelects(){
  const a = $('nsections'), b = $('loadSections');
  if (!a || !b) return;
  a.addEventListener('change', () => { b.value = a.value; });
  b.addEventListener('change', () => { a.value = b.value; });
})();

function onAudioLoadDrop(e) {
  e.preventDefault();
  $('audioLoad').classList.remove('drag-over');
  const file = e.dataTransfer.files[0];
  if (file) loadNarrationAudio(file);
}

function _audioUploadForm(file, mode) {
  const fd = new FormData();
  fd.append('file', file);
  fd.append('mode', mode);
  fd.append('script', $('script').value.trim());
  fd.append('voice', $('voice').value);
  fd.append('title', $('vidTitle').value.trim());
  fd.append('sections', ($('loadSections') || $('nsections')).value);
  if (mode === 'replace' && _jobId) fd.append('job_id', _jobId);
  return fd;
}

function _beginAudioProgress() {
  $('genBtn').disabled = true;
  $('progressWrap').classList.add('show');
  $('errMsg').classList.remove('show');
  $('progressBar').classList.add('indet');
  buildGenSteps();
  markStep('audio');
}

async function loadNarrationAudio(file) {
  if (!file) return;
  // Always split into the selected section count — script is optional.
  clearRememberedJob();
  _jobId = null;
  _sections = [];
  _audioDur = 0;
  _hashtags = [];
  $('audioArea').classList.remove('has-audio');
  const box = $('sectionsArea');
  box.innerHTML = '';
  const ph = document.createElement('div');
  ph.className = 'placeholder';
  ph.id = 'sec-placeholder';
  ph.style.display = 'block';
  ph.innerHTML = 'Building sections…';
  box.appendChild(ph);

  _beginAudioProgress();
  try {
    const r = await fetch('/load-audio', {method:'POST', body: _audioUploadForm(file, 'split')});
    const d = await r.json();
    if (d.error) { showErr(d.error); return; }

    if (d.mode === 'split' || d.mode === 'replace') {
      markStep('analysing', true);
      markStep('audio', true);
      $('progressBar').classList.remove('indet');
      $('progressBar').querySelector('i').style.width = '100%';
      $('genBtn').disabled = false;
      onAudioReady(d.job_id, d);
      return;
    }

    // bootstrap with script — AI sectioning via SSE
    rememberJob(d.job_id);
    markStep('analysing');
    streamGenProgress(d.job_id);
  } catch(e) {
    showErr('Audio load failed: ' + e.message);
  } finally {
    const inp = $('audioFile');
    if (inp) inp.value = '';
  }
}

async function replaceNarrationAudio(file) {
  if (!file) return;
  if (!_jobId) {
    // No job yet — treat as a fresh load with the selected section count.
    return loadNarrationAudio(file);
  }
  _beginAudioProgress();
  try {
    const r = await fetch('/load-audio', {method:'POST', body: _audioUploadForm(file, 'replace')});
    const d = await r.json();
    if (d.error) { showErr(d.error); return; }
    markStep('analysing', true);
    markStep('audio', true);
    $('progressBar').classList.remove('indet');
    $('progressBar').querySelector('i').style.width = '100%';
    $('genBtn').disabled = false;
    onAudioReady(d.job_id, d);
  } catch(e) {
    showErr('Audio replace failed: ' + e.message);
  } finally {
    const inp = $('replaceAudioFile');
    if (inp) inp.value = '';
  }
}

// ── live-fill of auto-extracted section clips ─────────────────────────
let _prefillTimer = null;

function pollClipPrefill(jobId) {
  if (_prefillTimer) clearInterval(_prefillTimer);

  const tick = async () => {
    let d;
    try {
      const r = await fetch('/job-state/'+jobId);
      d = await r.json();
    } catch(e) { return; } // transient network error — try again next tick

    (d.sections || []).forEach(s => {
      const local = _sections[s.idx];
      // media_ready means this card already shows a preview (either an earlier
      // sync or the producer's own upload — which always wins, never replaced).
      // dismissed means they clicked "Change media" on the auto clip — don't
      // shove the same clip straight back under their cursor.
      if (!local || local.media_ready || local.dismissed || !s.media) return;
      const ext     = (s.media_ext || '').toLowerCase();
      const isVideo = ['.mp4', '.mov', '.webm'].includes(ext);
      const file    = String(s.media).split(/[\\/]/).pop();
      $('su-'+s.idx).innerHTML = sectionPreviewHtml(`/result/${jobId}/${file}`, isVideo, s.idx, jobId);
      $('sec-card-'+s.idx).style.borderColor = '#059669';
      local.media_ready = true;
      local.ext = ext || null;
    });

    const pf = d.clip_prefill;
    const note = $('prefillNote');
    if (pf && pf.status === 'running') {
      note.style.display = '';
      note.textContent = `✂ Auto-filling clips from the source video… ${pf.done}/${pf.total}`;
    } else {
      note.style.display = 'none';
      clearInterval(_prefillTimer);
      _prefillTimer = null;
    }
  };

  _prefillTimer = setInterval(tick, 3000);
  tick();
}

// ── timeline ─────────────────────────────────────────────────────────
function buildTimeline(sections, totalDur) {
  const tl = $('timeline');
  tl.innerHTML = '';
  if (!sections.length || !totalDur) return;
  sections.forEach(s => {
    const dur = s.end - s.start;
    const pct = (dur / totalDur * 100).toFixed(2);
    const seg = document.createElement('div');
    seg.className = 'tl-seg';
    seg.style.cssText = `width:${pct}%;background:${SEC_COLS[s.idx % SEC_COLS.length]}`;
    seg.textContent = `§${s.idx+1}`;
    seg.title = `Section ${s.idx+1}: ${fmtTime(s.start)} → ${fmtTime(s.end)}`;
    seg.addEventListener('click', () => {
      const card = $('sec-card-'+s.idx);
      if (card) card.scrollIntoView({behavior:'smooth', block:'nearest'});
    });
    tl.appendChild(seg);
  });
}

function refreshTimeline() {
  buildTimeline(_sections, _audioDur);
}

// ── section media preview (shared by an initial pre-filled render and a
// fresh manual upload, so both end up with identical markup) ──────────────
function sectionPreviewHtml(previewUrl, isVideo, idx, jobId) {
  const media = isVideo
    ? `<video class="sec-preview" src="${previewUrl}" muted playsinline
    onmouseover="this.play()" onmouseleave="this.pause();this.currentTime=0"></video>
  <span class="sec-preview-badge">VIDEO</span>`
    : `<img class="sec-preview" src="${previewUrl}" alt="Section ${idx+1}">
  <span class="sec-preview-badge">IMAGE</span>`;
  return `
<div class="sec-preview-wrap">
  ${media}
</div>
<span class="sec-preview-change" onclick="resetSection(${idx},'${jobId}')">&#8635; Change media</span>`;
}

// ── section cards ─────────────────────────────────────────────────────
function buildSectionCard(jobId, s) {
  const color = SEC_COLS[s.idx % SEC_COLS.length];
  const card  = document.createElement('div');
  card.className = 'sec-card';
  card.id = 'sec-card-'+s.idx;
  const isVideo    = ['.mp4', '.mov', '.webm'].includes(s.ext);
  // Server-side media is an absolute path; anything in the job dir is
  // servable via /result/<job>/<basename>. Derived from the real filename
  // rather than assuming section_XXX naming, since auto-extracted clips
  // (producer_scout) use a distinct _auto suffix to dodge upload collisions.
  const mediaFile  = s.media ? String(s.media).split(/[\\/]/).pop() : '';
  const previewUrl = `/result/${jobId}/${mediaFile}`;
  const uploadBlock = s.media_ready
    ? sectionPreviewHtml(previewUrl, isVideo, s.idx, jobId)
    : `<label class="drop-zone" id="dz-${s.idx}" for="fi-${s.idx}"
       ondragover="dzDragOver(event,${s.idx})"
       ondragleave="dzDragLeave(${s.idx})"
       ondrop="dzDrop(event,${s.idx},'${jobId}')">
    <input type="file" id="fi-${s.idx}" accept="image/*,video/mp4,video/mov,video/webm"
           onchange="uploadSection(${s.idx},'${jobId}',this.files[0])">
    &#128247; Drop image or video here<br>
    <span style="color:var(--faint);font-size:10px">JPG · PNG · MP4 · MOV · WebM</span>
  </label>
  <div class="upload-err" id="ue-${s.idx}"></div>`;
  card.innerHTML = `
<div class="sec-head">
  <span class="sec-num" style="color:${color}">§${s.idx+1}</span>
  <div class="sec-times">
    <input class="time-inp" id="ts-${s.idx}" value="${fmtTime(s.start)}"
      title="Start time" onchange="onTimeChange(${s.idx},'start',this.value)">
    <span class="sec-arrow">→</span>
    <input class="time-inp" id="te-${s.idx}" value="${fmtTime(s.end)}"
      title="End time" onchange="onTimeChange(${s.idx},'end',this.value)">
  </div>
</div>
<div class="sec-text" title="${escHtml(s.text)}">${escHtml(s.text)}</div>
<div class="sec-upload" id="su-${s.idx}">${uploadBlock}</div>`;
  if (s.media_ready) card.style.borderColor = '#059669';
  $('sectionsArea').appendChild(card);
}

function escHtml(str) {
  return String(str)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── time editing ──────────────────────────────────────────────────────
function onTimeChange(idx, field, val) {
  const sec = Math.max(0, Math.min(parseTime(val), _audioDur));
  _sections[idx][field] = sec;
  // clamp
  if (field === 'start' && sec >= _sections[idx].end) {
    _sections[idx].end = Math.min(sec + 1, _audioDur);
    $('te-'+idx).value = fmtTime(_sections[idx].end);
  }
  if (field === 'end' && sec <= _sections[idx].start) {
    _sections[idx].start = Math.max(sec - 1, 0);
    $('ts-'+idx).value = fmtTime(_sections[idx].start);
  }
  // propagate to adjacent sections
  if (field === 'end' && idx+1 < _sections.length) {
    _sections[idx+1].start = _sections[idx].end;
    $('ts-'+(idx+1)).value = fmtTime(_sections[idx+1].start);
  }
  if (field === 'start' && idx > 0) {
    _sections[idx-1].end = _sections[idx].start;
    $('te-'+(idx-1)).value = fmtTime(_sections[idx-1].end);
  }
  refreshTimeline();
}

// ── drag-and-drop ─────────────────────────────────────────────────────
function dzDragOver(e, idx) {
  e.preventDefault();
  $('dz-'+idx).classList.add('drag-over');
}
function dzDragLeave(idx) {
  $('dz-'+idx).classList.remove('drag-over');
}
function dzDrop(e, idx, jobId) {
  e.preventDefault();
  $('dz-'+idx).classList.remove('drag-over');
  const file = e.dataTransfer.files[0];
  if (file) uploadSection(idx, jobId, file);
}

// ── upload section media ──────────────────────────────────────────────
async function uploadSection(idx, jobId, file) {
  if (!file) return;
  const dz  = $('dz-'+idx);
  const err = $('ue-'+idx);
  if (dz) dz.classList.add('drag-over');
  if (err) { err.textContent = 'Uploading…'; err.classList.add('show'); }

  try {
    const fd = new FormData();
    fd.append('file', file);
    const r   = await fetch(`/upload-section/${jobId}/${idx}`, {method:'POST', body:fd});
    let res;
    try { res = await r.json(); }
    catch (_) { throw new Error(r.status === 404
      ? 'Job not found — reload the page, then try again.'
      : `Upload failed (HTTP ${r.status})`); }
    if (!r.ok || res.error) throw new Error(res.error || `Upload failed (HTTP ${r.status})`);

    // show preview
    const su = $('su-'+idx);
    const previewUrl = res.is_video ? res.preview : `${res.preview}?t=${Date.now()}`;
    su.innerHTML = sectionPreviewHtml(previewUrl, res.is_video, idx, jobId);

    $('sec-card-'+idx).style.borderColor = '#059669';
    _sections[idx].media_ready = true;
    _sections[idx].dismissed  = false;
    const previewExt = (res.preview || '').match(/\.[^.?]+(?:\?|$)/);
    if (previewExt) _sections[idx].ext = previewExt[0].replace('?','');
    if (err) err.classList.remove('show');
  } catch(e) {
    resetSection(idx, jobId);
    const err2 = $('ue-'+idx);
    if (err2) {
      err2.textContent = 'Upload failed: '+e.message;
      err2.classList.add('show');
    }
  }
}

function resetSection(idx, jobId) {
  _sections[idx].media_ready = false;
  _sections[idx].dismissed   = true; // stops pollClipPrefill re-filling this card
  const su = $('su-'+idx);
  su.innerHTML = `
<label class="drop-zone" id="dz-${idx}" for="fi-${idx}"
     ondragover="dzDragOver(event,${idx})"
     ondragleave="dzDragLeave(${idx})"
     ondrop="dzDrop(event,${idx},'${jobId}')">
  <input type="file" id="fi-${idx}" accept="image/*,video/mp4,video/mov,video/webm"
         onchange="uploadSection(${idx},'${jobId}',this.files[0])">
  &#128247; Drop image or video here<br>
  <span style="color:var(--faint);font-size:10px">JPG · PNG · MP4 · MOV · WebM</span>
</label>
<div class="upload-err" id="ue-${idx}"></div>`;
  $('sec-card-'+idx).style.borderColor = '';
}

// ── assemble ──────────────────────────────────────────────────────────
// ── music ────────────────────────────────────────────────────────────
let _musicReady = false;

function onMusicDrop(e) {
  e.preventDefault();
  $('musicDrop').classList.remove('drag-over');
  const file = e.dataTransfer.files[0];
  if (file) uploadMusic(file);
}

async function uploadMusic(file) {
  if (!file || !_jobId) { alert('Generate audio first before uploading music.'); return; }
  const fd = new FormData();
  fd.append('file', file);
  $('musicLabel').textContent = 'Uploading…';
  try {
    const r = await fetch(`/upload-music/${_jobId}`, {method:'POST', body:fd});
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    _musicReady = true;
    $('musicLabel').textContent = '♪ ' + file.name;
    $('musicDrop').classList.add('ready');
    $('musicControls').classList.add('show');
  } catch(e) {
    $('musicLabel').textContent = 'Ἳ5 Background music (optional) — drop MP3/WAV';
    alert('Music upload failed: ' + e.message);
  }
}

function clearMusic() {
  _musicReady = false;
  $('musicLabel').textContent = '\u{1F3B5} Background music (optional) — drop MP3/WAV';
  $('musicDrop').classList.remove('ready');
  $('musicControls').classList.remove('show');
}

// ── thumbnail builder ─────────────────────────────────────────────────
let _thumbSelIdx = -1;

function buildThumbPicker() {
  const picker = $('thumbPicker');
  picker.innerHTML = '';
  _thumbSelIdx = -1;
  _sections.forEach(s => {
    if (!s.media_ready) return;
    const img = document.createElement('img');
    img.className = 'tpick';
    const ext = s.ext || '.jpg';
    img.src = `/result/${_jobId}/section_${String(s.idx).padStart(3,'0')}${ext}?t=${Date.now()}`;
    img.title = `Section ${s.idx+1}`;
    img.onerror = () => img.remove();
    img.addEventListener('click', () => {
      document.querySelectorAll('.tpick').forEach(el => el.classList.remove('sel'));
      img.classList.add('sel');
      _thumbSelIdx = s.idx;
    });
    picker.appendChild(img);
  });
  $('thumbTitle').value = JOBS_title || '';
}

$('thumbBtn').addEventListener('click', async () => {
  if (_thumbSelIdx < 0) { alert('Click a section image above to select it first.'); return; }
  $('thumbBtn').disabled = true;
  $('thumbBtn').textContent = 'Building…';
  try {
    const r = await fetch(`/thumbnail/${_jobId}`, {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({section_idx: _thumbSelIdx, title: $('thumbTitle').value.trim()}),
    });
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    $('thumbImg').src = d.preview;
    $('thumbDl').href = `/download/${_jobId}/thumbnail.jpg`;
    $('thumbPreviewWrap').style.display = 'block';
  } catch(e) { alert('Thumbnail failed: ' + e.message); }
  finally {
    $('thumbBtn').disabled = false;
    $('thumbBtn').textContent = '\u{1F4F7} Build Thumbnail';
  }
});

let JOBS_title = '';

// ── audio visualizer ─────────────────────────────────────────────────
var _vizStyle = 'none';  // var: restoreFormFromJob may run before this line
function setVizStyle(style) {
  _vizStyle = style;
  document.querySelectorAll('#vizStyles .viz-chip').forEach(b =>
    b.classList.toggle('on', b.dataset.style === style));
  const on = style !== 'none';
  $('vizOpts').classList.toggle('show', on);
  $('vizNote').classList.toggle('show', on);
}
document.querySelectorAll('#vizStyles .viz-chip').forEach(b =>
  b.addEventListener('click', () => setVizStyle(b.dataset.style)));

// ── voice preview ────────────────────────────────────────────────────
let _previewAudio = null;
$('voicePlay').addEventListener('click', async () => {
  const btn = $('voicePlay');
  if (btn.classList.contains('busy')) return;
  if (_previewAudio) { _previewAudio.pause(); _previewAudio = null; }
  btn.classList.add('busy');
  try {
    const r = await fetch('/voice-preview', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        voice: $('voice').value,
        rate:  $('voiceRate').value,
        pitch: $('voicePitch').value,
      }),
    });
    if (!r.ok) {
      let msg = 'Preview failed';
      try { msg = (await r.json()).error || msg; } catch (_) {}
      throw new Error(msg);
    }
    const url = URL.createObjectURL(await r.blob());
    _previewAudio = new Audio(url);
    _previewAudio.onended = () => URL.revokeObjectURL(url);
    await _previewAudio.play();
  } catch (e) {
    alert(e.message);
  } finally {
    btn.classList.remove('busy');
  }
});

$('assembleBtn').addEventListener('click', async () => {
  if (!_jobId) return;
  $('assembleBtn').disabled = true;
  $('assembleProgWrap').classList.add('show');
  $('assembleBar').classList.add('indet');
  $('assembleMsg').textContent = 'Starting assembly…';
  $('previewPlaceholder').style.display = 'block';
  $('videoSection').classList.add('section-hidden');
  $('chaptersSection').classList.add('section-hidden');
  $('thumbnailSection').classList.add('section-hidden');

  const payload = {
    sections:  _sections.map(s => ({
      idx: s.idx,
      start: s.start,
      end: s.end,
      text: s.text || '',
    })),
    xfade:     $('xfadeToggle').checked,
    music_vol: parseFloat($('musicVol').value) / 100,
    viz_style:     _vizStyle,
    viz_placement: $('vizPlacement').value,
    viz_opacity:   parseFloat($('vizOpacity').value) / 100,
  };

  try {
    // Soft ping so this worker hydrates the job before assemble (covers the
    // case where audio played from disk but JOBS was empty after recycle).
    try { await fetch('/job-state/'+_jobId); } catch (_) {}

    const r = await fetch(`/assemble/${_jobId}`, {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(payload),
    });
    let d;
    try { d = await r.json(); }
    catch (_) { assembleErr(r.status === 404 ? 'Job not found' : 'Server error (HTTP '+r.status+')'); return; }
    if (d.error) { assembleErr(d.error); return; }
    streamAssemble(_jobId);
  } catch(e) { assembleErr('Server error: '+e.message); }
});

function assembleErr(msg) {
  $('assembleMsg').textContent = 'Error: '+msg;
  $('assembleMsg').style.color = 'var(--red)';
  $('assembleBar').classList.remove('indet');
  $('assembleBtn').disabled = false;
}

function streamAssemble(jobId) {
  let settled = false;
  const es = new EventSource('/assemble-progress/'+jobId);
  const finishOk = (chapters) => {
    if (settled) return;
    settled = true;
    try { es.close(); } catch (e) {}
    $('assembleBar').classList.remove('indet');
    $('assembleBar').querySelector('i').style.width = '100%';
    $('assembleMsg').style.color = '';
    $('assembleMsg').textContent = 'Video ready!';
    $('assembleBtn').disabled = false;
    onVideoReady(jobId, chapters || '');
  };
  const finishErr = (msg) => {
    if (settled) return;
    settled = true;
    try { es.close(); } catch (e) {}
    assembleErr(msg);
  };
  es.onmessage = ev => {
    let d;
    try { d = JSON.parse(ev.data); } catch (e) { return; }
    $('assembleMsg').style.color = '';
    if (d.phase === 'assembling') {
      $('assembleMsg').textContent = d.msg;
    } else if (d.phase === 'seg_progress') {
      const pct = Math.round(d.current/d.total*70);
      $('assembleBar').classList.remove('indet');
      $('assembleBar').querySelector('i').style.width = pct+'%';
      $('assembleMsg').textContent = d.msg;
    } else if (d.phase === 'concat') {
      $('assembleBar').querySelector('i').style.width = '80%';
      $('assembleMsg').textContent = d.msg;
    } else if (d.phase === 'audio_merge') {
      $('assembleBar').querySelector('i').style.width = '92%';
      $('assembleMsg').textContent = d.msg;
    } else if (d.phase === 'visualizer') {
      // Visualizer render is the long tail — map its 0–100% onto 92–99%.
      const pct = 92 + Math.round((d.pct || 0) * 0.07);
      $('assembleBar').classList.remove('indet');
      $('assembleBar').querySelector('i').style.width = pct+'%';
      $('assembleMsg').textContent = d.msg;
    } else if (d.phase === 'done') {
      finishOk(d.chapters || '');
    } else if (d.phase === 'error') {
      finishErr(d.msg);
    }
  };
  es.onerror = () => {
    if (settled || es._polling) return;
    es._polling = true;
    try { es.close(); } catch (e) {}
    $('assembleMsg').style.color = '';
    $('assembleMsg').textContent = 'Connection dropped — assembly may still be running…';
    // status === 'done' only. has_video stays true during a re-assemble
    // because the previous video.mp4 is still on disk.
    pollJob(jobId, {
      maxTries: 240,
      ready: (d) => d.status === 'done',
      failed: (d) => d.status === 'error_assembly' || d.status === 'error' || d.status === 'audio_ready',
      failMessage: (d) => {
        if (d.status === 'audio_ready')
          return 'Assembly stopped before the video was ready. Click Assemble again.';
        return d.error_message || 'This job failed.';
      },
      onReady: (d) => finishOk(d.chapters || ''),
      onFail: finishErr,
    });
  };
}

function onVideoReady(jobId, chapters) {
  $('previewPlaceholder').style.display = 'none';
  $('videoSection').classList.remove('section-hidden');
  $('videoPlayer').src = `/result/${jobId}/video.mp4?t=${Date.now()}`;
  $('videoDl').href    = `/download/${jobId}/video.mp4`;
  // Chapters
  if (chapters) {
    $('chaptersOut').textContent = chapters;
    $('chaptersSection').classList.remove('section-hidden');
  }
  // Thumbnail builder
  buildThumbPicker();
  $('thumbnailSection').classList.remove('section-hidden');
  // Publish panel
  $('publishSection').classList.remove('section-hidden');
}

// ── hashtags ──────────────────────────────────────────────────────────
function buildHashtags(tags) {
  _hashtags = tags;
  const row = $('hashRow');
  row.innerHTML = '';
  tags.forEach(tag => {
    const sp = document.createElement('span');
    sp.className = 'htag';
    sp.textContent = '#'+tag;
    sp.title = 'Click to copy';
    sp.addEventListener('click', () => {
      navigator.clipboard.writeText('#'+tag);
      sp.style.background = 'rgba(86,211,100,.2)';
      setTimeout(() => sp.style.background = '', 800);
    });
    row.appendChild(sp);
  });
  $('copyHashBtn').onclick = () => {
    const all = _hashtags.map(t => '#'+t).join(' ');
    navigator.clipboard.writeText(all);
    $('copyHashBtn').textContent = 'Copied ✓';
    setTimeout(() => $('copyHashBtn').textContent = 'Copy all hashtags', 1500);
  };
}

// ── job resume (?job=<id> deep link + localStorage) ────────────────
// Used by the platform's producer_scout review flow: approving a proposal
// creates the job server-side and redirects here with ?job=<id> instead of
// this page creating the job itself via the Generate button.
// Also restores the last in-progress job after a plain browser refresh.
(async function resumeJobFromUrl(){
  let jobId = new URLSearchParams(window.location.search).get('job');
  if (!jobId) {
    try { jobId = localStorage.getItem(YTPROD_JOB_KEY); } catch (e) {}
  }
  if (!jobId) return;

  rememberJob(jobId);
  $('genBtn').disabled = true;
  $('progressWrap').classList.add('show');
  buildGenSteps();

  try {
    const r = await fetch('/job-state/'+jobId);
    const d = await r.json();
    if (d.error) {
      // Stale bookmark / wiped output dir — drop the remembered id quietly.
      clearRememberedJob();
      $('genBtn').disabled = false;
      $('progressWrap').classList.remove('show');
      return;
    }

    restoreFormFromJob(d);

    if (d.status === 'running') {
      // Still generating — reconnect to the live SSE stream, same as a
      // normal in-page generation, just skipping the /generate POST.
      markStep('analysing');
      streamGenProgress(jobId);
      return;
    }

    // SSE queue for this job has already fully drained (sentinel consumed) —
    // restore state directly from the snapshot instead, since reconnecting
    // to /progress at this point would hang forever with nothing left to send.
    markStep('analysing', true);
    markStep('audio', true);
    $('progressBar').classList.remove('indet');
    $('progressBar').querySelector('i').style.width = '100%';
    $('genBtn').disabled = false;

    if (d.status === 'error' || d.status === 'error_assembly') {
      // Still restore audio/sections if narration exists so refresh isn't a dead end.
      if (d.sections && d.sections.length && d.audio_dur) {
        onAudioReady(jobId, d);
      }
      showErr(d.error_message || 'This job failed — check the server log.');
      return;
    }

    onAudioReady(jobId, d);

    if (d.has_video) {
      // Assembly already finished in a previous visit — restore that view too.
      onVideoReady(jobId, d.chapters || '');
    }
  } catch(e) {
    showErr('Could not resume job: '+e.message);
  }
})();
</script>
</body>
</html>"""


if __name__ == "__main__":
    if not shutil.which("ffmpeg"):
        print("  WARNING: ffmpeg not found", flush=True)
    if not shutil.which("ffprobe"):
        print("  WARNING: ffprobe not found", flush=True)
    if not _EDGE_TTS:
        print("  WARNING: edge-tts not installed — pip install edge-tts", flush=True)
    if not os.environ.get("AI_KEY"):
        print("  WARNING: AI_KEY not set in .env", flush=True)

    import socket
    try:
        local_ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        local_ip = "127.0.0.1"
    print(f"\n  YT Production")
    print(f"  Local  : http://127.0.0.1:5001")
    print(f"  Network: http://{local_ip}:5001\n")
    app.run(host="0.0.0.0", port=5001, threaded=True, debug=False)
