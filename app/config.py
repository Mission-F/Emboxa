from __future__ import annotations

import os
from pathlib import Path


def env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


DATA_DIR = Path(os.getenv("DATA_DIR", "/data")).resolve()
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'db' / 'mailvault.db'}")
SESSION_SECRET_FILE = Path(os.getenv("SESSION_SECRET_FILE", DATA_DIR / "secrets" / "session.key"))
ENCRYPTION_KEY_FILE = Path(os.getenv("ENCRYPTION_KEY_FILE", DATA_DIR / "secrets" / "fernet.key"))
APP_SECRET = os.getenv("APP_SECRET", "")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "")
COOKIE_SECURE = env_bool("COOKIE_SECURE", False)
IMPORT_MAX_BYTES = int(os.getenv("IMPORT_MAX_BYTES", str(10 * 1024**3)))
IMPORT_MAX_EXPANDED_BYTES = int(os.getenv("IMPORT_MAX_EXPANDED_BYTES", str(50 * 1024**3)))
IMAP_TIMEOUT_SECONDS = int(os.getenv("IMAP_TIMEOUT_SECONDS", "60"))
IMAP_FETCH_BATCH = max(1, int(os.getenv("IMAP_FETCH_BATCH", "20")))
BACKUP_RETRIES = max(1, int(os.getenv("BACKUP_RETRIES", "3")))
BACKUP_ANOMALY_THRESHOLD = min(0.9, max(0.05, float(os.getenv("BACKUP_ANOMALY_THRESHOLD", "0.20"))))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
PUBLIC_APP_URL = os.getenv("PUBLIC_APP_URL", "https://emboxa.eu").rstrip("/")
GITHUB_REPOSITORY_URL = os.getenv("GITHUB_REPOSITORY_URL", "https://github.com/Mission-F/Emboxa").rstrip("/")
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_SECURITY = os.getenv("SMTP_SECURITY", "starttls")
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", "EMBOXA")
SMTP_FROM_EMAIL = os.getenv("SMTP_FROM_EMAIL", "")
STANDARD_STORAGE_LIMIT_BYTES = int(os.getenv("STANDARD_STORAGE_LIMIT_BYTES", str(15 * 1024**3)))
STANDARD_MAILBOX_LIMIT = int(os.getenv("STANDARD_MAILBOX_LIMIT", "5"))
STANDARD_RETENTION_DAYS = int(os.getenv("STANDARD_RETENTION_DAYS", "30"))
PERMANENT_MAILBOX_LOCK_DAYS = int(os.getenv("PERMANENT_MAILBOX_LOCK_DAYS", "31"))
EXPORT_TTL_HOURS = int(os.getenv("EXPORT_TTL_HOURS", "24"))
GOOGLE_ANALYTICS_ID = os.getenv("GOOGLE_ANALYTICS_ID", "")
LEGAL_ENTITY_NAME = os.getenv("LEGAL_ENTITY_NAME", "")
LEGAL_ADDRESS = os.getenv("LEGAL_ADDRESS", "")
LEGAL_VAT_ID = os.getenv("LEGAL_VAT_ID", "")
LEGAL_CONTACT_EMAIL = os.getenv("LEGAL_CONTACT_EMAIL", "info@missionf.it")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "")
MICROSOFT_CLIENT_ID = os.getenv("MICROSOFT_CLIENT_ID", "")
MICROSOFT_CLIENT_SECRET = os.getenv("MICROSOFT_CLIENT_SECRET", "")
MICROSOFT_TENANT = os.getenv("MICROSOFT_TENANT", "common")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

ARCHIVES_DIR = DATA_DIR / "archives"
EXPORTS_DIR = DATA_DIR / "exports"
IMPORTS_DIR = DATA_DIR / "imports"
SECRETS_DIR = DATA_DIR / "secrets"


def ensure_data_dirs() -> None:
    for path in (DATA_DIR / "db", ARCHIVES_DIR, EXPORTS_DIR, IMPORTS_DIR, SECRETS_DIR):
        path.mkdir(parents=True, exist_ok=True)
    try:
        SECRETS_DIR.chmod(0o700)
    except OSError:
        pass


def load_or_create_secret(path: Path, generator) -> bytes:
    ensure_data_dirs()
    if not path.exists() or path.stat().st_size == 0:
        value = generator()
        if isinstance(value, str):
            value = value.encode()
        path.write_bytes(value)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return path.read_bytes().strip()
