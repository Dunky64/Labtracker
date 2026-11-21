"""Encryption and signing.

- AES-256-GCM (authenticated encryption) for event contents.
- HMAC-SHA256 for request signing.
Both come from well-reviewed implementations (OpenSSL via `cryptography`,
and the Python standard library). Nothing here is home-made cryptography.
"""
import base64
import hashlib
import hmac
import json
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_BYTES = 32
NONCE_BYTES = 12


def b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")


def b64d(text: str) -> bytes:
    text = text.strip()
    text += "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text.encode("ascii"))


def new_key() -> bytes:
    return secrets.token_bytes(KEY_BYTES)


def new_token() -> str:
    return b64e(secrets.token_bytes(32))


def aad(user: str, row_id: str, key_id: str) -> bytes:
    """Binds a ciphertext to its row, so rows cannot be swapped or relabelled."""
    return f"labtracker/v1|{user}|{row_id}|{key_id}".encode("utf-8")


def seal(key: bytes, obj: dict, user: str, row_id: str, key_id: str):
    nonce = secrets.token_bytes(NONCE_BYTES)
    plaintext = json.dumps(obj, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ct = AESGCM(key).encrypt(nonce, plaintext, aad(user, row_id, key_id))
    return b64e(nonce), b64e(ct)


def unseal(key: bytes, nonce: str, ct: str, user: str, row_id: str, key_id: str) -> dict:
    plaintext = AESGCM(key).decrypt(b64d(nonce), b64d(ct), aad(user, row_id, key_id))
    return json.loads(plaintext.decode("utf-8"))


def sign(auth_key: bytes, ts: int, nonce: str, payload: str) -> str:
    msg = f"v1\n{ts}\n{nonce}\n{payload}".encode("utf-8")
    return b64e(hmac.new(auth_key, msg, hashlib.sha256).digest())
