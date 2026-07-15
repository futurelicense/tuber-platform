import os

import click
from flask import Flask

from .config import Config
from .extensions import db, login_manager, migrate


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    db.init_app(app)
    login_manager.init_app(app)
    migrate.init_app(app, db)

    from .models import User

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from .auth import bp as auth_bp
    from .admin import bp as admin_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)

    @app.route("/healthz")
    def healthz():
        return {"status": "ok"}

    register_cli(app)

    return app


def register_cli(app):
    @app.cli.command("create-admin")
    @click.argument("email")
    @click.argument("password")
    def create_admin(email, password):
        """Bootstrap the first admin account: flask create-admin you@example.com yourpassword"""
        from .models import User

        email = email.strip().lower()
        if User.query.filter_by(email=email).first():
            click.echo(f"User {email} already exists.")
            return
        user = User(email=email, role="admin", display_name="Admin")
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        click.echo(f"Created admin {email}.")

    @app.cli.command("run-suggest-agent")
    def run_suggest_agent():
        """Poll active WatchedChannels for new uploads and queue AI clip
        suggestions for a human clipper to review. Never cuts or publishes
        anything itself — suggest-only.
        """
        import sys as _sys
        from datetime import datetime, timezone

        from .models import WatchedChannel, DiscoveredVideo, SuggestedClip

        # FLASK_APP=wsgi:platform_app means wsgi.py already called
        # build_wsgi_app() -> load_clipper_app() before this command body
        # runs, so the module is very likely already loaded. Only load it
        # ourselves if that hasn't happened, to avoid a wasteful full re-exec.
        clipper = _sys.modules.get("vendored_clipper")
        if clipper is None:
            from .mounting.dispatcher import load_clipper_app

            load_clipper_app()
            clipper = _sys.modules["vendored_clipper"]

        channels = WatchedChannel.query.filter_by(is_active=True).all()
        click.echo(f"run-suggest-agent: checking {len(channels)} active channel(s)")

        total_new_videos = 0
        total_suggestions = 0

        for wc in channels:
            label = wc.label or wc.channel_url
            try:
                uploads = clipper._list_channel_uploads(wc.channel_url, limit=10)
            except Exception as e:
                click.echo(f"  [{label}] channel listing failed: {e}")
                continue

            wc.last_checked_at = datetime.now(timezone.utc)
            db.session.commit()

            # Null platform_target means "one default platform", not a fan-out
            # across every _PLATFORM_CFG entry (that would be 5x the AI calls
            # per video for no asked-for benefit).
            platform = wc.platform_target or "TikTok"

            for v in uploads:
                video = DiscoveredVideo.query.filter_by(
                    channel_watch_id=wc.id, video_url=v["url"]
                ).first()

                if video is None:
                    video = DiscoveredVideo(
                        channel_watch_id=wc.id,
                        video_url=v["url"],
                        video_title=v["title"],
                        process_status="pending",
                    )
                    db.session.add(video)
                    db.session.flush()  # need video.id for SuggestedClip rows below
                    total_new_videos += 1
                elif video.process_status == "suggested":
                    continue  # already processed successfully, nothing to do

                try:
                    cues = clipper._fetch_raw_transcript(video.video_url)
                    if not cues:
                        video.process_status = "no_transcript"
                        db.session.commit()
                        continue
                    clips = clipper._hf_suggest_clips(cues, platform, n=5)
                except Exception as e:
                    video.process_status = "error"
                    db.session.commit()
                    click.echo(f"  [{video.video_title}] suggest failed: {e}")
                    continue

                for c in clips:
                    db.session.add(SuggestedClip(
                        discovered_video_id=video.id,
                        start=c["start"],
                        end=c["end"],
                        title=c.get("title", ""),
                        reason=c.get("reason", ""),
                        platform=platform,
                    ))
                video.process_status = "suggested"
                db.session.commit()
                total_suggestions += len(clips)

        click.echo(
            f"run-suggest-agent: done — {total_new_videos} new video(s), "
            f"{total_suggestions} suggestion(s) queued"
        )
