# config_store.py — Encrypted local credential storage
#
# Uses Fernet symmetric encryption (AES-128-CBC + HMAC-SHA256).
# The key is machine-local (stored in KEY_FILE). If the key file is lost,
# the encrypted credentials cannot be recovered — users will re-enter them.
# Never commit KEY_FILE or CREDS_FILE to version control.

from __future__ import annotations
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from config import CREDS_FILE, KEY_FILE


def _get_or_create_key() -> bytes:
    """Return the local Fernet key, creating it if it doesn't exist."""
    path = Path(KEY_FILE)
    if path.exists():
        return path.read_bytes()
    key = Fernet.generate_key()
    path.write_bytes(key)
    # Restrict permissions on Unix systems
    try:
        os.chmod(KEY_FILE, 0o600)
    except OSError:
        pass
    return key


def credentials_saved() -> bool:
    """Return True if an encrypted credentials file exists."""
    return Path(CREDS_FILE).exists()


def save_credentials(connections: dict) -> None:
    """
    Encrypt and save the connections dict to disk.
    connections format: {"dev": {"host": ..., "user": ..., ...}, "qa": ..., "prod": ...}
    Passwords are included — keep CREDS_FILE and KEY_FILE out of version control.
    """
    key = _get_or_create_key()
    f = Fernet(key)
    plaintext = json.dumps(connections).encode("utf-8")
    encrypted = f.encrypt(plaintext)
    Path(CREDS_FILE).write_bytes(encrypted)
    try:
        os.chmod(CREDS_FILE, 0o600)
    except OSError:
        pass


def load_credentials() -> dict | None:
    """
    Decrypt and return the saved connections dict.
    Returns None if the file doesn't exist or decryption fails.
    """
    if not Path(CREDS_FILE).exists():
        return None
    try:
        key = _get_or_create_key()
        f = Fernet(key)
        encrypted = Path(CREDS_FILE).read_bytes()
        plaintext = f.decrypt(encrypted)
        return json.loads(plaintext.decode("utf-8"))
    except (InvalidToken, json.JSONDecodeError, OSError):
        return None


def delete_credentials() -> None:
    """Remove saved credentials from disk."""
    try:
        Path(CREDS_FILE).unlink(missing_ok=True)
    except OSError:
        pass
