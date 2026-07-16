# SPDX-License-Identifier: MIT
"""Fernet encryption for the Plaid access_tokens at rest.

The access_token is the credential that lets us pull a bank's transactions
indefinitely, so it never lands in the DB in plaintext. We encrypt it with a
symmetric Fernet key:

  * If FERNET_KEY is set (env or test_config), we use it verbatim.
  * Otherwise we autogenerate a key ONCE on first boot and persist it to
    {DATA_DIR}/fernet.key (0600), next to the other JSON settings — the same
    trust boundary as the rest of the single-tenant pilot's data volume.

Losing the key means the stored access_tokens can't be decrypted and every
bank has to be re-linked, so fernet.key is part of the backup surface (and is
gitignored). The key is cached per-process after first load."""
import logging
import os

from flask import current_app

log = logging.getLogger('bankbridge.crypto')

_KEY_FILENAME = 'fernet.key'
_cached_fernet = None


def _key_path() -> str:
    return os.path.join(current_app.config['DATA_DIR'], _KEY_FILENAME)


def _load_or_create_key() -> bytes:
    """Resolve the Fernet key: configured value wins; else the persisted file;
    else generate + persist a fresh one. Returns the url-safe base64 key bytes."""
    configured = (current_app.config.get('FERNET_KEY') or '').strip()
    if configured:
        return configured.encode('utf-8')

    path = _key_path()
    try:
        with open(path, 'rb') as f:
            data = f.read().strip()
        if data:
            return data
    except (FileNotFoundError, OSError):
        pass

    from cryptography.fernet import Fernet
    key = Fernet.generate_key()
    os.makedirs(current_app.config['DATA_DIR'], exist_ok=True)
    # 0600 — readable only by the container user.
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(key)
    log.info('generated a new Fernet key at %s', path)
    return key


def _fernet():
    """Cached Fernet instance for this process."""
    global _cached_fernet
    if _cached_fernet is None:
        from cryptography.fernet import Fernet
        _cached_fernet = Fernet(_load_or_create_key())
    return _cached_fernet


def reset_cache() -> None:
    """Drop the cached key/instance — used by tests that swap DATA_DIR."""
    global _cached_fernet
    _cached_fernet = None


def encrypt(plaintext: str) -> str:
    """Encrypt a token string → url-safe token string for DB storage."""
    return _fernet().encrypt((plaintext or '').encode('utf-8')).decode('utf-8')


def decrypt(token: str) -> str:
    """Decrypt a stored token string → plaintext. Raises
    cryptography.fernet.InvalidToken if the ciphertext or key is wrong."""
    return _fernet().decrypt((token or '').encode('utf-8')).decode('utf-8')
