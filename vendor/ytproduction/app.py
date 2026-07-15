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

_AI_ENDPOINT = os.environ.get("AI_ENDPOINT", "https://api.groq.com/openai/v1/chat/completions")
_AI_MODEL    = os.environ.get("AI_MODEL",    "llama-3.3-70b-versatile")

JOBS: dict = {}
app = Flask(__name__)

# ─── helpers ─────────────────────────────────────────────────────────

def _push(q, data: dict):
    q.put(json.dumps(data))

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
    payload = {"model": _AI_MODEL, "messages": msgs,
               "max_tokens": max_tokens, "temperature": 0.4}
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
            if r.status_code >= 400:
                raise RuntimeError(f"Groq API {r.status_code}: {r.text[:300]}")
            return r.json()["choices"][0]["message"]["content"].strip()
        except RuntimeError:
            raise
        except Exception as e:
            last_err = e
            print(f"  [ytprod] Groq error (attempt {attempt+1}): {e}", flush=True)
    raise RuntimeError(f"Groq API unreachable after 4 attempts: {last_err}")

def _ai_generate_metadata(script: str, title: str):
    """Generate title/description/hashtags only — no sections. Used by the
    video-to-sections flow, where sections already come from Clipper's chapter
    proposal rather than this app's own script-splitting step (run_generation's
    analysis_prompt). A trimmed version of that same prompt, minus the
    "sections" key.
    """
    prompt = f"""Based on this video script, generate YouTube metadata.

Return ONLY a JSON object:
{{
  "title": "<compelling video title based on: '{title or 'auto-generate'}'>",
  "description": "<YouTube description, 150-250 words, engaging>",
  "hashtags": ["tag1", "tag2", ... up to 20 relevant tags without #]
}}

SCRIPT:
{script}

Return ONLY the JSON, no explanation."""

    raw = _groq(prompt, system="You are a video production AI. Return only valid JSON.",
                max_tokens=1000)
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        raise ValueError("Could not parse AI response")
    analysis = json.loads(m.group())
    vid_title = analysis.get("title", title or "My Video")
    description = analysis.get("description", "")
    hashtags = analysis.get("hashtags", [])
    return vid_title, description, hashtags


def _audio_duration(path: str) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", path],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return 0.0
    try:
        data = json.loads(result.stdout)
        for stream in data.get("streams", []):
            dur = stream.get("duration")
            if dur:
                return float(dur)
    except Exception:
        pass
    return 0.0

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

def _make_placeholder(duration: float, idx: int, out_path: str):
    colours = ["0x1a1a2e","0x16213e","0x0f3460","0x533483","0x2c2c54","0x1a3a4a","0x2d1b2e","0x1b2d2e"]
    c = colours[idx % len(colours)]
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

