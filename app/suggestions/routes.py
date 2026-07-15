from datetime import datetime, timezone
from urllib.parse import quote
from collections import OrderedDict

from flask import render_template, redirect, flash
from flask_login import login_required, current_user

from . import bp
from ..extensions import db
from ..auth.decorators import roles_required
from ..models import SuggestedClip, DiscoveredVideo


@bp.before_request
@login_required
@roles_required("clipper")
def _require_clipper():
    pass


@bp.route("/")
def queue():
    pending = (
        SuggestedClip.query.filter_by(status="pending")
        .join(DiscoveredVideo)
        .order_by(DiscoveredVideo.discovered_at.desc(), SuggestedClip.start.asc())
        .all()
    )
    by_video = OrderedDict()
    for clip in pending:
        by_video.setdefault(clip.video, []).append(clip)
    return render_template("suggestions/queue.html", by_video=by_video)


@bp.route("/<int:clip_id>/approve", methods=["POST"])
def approve(clip_id):
    clip = SuggestedClip.query.get_or_404(clip_id)
    if clip.status != "pending":
        flash(
            f"Already handled by {clip.reviewed_by.email if clip.reviewed_by else 'someone else'}.",
            "error",
        )
        return redirect("/suggestions/")

    clip.status = "approved"
    clip.reviewed_by_id = current_user.id
    clip.reviewed_at = datetime.now(timezone.utc)
    db.session.commit()

    # Suggest-only: approving hands off to Clipper's own UI for the human to
    # actually review the timeline and cut it — never triggers a cut here.
    return redirect(f"/clip/?url={quote(clip.video.video_url, safe='')}")


@bp.route("/<int:clip_id>/reject", methods=["POST"])
def reject(clip_id):
    clip = SuggestedClip.query.get_or_404(clip_id)
    if clip.status != "pending":
        flash(
            f"Already handled by {clip.reviewed_by.email if clip.reviewed_by else 'someone else'}.",
            "error",
        )
        return redirect("/suggestions/")

    clip.status = "rejected"
    clip.reviewed_by_id = current_user.id
    clip.reviewed_at = datetime.now(timezone.utc)
    db.session.commit()
    flash("Suggestion rejected.", "success")
    return redirect("/suggestions/")
