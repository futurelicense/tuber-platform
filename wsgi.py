"""Gunicorn entrypoint: `gunicorn wsgi:application`.

Builds the platform Flask app, then wraps it with the vendored sub-apps
mounted at /clip and /produce via app.mounting.dispatcher.build_wsgi_app.
"""
from app import create_app
from app.mounting.dispatcher import build_wsgi_app

platform_app = create_app()
application = build_wsgi_app(platform_app)

if __name__ == "__main__":
    from werkzeug.serving import run_simple

    run_simple("0.0.0.0", 8000, application, use_reloader=True, use_debugger=True)
