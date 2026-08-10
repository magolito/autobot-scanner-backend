"""
Password hashing — PBKDF2-HMAC-SHA256, stdlib only (hashlib, no new
dependency). 200,000 iterations is OWASP's current recommendation for
PBKDF2-SHA256 as of this writing; bump it later if that guidance changes,
the stored hash format doesn't need to change to support that (a proper
implementation would embed the iteration count in the stored string so
old hashes stay verifiable after a bump — done here specifically for
that reason, not just because it's tidy).
"""

from __future__ import annotations
import hashlib
import hmac
import os
import binascii

PBKDF2_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"{PBKDF2_ITERATIONS}:{binascii.hexlify(salt).decode()}:{binascii.hexlify(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time comparison, same reasoning as the dashboard's shared-
    password check — a login endpoint is exactly the kind of place a
    timing side-channel is worth closing properly, not just for the
    single-password case."""
    try:
        iterations_str, salt_hex, hash_hex = stored.split(":")
        iterations = int(iterations_str)
        salt = binascii.unhexlify(salt_hex)
    except (ValueError, binascii.Error):
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(binascii.hexlify(dk).decode(), hash_hex)