def run_generation(job_id: str, script: str, voice: str, title: str, n_sections: int):
    job     = JOBS[job_id]
    q       = job["queue"]
    push    = lambda d: _push(q, d)
    job_dir = os.path.join(OUTPUT_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    try:
        # Step 1: Analyse script with Groq
        push({"phase": "analysing", "msg": "Analysing script…"})
        analysis_prompt = f"""Split this video script into exactly {n_sections} sections for a video.

Return ONLY a JSON object:
{{
  "sections": [
    {{"idx": 0, "text": "<script lines for this section>", "word_count": <integer>}},
    ...exactly {n_sections} items...
  ],
  "title": "<compelling video title based on: '{title or 'auto-generate'}'>",
  "description": "<YouTube description, 150-250 words, engaging>",
  "hashtags": ["tag1", "tag2", ... up to 20 relevant tags without #]
}}

Split at natural paragraph or topic breaks. Cover the entire script.
Estimate word_count for each section by counting the words in that section's text.

SCRIPT:
{script}

Return ONLY the JSON, no explanation."""

        raw = _groq(analysis_prompt,
                    system="You are a video production AI. Return only valid JSON.",
                    max_tokens=3000)
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            raise ValueError("Could not parse AI response")
        analysis = json.loads(m.group())

        sections_raw = analysis.get("sections", [])
        if not sections_raw:
            raise ValueError("No sections returned from AI")

        vid_title   = analysis.get("title", title or "My Video")
        description = analysis.get("description", "")
        hashtags    = analysis.get("hashtags", [])

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

        # Step 2: Generate audio
        push({"phase": "audio", "msg": "Generating audio narration…"})
        if not _EDGE_TTS:
            raise RuntimeError("edge-tts not installed — run: pip install edge-tts")

        audio_path = os.path.join(job_dir, "narration.mp3")

        async def _tts():
            communicate = edge_tts.Communicate(script, voice, receive_timeout=300)
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
        if actual_dur > 0 and sections:
            est_total = sections[-1]["end"]
            ratio = actual_dur / est_total
            for s in sections:
                s["start"] = round(s["start"] * ratio, 2)
                s["end"]   = round(s["end"]   * ratio, 2)
            sections[-1]["end"] = round(actual_dur, 2)

        job["status"] = "audio_ready"
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
            communicate = edge_tts.Communicate(script_for_tts, voice, receive_timeout=300)
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
        if actual_dur > 0 and sections:
            est_total = sections[-1]["end"]
            ratio = actual_dur / est_total
            for s in sections:
                s["start"] = round(s["start"] * ratio, 2)
                s["end"]   = round(s["end"]   * ratio, 2)
            sections[-1]["end"] = round(actual_dur, 2)

        job["status"] = "audio_ready"
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
        push({"phase": "error", "msg": str(e)})
    finally:
        q.put(None)


# ─── assembly pipeline ────────────────────────────────────────────────

def _fmt_ch(sec: float) -> str:
    s = int(sec); m = s // 60; sc = s % 60
    return f"{m}:{sc:02d}"

def run_assembly(job_id: str):
    job       = JOBS[job_id]
    q         = job["assemble_queue"]
    push      = lambda d: _push(q, d)
    job_dir   = os.path.join(OUTPUT_DIR, job_id)
    sections  = job["sections"]
    total     = len(sections)
    use_xfade = job.get("xfade", True)
    xfade_dur = 0.5

    try:
        push({"phase": "assembling", "msg": f"Building {total} video segments…"})
        seg_paths = []
        seg_durations = []

        for s in sections:
            idx     = s["idx"]
            dur     = max(s["end"] - s["start"], 0.5)
            media   = s.get("media")
            ext     = (s.get("media_ext") or "").lower()
            seg_out = os.path.join(job_dir, f"seg_{idx:03d}.mp4")

            push({"phase": "seg_progress", "msg": f"Segment {idx+1}/{total}…",
                  "current": idx+1, "total": total})

            if media and os.path.exists(media):
                is_image = ext in (".jpg", ".jpeg", ".png", ".gif", ".webp")
                _make_segment(media, dur, seg_out, is_image)
            else:
                _make_placeholder(dur, idx, seg_out)

            actual_dur = _video_duration(seg_out) or dur
            seg_paths.append(seg_out)
            seg_durations.append(actual_dur)

        push({"phase": "concat", "msg": "Joining segments…"})
        merged = os.path.join(job_dir, "merged.mp4")

        if use_xfade and len(seg_paths) > 1:
            _ffmpeg_xfade_concat(seg_paths, seg_durations, merged, xfade_dur)
        else:
            concat_txt = os.path.join(job_dir, "concat.txt")
            with open(concat_txt, "w") as f:
                for p in seg_paths:
                    f.write(f"file '{p}'\n")
            r = subprocess.run(
                ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_txt, "-c", "copy", merged],
                capture_output=True, text=True
            )
            if r.returncode != 0:
                raise RuntimeError(f"ffmpeg concat: {r.stderr[-400:]}")

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
        push({"phase": "done", "msg": "Video ready!", "chapters": chapters})

    except Exception as e:
        traceback.print_exc()
        job["status"] = "error_assembly"
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
    n_sections = max(2, min(10, int(data.get("sections", 6))))

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
        "sections": [],
        "audio_path": None,
        "video_path": None,
    }
    threading.Thread(target=run_generation,
                     args=(job_id, script, voice, title, n_sections), daemon=True).start()
    return jsonify({"job_id": job_id})


def _start_generation_from_sections(sections_text, voice, title, description, hashtags):
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
        "sections": [],
        "audio_path": None,
        "video_path": None,
    }
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
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"job_id": job_id})


