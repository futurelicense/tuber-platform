from datetime import datetime, timezone
from urllib.parse import quote
from collections import OrderedDict

from flask import render_template, redirect, flash
from flask_login import login_required, current_user
from sqlalchemy import update

from . import bp
from ..extensions import db
from ..auth.decorators import roles_required
from ..models import SuggestedClip, DiscoveredVideo


def _claim(clip_id, new_status):
    """Atomically move a clip out of 'pending', guarding against two clippers
    approving/rejecting the same suggestion at once. A plain read-then-write
    (SELECT, check .status, then UPDATE) has a TOCTOU race under gunicorn's
    multi-threaded worker (Dockerfile runs --threads 24): both requests can
    read 'pending' before either commits, so both would "win" and the second
    commit would silently overwrite the first reviewer's attribution while
    both users get redirected as if they were the one handling it.

    The UPDATE...WHERE status='pending' only matches for whichever request
    gets there first; rowcount tells the loser it lost.
    """
    result = db.session.execute(
        update(SuggestedClip)
        .where(SuggestedClip.id == clip_id, SuggestedClip.status == "pending")
        .values(
            status=new_status,
            reviewed_by_id=current_user.id,
            reviewed_at=datetime.now(timezone.utc),
        )
    )
    db.session.commit()
    return result.rowcount > 0


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
    won = _claim(clip_id, "approved")
    if not won:
        db.session.refresh(clip)
        flash(
            f"Already handled by {clip.reviewed_by.email if clip.reviewed_by else 'someone else'}.",
            "error",
        )
        return redirect("/suggestions/")

    # Suggest-only: approving hands off to Clipper's own UI for the human to
    # actually review the timeline and cut it — never triggers a cut here.
    return redirect(f"/clip/?url={quote(clip.video.video_url, safe='')}")


@bp.route("/<int:clip_id>/reject", methods=["POST"])
def reject(clip_id):
    clip = SuggestedClip.query.get_or_404(clip_id)
    won = _claim(clip_id, "rejected")
    if not won:
        db.session.refresh(clip)
        flash(
            f"Already handled by {clip.reviewed_by.email if clip.reviewed_by else 'someone else'}.",
            "error",
        )
        return redirect("/suggestions/")

    flash("Suggestion rejected.", "success")
    return redirect("/suggestions/")
