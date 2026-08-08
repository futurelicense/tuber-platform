"""Image-upload validation/storage for admin-uploaded proof attachments
(currently just channel listing proof images). Not a generic upload
framework — scoped to exactly what listings need.

Path safety comes from never using the caller's filename on disk: every
saved file gets a server-generated name (secrets.token_hex), so the
original filename is pure display metadata, never interpolated into a
filesystem path — a malicious "../../etc/passwd.png" original_filename is
just a harmless (HTML-escaped, by Jinja) string in the database.
"""
import io
import os
import secrets

from flask import current_app
from PIL import Image, UnidentifiedImageError

ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
ALLOWED_PIL_FORMATS = {"PNG", "JPEG", "WEBP"}


class UploadRejected(Exception):
    pass


def save_image(file_storage, prefix):
    """file_storage is a werkzeug FileStorage from request.files. Validates
    it's a genuine, parseable image of an allowed format (re-verified via
    Pillow, not trusted from the extension/declared content-type alone —
    a renamed non-image must not be accepted) and writes it under
    Config.LISTING_UPLOAD_DIR. Returns
    (filename, original_filename, content_type, size_bytes). Raises
    UploadRejected with a human-readable reason on any validation failure.
    """
    original_filename = file_storage.filename or ""
    ext = os.path.splitext(original_filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise UploadRejected(f"'{original_filename}': only PNG, JPG, and WEBP images are allowed.")

    raw = file_storage.read()
    if not raw:
        raise UploadRejected(f"'{original_filename}': file is empty.")

    try:
        img = Image.open(io.BytesIO(raw))
        img.verify()
        # verify() leaves the Image object unusable for anything else —
        # the format check needs a fresh open of the same bytes.
        fmt = Image.open(io.BytesIO(raw)).format
    except (UnidentifiedImageError, OSError):
        raise UploadRejected(f"'{original_filename}': not a valid image file.")

    if fmt not in ALLOWED_PIL_FORMATS:
        raise UploadRejected(f"'{original_filename}': unsupported image format ({fmt}).")

    upload_dir = current_app.config["LISTING_UPLOAD_DIR"]
    os.makedirs(upload_dir, exist_ok=True)

    filename = f"{prefix}-{secrets.token_hex(8)}{ext}"
    with open(os.path.join(upload_dir, filename), "wb") as f:
        f.write(raw)

    return filename, original_filename[:255], file_storage.content_type, len(raw)


def delete_image(filename):
    upload_dir = current_app.config["LISTING_UPLOAD_DIR"]
    path = os.path.join(upload_dir, filename)
    if os.path.isfile(path):
        os.remove(path)
