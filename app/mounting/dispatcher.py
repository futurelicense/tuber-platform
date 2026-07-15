"""Assembles the platform Flask app and the two vendored sub-apps into one
WSGI callable via werkzeug's DispatcherMiddleware:

    /          -> platform app (login, admin dashboard)
    /clip      -> Youtube-Clipper, gated to clipper/admin
    /produce   -> ytproduction, gated to producer/admin

Each mounted sub-app is wrapped, closest-to-furthest from the real app:
    RoleGateMiddleware      (auth/role check + coarse activity log — runs first)
    PrefixRewriteMiddleware (rewrites root-relative URLs in the response — runs last)
"""
import importlib.util
import os
import sys
from pathlib import Path

from werkzeug.middleware.dispatcher import DispatcherMiddleware
from werkzeug.middleware.proxy_fix import ProxyFix

from .prefix_rewrite import PrefixRewriteMiddleware
from .role_gate import RoleGateMiddleware

VENDOR_DIR = Path(__file__).resolve().parent.parent.parent / "vendor"

CLIPPER_ROUTES = [
    "/info",
    "/clip",
    "/progress/",
    "/download/",
    "/download_video",
    "/script/",
    "/suggest",
    "/google/",
    "/gdrive/",
    "/youtube/",
    "/tiktok/",
    "/stream/",
    "/transcript",
]
YTPROD_ROUTES = [
    "/generate",
    "/progress/",
    "/upload-section/",
    "/assemble",
    "/upload-music",
    "/thumbnail",
    "/assemble-progress",
    "/result/",
    "/download/",
    "/job-state/",
]


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_clipper_app():
    # Read at module-import time by the vendored app (OAuth redirect URIs).
    os.environ.setdefault("CLIPPER_MOUNT_PREFIX", "/clip")
    return _load_module("vendored_clipper", VENDOR_DIR / "youtube-clipper" / "app.py").app


def load_ytproduction_app():
    return _load_module("vendored_ytproduction", VENDOR_DIR / "ytproduction" / "app.py").app


def build_wsgi_app(platform_app):
    clipper_app = load_clipper_app()
    ytprod_app = load_ytproduction_app()

    clip_wsgi = RoleGateMiddleware(
        clipper_app.wsgi_app, platform_app, section="clip", allowed_roles=("clipper",)
    )
    clip_wsgi = PrefixRewriteMiddleware(clip_wsgi, "/clip", CLIPPER_ROUTES)

    produce_wsgi = RoleGateMiddleware(
        ytprod_app.wsgi_app, platform_app, section="produce", allowed_roles=("producer",)
    )
    produce_wsgi = PrefixRewriteMiddleware(produce_wsgi, "/produce", YTPROD_ROUTES)

    dispatched = DispatcherMiddleware(
        platform_app.wsgi_app,
        {"/clip": clip_wsgi, "/produce": produce_wsgi},
    )

    return ProxyFix(dispatched, x_for=1, x_proto=1, x_host=1)