@app.route("/job-state/<job_id>")
def job_state(job_id):
    """Lightweight snapshot of a job's current state (no queue) — lets the
    frontend resume a job whose SSE stream has already been fully drained
    (the sentinel already consumed means reconnecting to /progress hangs
    forever with no way to ever see that job's state again). Used by the
    ?job= deep-link resume path.
    """
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404
    audio_dur = None
    if job.get("audio_path") and os.path.exists(job["audio_path"]):
        audio_dur = round(_audio_duration(job["audio_path"]), 2)
    return jsonify({
        "status": job.get("status"),
        "title": job.get("title"),
        "description": job.get("description"),
        "hashtags": job.get("hashtags"),
        "sections": job.get("sections", []),
        "audio_dur": audio_dur,
        "has_video": bool(job.get("video_path")),
        "chapters": job.get("chapters"),
        "error_message": job.get("error_message"),
    })


@app.route("/progress/<job_id>")
def progress(job_id):
    job = JOBS.get(job_id)
    if not job:
        return "Not found", 404

    def stream():
        q = job["queue"]
        while True:
            item = q.get()
            if item is None:
                break
            yield f"data: {item}\n\n"

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/upload-section/<job_id>/<int:idx>", methods=["POST"])
def upload_section(job_id, idx):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400
    f   = request.files["file"]
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".webm"):
        return jsonify({"error": "Unsupported file type"}), 400
    job_dir    = os.path.join(OUTPUT_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    media_path = os.path.join(job_dir, f"section_{idx:03d}{ext}")
    f.save(media_path)
    if 0 <= idx < len(job["sections"]):
        job["sections"][idx]["media"]     = media_path
        job["sections"][idx]["media_ext"] = ext
    is_video = ext in (".mp4", ".mov", ".webm")
    return jsonify({
        "ok": True,
        "preview": f"/result/{job_id}/section_{idx:03d}{ext}",
        "is_video": is_video,
    })


@app.route("/assemble/<job_id>", methods=["POST"])
def assemble(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job["status"] not in ("audio_ready", "done", "error_assembly"):
        return jsonify({"error": "Audio not ready yet"}), 400

    data = request.get_json(silent=True) or {}
    for cs in data.get("sections", []):
        idx = cs.get("idx", -1)
        if 0 <= idx < len(job["sections"]):
            job["sections"][idx]["start"] = float(cs.get("start", job["sections"][idx]["start"]))
            job["sections"][idx]["end"]   = float(cs.get("end",   job["sections"][idx]["end"]))

    job["xfade"]          = bool(data.get("xfade", True))
    job["music_vol"]      = float(data.get("music_vol", 0.15))
    job["status"]         = "assembling"
    job["assemble_queue"] = queue.Queue()
    threading.Thread(target=run_assembly, args=(job_id,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/upload-music/<job_id>", methods=["POST"])
def upload_music(job_id):
    job = JOBS.get(job_id)
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
    return jsonify({"ok": True, "filename": f.filename})


@app.route("/thumbnail/<job_id>", methods=["POST"])
def make_thumbnail(job_id):
    job = JOBS.get(job_id)
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
    job = JOBS.get(job_id)
    if not job:
        return "Not found", 404

    def stream():
        q = job.get("assemble_queue")
        if not q:
            return
        while True:
            item = q.get()
            if item is None:
                break
            yield f"data: {item}\n\n"

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/result/<job_id>/<file>")
def result_file(job_id, file):
    job = JOBS.get(job_id)
    if not job:
        return "Not found", 404
    safe = re.sub(r"[^a-zA-Z0-9_.\-]", "", file)
    path = os.path.join(OUTPUT_DIR, job_id, safe)
    if not os.path.exists(path):
        return "File not found", 404
    return send_file(path)


@app.route("/download/<job_id>/<file>")
def download_file(job_id, file):
    job = JOBS.get(job_id)
    if not job:
        return "Not found", 404
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
.col2-audio{flex:none;padding:12px;border-bottom:1px solid var(--line);display:none}
.col2-audio.show{display:block}
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
  cursor:pointer;transition:border-color .15s,background .15s;line-height:1.6}
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
<div class="wrap">
<header>
  <div class="logo-mark">&#9654;</div>
  <h1>Team MoneyTuber &mdash; YT Production</h1>
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
        <select id="voice">
          <optgroup label="English (US)">
            <option value="en-US-GuyNeural">Guy · Male, News</option>
            <option value="en-US-JennyNeural">Jenny · Female</option>
            <option value="en-US-AriaNeural">Aria · Female, Natural</option>
            <option value="en-US-DavisNeural">Davis · Male, Casual</option>
            <option value="en-US-TonyNeural">Tony · Male, Bold</option>
          </optgroup>
          <optgroup label="English (UK)">
            <option value="en-GB-RyanNeural">Ryan · Male, British</option>
            <option value="en-GB-SoniaNeural">Sonia · Female, British</option>
          </optgroup>
          <optgroup label="English (Nigeria)">
            <option value="en-NG-AbeoNeural">Abeo · Male, Nigerian</option>
            <option value="en-NG-EzinneNeural">Ezinne · Female, Nigerian</option>
          </optgroup>
        </select>
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

  <!-- fixed audio + timeline zone -->
  <div class="col2-audio" id="audioArea">
    <audio id="audioPlayer" controls></audio>
    <div class="timeline" id="timeline"></div>
    <a class="dl-btn" id="audioDl" href="#" download style="font-size:11px">&#8595; Download MP3</a>
  </div>

  <!-- scrollable section cards -->
  <div class="col2-sections" id="sectionsArea">
    <div class="placeholder" id="sec-placeholder">
      Generate audio first.<br>Your section cards will appear here.
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

// ── generate audio ───────────────────────────────────────────────────
$('genBtn').addEventListener('click', async () => {
  const script = $('script').value.trim();
  if (!script) { alert('Paste your script first.'); return; }

  // reset
  _jobId = null; _sections = []; _audioDur = 0; _hashtags = [];
  $('genBtn').disabled = true;
  $('progressWrap').classList.add('show');
  $('errMsg').classList.remove('show');
  $('progressBar').classList.add('indet');
  $('audioArea').classList.remove('show');
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
        title:    $('vidTitle').value.trim(),
        sections: $('nsections').value,
      }),
    });
    const d = await r.json();
    if (d.error) { showErr(d.error); return; }
    _jobId = d.job_id;
    streamGenProgress(_jobId);
  } catch(e) { showErr('Server error: '+e.message); }
});

