import os
import sys
import threading
import time

from flask import render_template, request, redirect, flash, url_for
from flask_login import login_required

from . import bp
from ..auth.decorators import roles_required

MAX_SECTIONS = 13


@bp.before_request
@login_required
@roles_required("producer")
def _require_producer():
    pass


def _clipper():
    # Same lazy-load pattern as the run-suggest-agent CLI (app/__init__.py) —
    # wsgi.py's build_wsgi_app() has very likely already loaded this module by
    # the time any request comes in; only load it ourselves as a fallback.
    mod = sys.modules.get("vendored_clipper")
    if mod is None:
        from ..mounting.dispatcher import load_clipper_app

        load_clipper_app()
        mod = sys.modules["vendored_clipper"]
    return mod


def _ytprod():
    mod = sys.modules.get("vendored_ytproduction")
    if mod is None:
        from ..mounting.dispatcher import load_ytproduction_app

        load_ytproduction_app()
        mod = sys.modules["vendored_ytproduction"]
    return mod


@bp.route("/")
def new():
    return render_template("producer_scout/new.html")


@bp.route("/propose", methods=["POST"])
def propose():
    video_url = (request.form.get("video_url") or "").strip()
    if not video_url:
        flash("Paste a YouTube URL first.", "error")
        return redirect(url_for("producer_scout.new"))

    clipper = _clipper()
    ytprod = _ytprod()

    try:
        info = clipper._ydl_extract(
            video_url, {"quiet": True, "no_warnings": True, "skip_download": True}, download=False
        )
        if info.get("_type") == "playlist" and info.get("entries"):
            info = info["entries"][0]
        video_title = info.get("title", "")
        video_duration = info.get("duration") or 0
    except Exception as e:
        flash(f"Couldn't read that video: {e}", "error")
        return redirect(url_for("producer_scout.new"))

    cues = clipper._fetch_raw_transcript(video_url)
    if not cues:
        flash("Couldn't find English captions on that video — try a different one.", "error")
        return redirect(url_for("producer_scout.new"))

    try:
        chapters = clipper._ai_generate_chapters(cues, title=video_title, max_chapters=MAX_SECTIONS)
    except Exception as e:
        flash(f"AI sectioning failed: {e}", "error")
        return redirect(url_for("producer_scout.new"))

    # Each section's *source* time range — chapter i runs from its own start
    # to the next chapter's start (or the video's end for the last one). Only
    # used to pre-fill each section's media with the matching footage after
    # approval; has nothing to do with the narration timeline, which is a
    # completely different clock (word-count estimated, then rescaled to the
    # synthesized audio's real duration).
    sections = []
    for i, c in enumerate(chapters):
        src_end = chapters[i + 1]["start"] if i + 1 < len(chapters) else video_duration
        sections.append({
            "title": c["title"],
            "text": c["text"],
            "src_start": c["start"],
            "src_end": max(src_end, c["start"] + 1),
        })

    # Metadata is generated from the cleaned chapter text (not the raw
    # transcript) so title/description reflect the same polished script the
    # producer is about to review, not the noisy auto-caption source.
    script_preview = "\n\n".join(s["text"] for s in sections)
    try:
        vid_title, description, hashtags = ytprod._ai_generate_metadata(script_preview, video_title)
    except Exception as e:
        flash(f"AI metadata generation failed: {e}", "error")
        return redirect(url_for("producer_scout.new"))

    return render_template(
        "producer_scout/review.html",
        video_url=video_url,
        sections=sections,
        title=vid_title,
        description=description,
        hashtags=hashtags,
    )


