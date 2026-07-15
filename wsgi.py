"""Gunicorn entrypoint: `gunicorn wsgi:application`.

Builds the platform Flask app, then wraps it with the vendored sub-apps
mounted at /clip and /produce via app.mounting.dispatcher.build_wsgi_app.
"""
import os

from dotenv import load_dotenv

# Load the platform's own .env before anything else reads os.environ —
# neither app/config.py nor the vendored Clipper's .env-loader (which looks
# relative to its own file, i.e. vendor/youtube-clipper/.env, not this repo's
# root .env) ever picked this up automatically. A no-op in production (Render
# injects env vars directly; there's no .env file there to find).
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from app import create_app  # noqa: E402
from app.mounting.dispatcher import build_wsgi_app  # noqa: E402

platform_app = create_app()
application = build_wsgi_app(platform_app)

if __name__ == "__main__":
    from werkzeug.serving import run_simple

    run_simple("0.0.0.0", 8000, application, use_reloader=True, use_debugger=True)
