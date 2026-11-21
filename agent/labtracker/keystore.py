"""Secret storage in the desktop keyring (GNOME Keyring / Secret Service).

On Linux Mint the login keyring is encrypted with your login password and
unlocked when you log in. Keys are never written to plain files.
"""
import json
from dataclasses import dataclass, asdict

from . import crypto

RECOVERY_PREFIX = "LTK1:"


@dataclass
class Keys:
    share: bytes      # encrypts the timeline (employer has this via the dashboard link)
    private: bytes    # encrypts notes and original values of adjusted entries (only you)
    auth: bytes       # signs requests (also stored in the Apps Script)
    read_token: str   # lets the dashboard fetch shared rows

    @classmethod
    def generate(cls):
        return cls(crypto.new_key(), crypto.new_key(), crypto.new_key(), crypto.new_token())

    def to_json(self) -> str:
        return json.dumps({
            "share": crypto.b64e(self.share), "private": crypto.b64e(self.private),
            "auth": crypto.b64e(self.auth), "read_token": self.read_token,
        })

    @classmethod
    def from_json(cls, text: str):
        d = json.loads(text)
        keys = cls(crypto.b64d(d["share"]), crypto.b64d(d["private"]),
                   crypto.b64d(d["auth"]), d["read_token"])
        for k in (keys.share, keys.private, keys.auth):
            if len(k) != crypto.KEY_BYTES:
                raise ValueError("invalid key length")
        return keys

    def recovery_code(self, user_id: str) -> str:
        d = json.loads(self.to_json())
        d["user"] = user_id
        return RECOVERY_PREFIX + crypto.b64e(json.dumps(d).encode("utf-8"))

    @classmethod
    def from_recovery_code(cls, code: str):
        code = "".join(code.split())
        if not code.startswith(RECOVERY_PREFIX):
            raise ValueError("not a Labtracker recovery code")
        d = json.loads(crypto.b64d(code[len(RECOVERY_PREFIX):]).decode("utf-8"))
        user = d.pop("user")
        return cls.from_json(json.dumps(d)), user


class KeyringError(RuntimeError):
    pass


class Keyring:
    """Stores the key bundle as one Secret Service item."""

    def __init__(self):
        try:
            import gi
            gi.require_version("Secret", "1")
            from gi.repository import Secret
        except (ImportError, ValueError) as e:
            raise KeyringError("The keyring library is missing (install gir1.2-secret-1).") from e
        self._Secret = Secret
        self._schema = Secret.Schema.new(
            "org.labtracker.Keys", Secret.SchemaFlags.NONE,
            {"app": Secret.SchemaAttributeType.STRING},
        )
        self._attrs = {"app": "labtracker"}

    def load(self):
        try:
            text = self._Secret.password_lookup_sync(self._schema, self._attrs, None)
        except Exception as e:  # GLib.Error
            raise KeyringError(f"Could not read the keyring: {e}") from e
        return Keys.from_json(text) if text else None

    def save(self, keys: Keys):
        try:
            ok = self._Secret.password_store_sync(
                self._schema, self._attrs, self._Secret.COLLECTION_DEFAULT,
                "Labtracker encryption keys", keys.to_json(), None)
        except Exception as e:
            raise KeyringError(f"Could not write to the keyring: {e}") from e
        if not ok:
            raise KeyringError("The keyring refused to store the keys.")

    def delete(self):
        try:
            self._Secret.password_clear_sync(self._schema, self._attrs, None)
        except Exception as e:
            raise KeyringError(f"Could not delete from the keyring: {e}") from e
