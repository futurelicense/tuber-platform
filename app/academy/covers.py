"""Resolve Academy course cover URLs with durable static fallbacks.

Admin uploads live under LISTING_UPLOAD_DIR and are referenced as
`/marketplace/uploads/<filename>`. On Render those files only survive when the
persistent disk is mounted; otherwise the DB URL 404s. This helper falls back
to repo-bundled SVGs so cards always show artwork.
"""

from __future__ import annotations

import os

from flask import current_app, url_for

from .sample_course import SAMPLE_SLUG

_STATIC_BY_CATALOG = {
    "tool": "academy/covers/tool.svg",
    "use_case": "academy/covers/use_case.svg",
    "challenge": "academy/covers/challenge.svg",
}
_STATIC_BY_SLUG = {
    SAMPLE_SLUG: "academy/covers/chatgpt.svg",
}


def default_cover_static_path(course):
    slug = getattr(course, "slug", None) or ""
    if slug in _STATIC_BY_SLUG:
        return _STATIC_BY_SLUG[slug]
    catalog = getattr(course, "catalog", None) or "tool"
    return _STATIC_BY_CATALOG.get(catalog, _STATIC_BY_CATALOG["tool"])


def _upload_filename_from_url(url):
    if not url:
        return None
    # Relative app URL or absolute URL ending in /marketplace/uploads/<file>
    marker = "/marketplace/uploads/"
    if marker not in url:
        return None
    name = url.split(marker, 1)[1].split("?", 1)[0].strip("/")
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    return name


def upload_file_exists(filename):
    if not filename:
        return False
    upload_dir = current_app.config.get("LISTING_UPLOAD_DIR") or ""
    path = os.path.join(upload_dir, filename)
    return os.path.isfile(path)


def resolve_course_cover_url(course):
    """Return a working cover URL for templates (upload if present, else static)."""
    raw = (getattr(course, "cover_image_url", None) or "").strip()
    if raw:
        filename = _upload_filename_from_url(raw)
        if filename:
            if upload_file_exists(filename):
                return url_for("marketplace.uploaded_file", filename=filename)
            # Broken upload reference — use static fallback
        elif raw.startswith("/static/") or raw.startswith("http://") or raw.startswith("https://"):
            return raw
        elif not raw.startswith("/"):
            # Allow storing static relative paths like academy/covers/tool.svg
            return url_for("static", filename=raw)

    return url_for("static", filename=default_cover_static_path(course))


def repair_missing_upload_covers():
    """Point courses with missing upload files at durable static covers.

    Returns number of rows updated.
    """
    from ..extensions import db
    from ..models import AcademyCourse

    updated = 0
    for course in AcademyCourse.query.all():
        raw = (course.cover_image_url or "").strip()
        filename = _upload_filename_from_url(raw)
        if filename and not upload_file_exists(filename):
            course.cover_image_url = default_cover_static_path(course)
            updated += 1
        elif not raw:
            course.cover_image_url = default_cover_static_path(course)
            updated += 1
    if updated:
        db.session.commit()
    return updated