@bp.route("/approve", methods=["POST"])
def approve():
    video_url = (request.form.get("video_url") or "").strip()
    section_texts = request.form.getlist("section_text")
    src_starts = request.form.getlist("section_src_start")
    src_ends = request.form.getlist("section_src_end")
    title = (request.form.get("title") or "").strip()
    description = request.form.get("description") or ""
    hashtags = [h.strip() for h in (request.form.get("hashtags") or "").split(",") if h.strip()]
    voice = request.form.get("voice") or "en-US-GuyNeural"

    # A producer could blank a textarea by mistake before submitting — mirrors
    # /generate's own len(script) < 20 guard in ytproduction.
    if not any(t.strip() for t in section_texts):
        flash("At least one section needs text.", "error")
        return redirect(url_for("producer_scout.new"))

    ytprod = _ytprod()
    try:
        # word_count for each section is recomputed fresh inside
        # run_generation_from_sections from whatever text is passed here —
        # never carried over stale from the propose step, so any edits the
        # producer made in the review form drive the real duration estimate.
        job_id = ytprod._start_generation_from_sections(
            sections_text=section_texts,
            voice=voice,
            title=title,
            description=description,
            hashtags=hashtags,
        )
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("producer_scout.new"))

    if video_url:
        src_ranges = []
        for s, e in zip(src_starts, src_ends):
            try:
                src_ranges.append((float(s), float(e)))
            except (TypeError, ValueError):
                src_ranges.append(None)
        # Marked on the job before the redirect (not inside the thread) so the
        # page never loads mid-race and concludes "no prefill running" —
        # /job-state exposes this and the frontend polls it to live-fill
        # section previews as each clip lands.
        total = sum(1 for r in src_ranges if r and r[1] > r[0])
        ytprod.JOBS[job_id]["clip_prefill"] = {"status": "running", "done": 0, "total": total}
        threading.Thread(
            target=_extract_section_clips,
            args=(job_id, video_url, src_ranges),
            daemon=True,
        ).start()

    return redirect(f"/produce/?job={job_id}")


def _extract_section_clips(job_id, video_url, src_ranges):
    """Pre-fill each section's media with the matching footage cut from the
    source video, so a Producer starting from an existing video doesn't have
    to manually re-source clips for content that's already right there.
    Runs in its own background thread, entirely separate from narration
    generation (run_generation_from_sections) — synchronous extraction of up
    to 13 clips can easily take longer than a request should block for (real
    risk of hitting gunicorn's worker timeout in production), and a failed or
    slow extraction here must never take down narration generation with it.
    A section left unfilled just falls back to the existing manual-upload
    drop zone, same as the typed-script flow always required.
    """
    ytprod = _ytprod()
    job = ytprod.JOBS.get(job_id)
    if job is None:
        return
    prefill = job.get("clip_prefill") or {}

    def _finish(status):
        prefill["status"] = status

    # run_generation_from_sections populates job["sections"] almost
    # immediately (Step 1, before the TTS call that actually takes real
    # wall-clock time) — this just waits out that small startup window rather
    # than assuming it's already there.
    for _ in range(50):
        if job.get("sections"):
            break
        time.sleep(0.1)
    sections = job.get("sections") or []
    if not sections:
        _finish("error")
        return

    job_dir = os.path.join(ytprod.OUTPUT_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    clipper = _clipper()
    try:
        info = clipper._resolve_stream_info(video_url)
    except Exception as e:
        print(f"  [producer_scout] couldn't resolve source video for clip extraction: {e}", flush=True)
        _finish("error")
        return

    for i, rng in enumerate(src_ranges):
        if i >= len(sections) or rng is None:
            continue
        start, end = rng
        if end <= start:
            continue
        # The producer may have manually uploaded media for this section while
        # earlier clips were still cutting — their explicit choice wins, don't
        # overwrite it with the auto-extract.
        if sections[i].get("media"):
            prefill["done"] = prefill.get("done", 0) + 1
            continue
        # "_auto" suffix keeps this path distinct from /upload-section's
        # section_{idx}{ext} naming — a manual upload mid-extraction must
        # never race ffmpeg writing to the same file.
        out_file = os.path.join(job_dir, f"section_{i:03d}_auto.mp4")
        try:
            clipper._extract_clip_from_info(info, start, end, out_file)
        except Exception as e:
            print(f"  [producer_scout] section {i} clip extraction failed: {e}", flush=True)
            continue
        # Same shape /upload-section writes — the assemble step (run_assembly)
        # reads these two keys the same way regardless of how media got here.
        sections[i]["media"] = out_file
        sections[i]["media_ext"] = ".mp4"
        prefill["done"] = prefill.get("done", 0) + 1

    _finish("done")
