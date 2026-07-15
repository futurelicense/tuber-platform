from datetime import datetime, timezone

from ..extensions import db


class WatchedChannel(db.Model):
    """Admin-configured YouTube channel the suggest-agent polls for new
    uploads. Admin-only CRUD (not tuber-configurable in v1) — matches the
    platform's existing philosophy that admin configures the pipeline,
    clippers do the review/approve work.
    """

    __tablename__ = "watched_channels"

    id = db.Column(db.Integer, primary_key=True)
    channel_url = db.Column(db.String(500), nullable=False)
    label = db.Column(db.String(200))
    # Null means "generate suggestions for one default platform" — NOT fan
    # out across every _PLATFORM_CFG entry, which would be 5x the AI calls
    # per video for no asked-for benefit.
    platform_target = db.Column(db.String(30))
    added_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    last_checked_at = db.Column(db.DateTime(timezone=True))

    added_by = db.relationship("User", foreign_keys=[added_by_user_id])

    __table_args__ = (
        db.CheckConstraint(
            "platform_target is null or platform_target in "
            "('TikTok','Shorts','Reels','Twitter/X','LinkedIn')",
            name="ck_watched_channel_platform",
        ),
    )


class DiscoveredVideo(db.Model):
    """A video seen from a WatchedChannel. process_status (not a bare
    nullable timestamp) distinguishes "not attempted", "attempted but no
    transcript yet" (common right after upload — retry next run), "done",
    and "errored" (also retry next run), so nothing gets silently and
    permanently stuck with zero visibility into why.
    """

    __tablename__ = "discovered_videos"

    id = db.Column(db.Integer, primary_key=True)
    channel_watch_id = db.Column(
        db.Integer, db.ForeignKey("watched_channels.id"), nullable=False, index=True
    )
    video_url = db.Column(db.String(500), nullable=False)
    video_title = db.Column(db.String(500))
    discovered_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    process_status = db.Column(db.String(20), nullable=False, default="pending")

    channel = db.relationship("WatchedChannel", foreign_keys=[channel_watch_id])

    __table_args__ = (
        db.CheckConstraint(
            "process_status in ('pending','no_transcript','suggested','error')",
            name="ck_discovered_video_status",
        ),
        db.UniqueConstraint("channel_watch_id", "video_url", name="uq_video_per_channel"),
    )


class SuggestedClip(db.Model):
    """One AI-generated clip suggestion queued for a human clipper to
    review. Approving does NOT trigger a cut — it just marks the row and
    the review-queue UI links out to Clipper's own UI for the human to
    actually cut/publish.
    """

    __tablename__ = "suggested_clips"

    id = db.Column(db.Integer, primary_key=True)
    discovered_video_id = db.Column(
        db.Integer, db.ForeignKey("discovered_videos.id"), nullable=False, index=True
    )
    start = db.Column(db.Integer, nullable=False)
    end = db.Column(db.Integer, nullable=False)
    title = db.Column(db.String(300))
    reason = db.Column(db.Text)
    platform = db.Column(db.String(30))
    status = db.Column(db.String(20), nullable=False, default="pending")
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    reviewed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    reviewed_at = db.Column(db.DateTime(timezone=True))

    video = db.relationship("DiscoveredVideo", foreign_keys=[discovered_video_id])
    reviewed_by = db.relationship("User", foreign_keys=[reviewed_by_id])

    __table_args__ = (
        db.CheckConstraint(
            "status in ('pending','approved','rejected')", name="ck_suggested_clip_status"
        ),
    )
