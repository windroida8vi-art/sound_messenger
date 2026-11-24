"""
AES-256-GCM encryption with a passphrase-derived key (PBKDF2-HMAC-SHA256).
Same scheme as the Android app's Crypto.kt, so the two can decrypt each
other's messages given the same passphrase.

This is deliberately a well-known, publicly reviewed construction, not a
secret home-made cipher -- that is what makes it strong. What keeps a
message private is the passphrase, which only exists in your head and the
receiver's, never in the app or on the wire. Share it out of band (in
person, etc.), not over the same hidden channel.

Packed blob layout: 16-byte salt, then 12-byte IV, then the GCM ciphertext
(which already includes its own 16-byte authentication tag at the end).
"""

import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

SALT_BYTES = 16
IV_BYTES = 12
PBKDF2_ITERATIONS = 20_000
KEY_BYTES = 32  # 256 bits


class WrongPassphraseException(Exception):
    """Raised when the passphrase is wrong or the data was corrupted/tampered with."""

    def __init__(self):
        super().__init__("رمز اشتباه است یا داده خراب شده")


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=KEY_BYTES,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    return kdf.derive(passphrase.encode("utf-8"))


def encrypt(plaintext: bytes, passphrase: str) -> bytes:
    """Encrypts plaintext with passphrase; returns salt + iv + ciphertext (with its tag)."""
    salt = os.urandom(SALT_BYTES)
    iv = os.urandom(IV_BYTES)
    key = _derive_key(passphrase, salt)
    ciphertext = AESGCM(key).encrypt(iv, plaintext, None)
    return salt + iv + ciphertext


def decrypt(packed: bytes, passphrase: str) -> bytes:
    """Reverses encrypt(); raises WrongPassphraseException on a wrong passphrase or corrupt/tampered data."""
    if len(packed) < SALT_BYTES + IV_BYTES:
        raise WrongPassphraseException()
    salt = packed[:SALT_BYTES]
    iv = packed[SALT_BYTES:SALT_BYTES + IV_BYTES]
    ciphertext = packed[SALT_BYTES + IV_BYTES:]
    key = _derive_key(passphrase, salt)
    try:
        return AESGCM(key).decrypt(iv, ciphertext, None)
    except Exception:
        raise WrongPassphraseException()
