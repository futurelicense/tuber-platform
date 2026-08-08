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


def _channel_token_enc_key():
    # Without this key, app/crypto.py falls back to storing
    # ConnectedChannel.token_blob (real OAuth tokens, once that flow is
    # wired up) as plain JSON instead of Fernet-encrypted — silently, since
    # encrypt_token_blob() has no other signal that encryption was skipped.
    # Same "DATABASE_URL means real deployment" rule as SECRET_KEY: refuse to
    # boot rather than risk a misconfigured prod writing tokens in the clear.
    key = os.environ.get("CHANNEL_TOKEN_ENC_KEY", "").strip()
    if key or not os.environ.get("DATABASE_URL"):
        return key
    raise RuntimeError(
        "CHANNEL_TOKEN_ENC_KEY is not set but DATABASE_URL is — refusing to "
        "start without it, since app/crypto.py would silently store "
        "connected-channel OAuth tokens unencrypted. Set CHANNEL_TOKEN_ENC_KEY "
        "in the environment (Render: generateValue on the web service; copy "
        "the same value to the cron service)."
    )


def _paystack_secret_key():
    # Master Class checkout (initializing/verifying transactions) and
    # webhook signature verification both need this. Same "DATABASE_URL
    # means real deployment" rule as SECRET_KEY/CHANNEL_TOKEN_ENC_KEY:
    # refuse to boot rather than silently run with payments broken or an
    # unverifiable webhook endpoint.
    key = os.environ.get("PAYSTACK_SECRET_KEY", "").strip()
    if key or not os.environ.get("DATABASE_URL"):
        return key
    raise RuntimeError(
        "PAYSTACK_SECRET_KEY is not set but DATABASE_URL is — refusing to "
        "start without it, since the Master Class checkout and webhook "
        "signature verification both require it. Set PAYSTACK_SECRET_KEY "
        "in the environment."
    )


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

    # No expiry on CSRF tokens (they stay session-bound and signed) — the
    # default 1-hour limit turned a login tab left open, or a browser-cached
    # login page, into "Bad Request: The CSRF token has expired" on submit.
    WTF_CSRF_TIME_LIMIT = None

    PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000")
    GEO_API_URL = os.environ.get("GEO_API_URL", "http://ip-api.com/json")
    CHANNEL_TOKEN_ENC_KEY = _channel_token_enc_key()
    PAYSTACK_SECRET_KEY = _paystack_secret_key()
    PAYSTACK_PUBLIC_KEY = os.environ.get("PAYSTACK_PUBLIC_KEY", "")

    # Same env-configurable-with-local-fallback shape as the vendored
    # Clipper's CLIPPER_DOWNLOAD_DIR (vendor/youtube-clipper/app.py) — falls
    # back to a relative dir for local dev, points at the Render disk
    # already mounted at /app/data in production (see render.yaml).
    LISTING_UPLOAD_DIR = os.environ.get("LISTING_UPLOAD_DIR") or os.path.join(
        os.getcwd(), "listing_uploads"
    )
    # Caps total request body size — a payment-adjacent upload form is a
    # plausible DoS target otherwise. 15MB comfortably fits the 6-image
    # per-listing cap enforced in app/admin/routes.py.
    MAX_CONTENT_LENGTH = 15 * 1024 * 1024

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    # Only force Secure cookies when actually served over https (ProxyFix sets
    # wsgi.url_scheme correctly behind Render's proxy; local dev stays http).
    SESSION_COOKIE_SECURE = PUBLIC_BASE_URL.startswith("https://")
