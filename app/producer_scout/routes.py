import sys

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

    # Metadata is generated from the cleaned chapter text (not the raw
    # transcript) so title/description reflect the same polished script the
    # producer is about to review, not the noisy auto-caption source.
    script_preview = "\n\n".join(c["text"] for c in chapters)
    try:
        vid_title, description, hashtags = ytprod._ai_generate_metadata(script_preview, video_title)
    except Exception as e:
        flash(f"AI metadata generation failed: {e}", "error")
        return redirect(url_for("producer_scout.new"))

    return render_template(
        "producer_scout/review.html",
        video_url=video_url,
        chapters=chapters,
        title=vid_title,
        description=description,
        hashtags=hashtags,
    )


@bp.route("/approve", methods=["POST"])
def approve():
    section_texts = request.form.getlist("section_text")
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

    return redirect(f"/produce/?job={job_id}")
