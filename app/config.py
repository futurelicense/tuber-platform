import os


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-insecure-key")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", "sqlite:///" + os.path.join(os.getcwd(), "dev.db")
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000")
    GEO_API_URL = os.environ.get("GEO_API_URL", "http://ip-api.com/json")
    CHANNEL_TOKEN_ENC_KEY = os.environ.get("CHANNEL_TOKEN_ENC_KEY", "")

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    # Only force Secure cookies when actually served over https (ProxyFix sets
    # wsgi.url_scheme correctly behind Render's proxy; local dev stays http).
    SESSION_COOKIE_SECURE = PUBLIC_BASE_URL.startswith("https://")