function streamGenProgress(jobId) {
  const es = new EventSource('/progress/'+jobId);
  es.onmessage = ev => {
    const d = JSON.parse(ev.data);
    if (d.phase === 'analysing') markStep('analysing');
    else if (d.phase === 'analysed') markStep('analysing', true);
    else if (d.phase === 'audio') markStep('audio');
    else if (d.phase === 'audio_done') {
      es.close();
      markStep('audio', true);
      $('progressBar').classList.remove('indet');
      $('progressBar').querySelector('i').style.width = '100%';
      $('genBtn').disabled = false;
      onAudioReady(jobId, d);
    }
    else if (d.phase === 'error') { es.close(); showErr(d.msg || 'Unknown error'); }
  };
  es.onerror = () => { es.close(); $('genBtn').disabled = false; };
}

// ── after audio ready ────────────────────────────────────────────────
function onAudioReady(jobId, data) {
  _audioDur   = data.audio_dur || 0;
  _sections   = (data.sections || []).map(s => ({...s, media_ready: false, ext: null}));
  _hashtags   = data.hashtags  || [];
  JOBS_title  = data.title || '';

  // audio player
  $('audioPlayer').src = `/result/${jobId}/narration.mp3?t=${Date.now()}`;
  $('audioDl').href    = `/download/${jobId}/narration.mp3`;
  $('audioArea').classList.add('show');

  // timeline
  buildTimeline(_sections, _audioDur);

  // section cards
  const _phEl = $('sec-placeholder');
  if (_phEl) _phEl.style.display = 'none';
  _sections.forEach(s => buildSectionCard(jobId, s));

  // publish panel meta (shown later after assemble)
  $('titleOut').textContent = data.title       || '';
  $('descOut').textContent  = data.description || '';
  buildHashtags(data.hashtags || []);

  $('assembleBtn').disabled = false;
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

// ── section cards ─────────────────────────────────────────────────────
function buildSectionCard(jobId, s) {
  const color = SEC_COLS[s.idx % SEC_COLS.length];
  const card  = document.createElement('div');
  card.className = 'sec-card';
  card.id = 'sec-card-'+s.idx;
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
<div class="sec-upload" id="su-${s.idx}">
  <div class="drop-zone" id="dz-${s.idx}"
       onclick="$('fi-${s.idx}').click()"
       ondragover="dzDragOver(event,${s.idx})"
       ondragleave="dzDragLeave(${s.idx})"
       ondrop="dzDrop(event,${s.idx},'${jobId}')">
    <input type="file" id="fi-${s.idx}" accept="image/*,video/mp4,video/mov,video/webm"
           onchange="uploadSection(${s.idx},'${jobId}',this.files[0])">
    &#128247; Drop image or video here<br>
    <span style="color:var(--faint);font-size:10px">JPG · PNG · MP4 · MOV · WebM</span>
  </div>
  <div class="upload-err" id="ue-${s.idx}"></div>
</div>`;
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
  dz.style.opacity = '.5';
  dz.textContent   = 'Uploading…';
  err.classList.remove('show');

  try {
    const fd = new FormData();
    fd.append('file', file);
    const r   = await fetch(`/upload-section/${jobId}/${idx}`, {method:'POST', body:fd});
    const res = await r.json();
    if (res.error) throw new Error(res.error);

    // show preview
    const su = $('su-'+idx);
    if (res.is_video) {
      su.innerHTML = `
<div class="sec-preview-wrap">
  <video class="sec-preview" src="${res.preview}" muted playsinline
    onmouseover="this.play()" onmouseleave="this.pause();this.currentTime=0"></video>
  <span class="sec-preview-badge">VIDEO</span>
</div>
<span class="sec-preview-change" onclick="resetSection(${idx},'${jobId}')">&#8635; Change media</span>`;
    } else {
      su.innerHTML = `
<div class="sec-preview-wrap">
  <img class="sec-preview" src="${res.preview}?t=${Date.now()}" alt="Section ${idx+1}">
  <span class="sec-preview-badge">IMAGE</span>
</div>
<span class="sec-preview-change" onclick="resetSection(${idx},'${jobId}')">&#8635; Change media</span>`;
    }

    $('sec-card-'+idx).style.borderColor = '#059669';
    _sections[idx].media_ready = true;
    // Store ext from the preview URL for thumb picker
    const previewExt = res.preview.match(/\.[^.?]+(?:\?|$)/);
    if (previewExt) _sections[idx].ext = previewExt[0].replace('?','');
  } catch(e) {
    dz.style.opacity = '1';
    dz.innerHTML = `&#128247; Drop image or video here<br><span style="color:var(--faint);font-size:10px">JPG · PNG · MP4 · MOV · WebM</span>`;
    err.textContent = 'Upload failed: '+e.message;
    err.classList.add('show');
  }
}

