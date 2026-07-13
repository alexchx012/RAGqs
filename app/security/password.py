"""Stateless password hashing and verification for local_credentials auth."""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerificationError

_hasher = PasswordHasher()


def hash_password(plain: str) -> str:
    """Hash a plaintext password using argon2 with library-default parameters."""

    return _hasher.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Return True when ``plain`` matches the argon2 ``hashed`` digest.

    Never raises: any verification failure (wrong password, malformed hash)
    is reported as ``False`` so callers can treat this as a plain predicate.
    """

    try:
        return _hasher.verify(hashed, plain)
    except (VerificationError, InvalidHash):
        return False
