import os


def _normalized_database_uri():
    # Render's (and Heroku's) Postgres connectionString uses the "postgres://"
    # scheme, which SQLAlchemy 1.4+ no longer recognizes — it raises
    # NoSuchModuleError unless rewritten to "postgresql://". Without this,
    # every DB-touching route 500s while /healthz (no DB query) still passes.
    uri = os.environ.get("DATABASE_URL", "sqlite:///" + os.path.join(os.getcwd(), "dev.db"))
    if uri.startswith("postgres://"):
        uri = "postgresql://" + uri[len("postgres://"):]
    return uri


def _secret_key():
    # A guessable SECRET_KEY means forgeable session cookies, and the session
    # cookie's signature is the ONLY thing RoleGateMiddleware and the admin
    # blueprint trust — so a real deployment must never boot on the dev
    # fallback. DATABASE_URL being set is the "this is a real deployment"
    # signal (local dev runs on the sqlite default).
    key = os.environ.get("SECRET_KEY", "").strip()
    if key:
        return key
    if os.environ.get("DATABASE_URL"):
        raise RuntimeError(
            "SECRET_KEY is not set but DATABASE_URL is — refusing to start "
            "with the insecure dev fallback key. Set SECRET_KEY in the "
            "environment (Render: generateValue on the web service; copy the "
            "same value to the cron service)."
        )
    return "dev-only-insecure-key"


class Config:
    SECRET_KEY = _secret_key()
    SQLALCHEMY_DATABASE_URI = _normalized_database_uri()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # Supabase's pooler (like most managed Postgres) silently kills idle
    # connections after a few minutes; SQLAlchemy's pool would then hand the
    # dead socket to the next request, 500ing it ("server closed the
    # connection unexpectedly") while the retry works. pre_ping validates
    # each connection at checkout and replaces dead ones transparently;
    # recycle retires connections before typical idle-kill windows. Both are
    # no-ops for local SQLite.
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": 280,
    }

    PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000")
    GEO_API_URL = os.environ.get("GEO_API_URL", "http://ip-api.com/json")
    CHANNEL_TOKEN_ENC_KEY = os.environ.get("CHANNEL_TOKEN_ENC_KEY", "")

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    # Only force Secure cookies when actually served over https (ProxyFix sets
    # wsgi.url_scheme correctly behind Render's proxy; local dev stays http).
    SESSION_COOKIE_SECURE = PUBLIC_BASE_URL.startswith("https://")
