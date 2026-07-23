"""Password hashing and session helpers for optional UI protection.

Passwords are never stored in plaintext: only a salted PBKDF2-HMAC-SHA256 hash
is written to the config file.  A per-install random secret signs the session
cookie (via Starlette's ``SessionMiddleware``).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets

_ITERATIONS = 200_000


def hash_password(password: str, iterations: int = _ITERATIONS) -> str:
    """Return a ``pbkdf2$iterations$salt_hex$hash_hex`` string for *password*."""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    """Constant-time check of *password* against a stored hash."""
    if not stored:
        return False
    try:
        algo, iter_s, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iter_s)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), hash_hex)


def generate_secret() -> str:
    """A random secret suitable for signing session cookies."""
    return secrets.token_hex(32)
