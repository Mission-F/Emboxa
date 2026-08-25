from __future__ import annotations

import hmac
import re
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

from .config import APP_SECRET, ENCRYPTION_KEY, ENCRYPTION_KEY_FILE, SESSION_SECRET_FILE, load_or_create_secret

_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
_login_attempts: dict[str, deque[float]] = defaultdict(deque)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored: str, password: str) -> bool:
    try:
        return _hasher.verify(stored, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def password_needs_rehash(stored: str) -> bool:
    try:
        return _hasher.check_needs_rehash(stored)
    except InvalidHashError:
        return True


def get_session_secret() -> str:
    return APP_SECRET or load_or_create_secret(SESSION_SECRET_FILE, lambda: secrets.token_urlsafe(64)).decode()


def _fernet() -> Fernet:
    return Fernet(ENCRYPTION_KEY.encode() if ENCRYPTION_KEY else load_or_create_secret(ENCRYPTION_KEY_FILE, Fernet.generate_key))


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt_secret(value: str | None) -> str:
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("Impossibile decifrare la credenziale IMAP: chiave non valida") from exc


def make_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_matches(expected: str | None, received: str | None) -> bool:
    return bool(expected and received and hmac.compare_digest(expected, received))


def login_rate_limited(key: str, limit: int = 5, window_seconds: int = 900) -> bool:
    now = time.monotonic()
    attempts = _login_attempts[key]
    while attempts and attempts[0] < now - window_seconds:
        attempts.popleft()
    return len(attempts) >= limit


def record_failed_login(key: str) -> None:
    _login_attempts[key].append(time.monotonic())


def clear_login_failures(key: str) -> None:
    _login_attempts.pop(key, None)


def safe_filename(value: str | None, fallback: str = "attachment") -> str:
    name = Path(value or fallback).name.replace("\x00", "")
    name = re.sub(r"[\\/:*?\"<>|\r\n]+", "_", name).strip(" .")
    return (name[:240] or fallback)


def safe_resolve(base: Path, relative: str) -> Path:
    target = (base / relative).resolve()
    base_resolved = base.resolve()
    if target != base_resolved and base_resolved not in target.parents:
        raise ValueError("Percorso non valido")
    return target
