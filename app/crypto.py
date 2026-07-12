"""Encrypt-at-rest helper for storing OAuth tokens (ConnectedChannel.token_blob).

Not wired to any real OAuth flow yet (Phase 1 leaves connected_channels as a
schema placeholder) — this exists so the column format doesn't need to change
later when real tokens start landing in it.
"""
import json
import os

from cryptography.fernet import Fernet, InvalidToken


def _fernet():
    key = os.environ.get("CHANNEL_TOKEN_ENC_KEY", "").strip()
    if not key:
        return None
    return Fernet(key.encode())


def encrypt_token_blob(data: dict) -> str:
    f = _fernet()
    raw = json.dumps(data).encode()
    if not f:
        # No key configured (e.g. local dev) — store as plain JSON, not encrypted.
        return raw.decode()
    return f.encrypt(raw).decode()


def decrypt_token_blob(stored: str) -> dict:
    f = _fernet()
    if not f:
        return json.loads(stored)
    try:
        raw = f.decrypt(stored.encode())
    except InvalidToken:
        # Fall back to treating it as unencrypted plain JSON (dev-mode data).
        return json.loads(stored)
    return json.loads(raw)