function resetSection(idx, jobId) {
  _sections[idx].media_ready = false;
  const su = $('su-'+idx);
  su.innerHTML = `
<div class="drop-zone" id="dz-${idx}"
     onclick="$('fi-${idx}').click()"
     ondragover="dzDragOver(event,${idx})"
     ondragleave="dzDragLeave(${idx})"
     ondrop="dzDrop(event,${idx},'${jobId}')">
  <input type="file" id="fi-${idx}" accept="image/*,video/mp4,video/mov,video/webm"
         onchange="uploadSection(${idx},'${jobId}',this.files[0])">
  &#128247; Drop image or video here<br>
  <span style="color:var(--faint);font-size:10px">JPG · PNG · MP4 · MOV · WebM</span>
</div>
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
    sections:  _sections.map(s => ({idx: s.idx, start: s.start, end: s.end})),
    xfade:     $('xfadeToggle').checked,
    music_vol: parseFloat($('musicVol').value) / 100,
  };

  try {
    const r = await fetch(`/assemble/${_jobId}`, {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(payload),
    });
    const d = await r.json();
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
  const es = new EventSource('/assemble-progress/'+jobId);
  es.onmessage = ev => {
    const d = JSON.parse(ev.data);
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
    } else if (d.phase === 'done') {
      es.close();
      $('assembleBar').querySelector('i').style.width = '100%';
      $('assembleMsg').textContent = '&#10003; Video ready!';
      $('assembleBtn').disabled = false;
      onVideoReady(jobId, d.chapters || '');
    } else if (d.phase === 'error') {
      es.close();
      assembleErr(d.msg);
    }
  };
  es.onerror = () => { es.close(); $('assembleBtn').disabled = false; };
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
