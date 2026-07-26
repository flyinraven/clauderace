"""Password hashing, JWT issuing/verification, and secret encryption."""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

import bcrypt
import jwt
from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

ALGORITHM = "HS256"


# --- Passwords ------------------------------------------------------------
def _prehash(password: str) -> bytes:
    """bcrypt silently ignores bytes past 72; SHA-256 first so long
    passphrases are fully honoured."""
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return base64.b64encode(digest)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_prehash(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(_prehash(password), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# --- Tokens ---------------------------------------------------------------
def create_access_token(subject: str | int, role: str, ttl_minutes: int | None = None) -> str:
    ttl = ttl_minutes or settings.access_token_ttl_minutes
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(subject),
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=ttl)).timestamp()),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None


def generate_invite_code() -> str:
    """Short, unambiguous, case-insensitive code (no O/0/I/1)."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "-".join(
        "".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3)
    )


# --- Secret encryption ----------------------------------------------------
_KEY_FILE = Path(".fernet_key")


def _load_fernet() -> Fernet:
    """Resolve the encryption key for the `settings` table.

    In production `SETTINGS_ENCRYPTION_KEY` must be set - Render's filesystem is
    ephemeral, so a generated key would be lost on redeploy and every stored API
    key would become undecryptable.
    """
    key = settings.settings_encryption_key
    if not key:
        if _KEY_FILE.exists():
            key = _KEY_FILE.read_text(encoding="utf-8").strip()
        else:
            key = Fernet.generate_key().decode("utf-8")
            _KEY_FILE.write_text(key, encoding="utf-8")
    return Fernet(key.encode("utf-8") if isinstance(key, str) else key)


_fernet: Fernet | None = None


def get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = _load_fernet()
    return _fernet


def encrypt_secret(plaintext: str) -> str:
    return get_fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(ciphertext: str) -> str | None:
    try:
        return get_fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        return None


def mask_secret(plaintext: str | None) -> str:
    """Preview shown in the admin UI - never the full key."""
    if not plaintext:
        return ""
    if len(plaintext) <= 8:
        return "*" * len(plaintext)
    return f"{plaintext[:4]}{'*' * 8}{plaintext[-4:]}"
