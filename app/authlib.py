"""
Password hashing (PBKDF2-HMAC-SHA256) and TOTP (RFC 6238), implemented
with the Python standard library only, dependency-free by design.
Used by the web app (webapp.py) for setup, login and user management.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import struct
import time

# ---------------------------------------------------------------------------
# Password hashing (PBKDF2-HMAC-SHA256)
# ---------------------------------------------------------------------------

_PBKDF2_ITERATIONS = 260_000
_PBKDF2_ALGO = "sha256"


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac(_PBKDF2_ALGO, password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        _PBKDF2_ITERATIONS, base64.b64encode(salt).decode(), base64.b64encode(dk).decode()
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt_b64, hash_b64 = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        iterations = int(iterations)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
    except Exception:
        return False
    dk = hashlib.pbkdf2_hmac(_PBKDF2_ALGO, password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(dk, expected)


# ---------------------------------------------------------------------------
# TOTP (RFC 6238), 30-second step, 6 digits, SHA1 (the widest-compatible
# choice for authenticator apps like Google Authenticator / Aegis / etc.)
# ---------------------------------------------------------------------------

_TOTP_STEP = 30
_TOTP_DIGITS = 6


def generate_totp_secret() -> str:
    """Base32-encoded random secret, suitable for an authenticator app."""
    return base64.b32encode(os.urandom(20)).decode("utf-8").rstrip("=")


def _hotp(secret_b32: str, counter: int) -> str:
    key = base64.b32decode(secret_b32 + "=" * ((8 - len(secret_b32) % 8) % 8))
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** _TOTP_DIGITS)
    return str(code_int).zfill(_TOTP_DIGITS)


def totp_now(secret_b32: str, for_time: float | None = None) -> str:
    t = for_time if for_time is not None else time.time()
    counter = int(t // _TOTP_STEP)
    return _hotp(secret_b32, counter)


def verify_totp(secret_b32: str, code: str, valid_window: int = 1) -> bool:
    if not code or not code.isdigit():
        return False
    now = time.time()
    counter = int(now // _TOTP_STEP)
    for offset in range(-valid_window, valid_window + 1):
        if hmac.compare_digest(_hotp(secret_b32, counter + offset), code.strip()):
            return True
    return False


def new_device_token() -> str:
    """Random token for the "trusted device" cookie (256 bits)."""
    return secrets.token_urlsafe(32)


def hash_device_token(token: str) -> str:
    """Only this hash is stored server-side, so a copy of the database can't
    be used to forge a trusted-device cookie. Plain SHA-256 is right for a
    high-entropy random token (a slow password KDF would add nothing)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def provisioning_uri(secret_b32: str, username: str, issuer: str = "BackupMonitor") -> str:
    from urllib.parse import quote

    label = quote(f"{issuer}:{username}")
    return (
        f"otpauth://totp/{label}?secret={secret_b32}"
        f"&issuer={quote(issuer)}&digits={_TOTP_DIGITS}&period={_TOTP_STEP}"
    )
