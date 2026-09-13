"""Encryption at rest for the Kraken API key/secret, the Anthropic API key, and
the dashboard Basic-Auth password.

Everything sensitive is Fernet-encrypted with a key derived from the MASTER_KEY
env var (set on the droplet via systemd's EnvironmentFile — never committed).
Encrypted blobs are stored in the `secrets` kv table, never in config.json, and
GET endpoints must never echo the plaintext back to the browser — only a
"configured" flag and the last 4 characters, via SecretStatus below.

Without MASTER_KEY set, the store refuses to start (better to fail loudly than
run with unencrypted secrets or silently generate a throwaway key that changes
every restart and orphans previously-stored secrets).
"""

import base64
import hashlib
import os
import secrets as _pysecrets

from cryptography.fernet import Fernet, InvalidToken

MASTER_KEY_ENV = "MASTER_KEY"


class SecretsUnavailable(Exception):
    pass


def _derive_fernet_key(master_key: str) -> bytes:
    # Fernet needs a url-safe base64 32-byte key; accept any passphrase length.
    digest = hashlib.sha256(master_key.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


class SecretsStore:
    def __init__(self, db):
        self._db = db
        master = os.environ.get(MASTER_KEY_ENV)
        if not master:
            raise SecretsUnavailable(
                f"{MASTER_KEY_ENV} env var is not set. Generate one with "
                f"`python -c \"import secrets;print(secrets.token_urlsafe(32))\"` "
                f"and set it before starting the service."
            )
        self._fernet = Fernet(_derive_fernet_key(master))

    def _encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def _decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken as e:
            raise SecretsUnavailable(
                "Stored secret could not be decrypted — MASTER_KEY may have changed."
            ) from e

    # ---- generic secret slots -----------------------------------------
    def set_secret(self, name: str, value: str):
        self._db.kv_set(f"secret:{name}", self._encrypt(value))

    def get_secret(self, name: str, default=None):
        token = self._db.kv_get(f"secret:{name}")
        if not token:
            return default
        return self._decrypt(token)

    def has_secret(self, name: str) -> bool:
        return bool(self._db.kv_get(f"secret:{name}"))

    def status(self, name: str) -> dict:
        val = self.get_secret(name)
        if not val:
            return {"configured": False, "last4": None}
        return {"configured": True, "last4": val[-4:] if len(val) >= 4 else "****"}

    def clear_secret(self, name: str):
        self._db.kv_delete(f"secret:{name}")

    # ---- named accessors -------------------------------------------------
    def kraken_credentials(self):
        return self.get_secret("kraken_api_key", ""), self.get_secret("kraken_api_secret", "")

    def set_kraken_credentials(self, key: str, secret: str):
        self.set_secret("kraken_api_key", key)
        self.set_secret("kraken_api_secret", secret)

    def anthropic_api_key(self):
        return self.get_secret("anthropic_api_key", "")

    def set_anthropic_api_key(self, key: str):
        self.set_secret("anthropic_api_key", key)

    def dashboard_password(self):
        return self.get_secret("dashboard_password")

    def set_dashboard_password(self, password: str):
        self.set_secret("dashboard_password", password)

    def dashboard_username(self):
        return self.get_secret("dashboard_username", "admin")

    def set_dashboard_username(self, username: str):
        self.set_secret("dashboard_username", username)

    def ensure_dashboard_password(self) -> str | None:
        """If no dashboard password is set yet, generate one and return it
        (once) so the caller can print it to the service log on first boot."""
        if self.has_secret("dashboard_password"):
            return None
        generated = _pysecrets.token_urlsafe(16)
        self.set_dashboard_password(generated)
        return generated
