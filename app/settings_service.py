from __future__ import annotations

from sqlalchemy.orm import Session

from . import config
from .database import SessionLocal
from .models import AppSetting
from .security import decrypt_secret, encrypt_secret


SETTING_DEFAULTS: dict[str, str] = {
    "smtp_enabled": str(bool(config.SMTP_HOST)).lower(),
    "smtp_host": config.SMTP_HOST,
    "smtp_port": str(config.SMTP_PORT),
    "smtp_security": config.SMTP_SECURITY,
    "smtp_username": config.SMTP_USERNAME,
    "smtp_password": config.SMTP_PASSWORD,
    "smtp_from_name": config.SMTP_FROM_NAME,
    "smtp_from_email": config.SMTP_FROM_EMAIL,
    "smtp_reply_to": "",
    "telegram_enabled": str(bool(config.TELEGRAM_BOT_TOKEN)).lower(),
    "telegram_bot_token": config.TELEGRAM_BOT_TOKEN,
    "telegram_bot_username": config.TELEGRAM_BOT_USERNAME,
    "telegram_mode": "webhook",
    "telegram_webhook_url": f"{config.PUBLIC_APP_URL}/api/telegram/webhook",
    "public_app_name": "Emboxa Web",
    "public_domain": config.PUBLIC_APP_URL,
    "support_email": config.LEGAL_CONTACT_EMAIL,
    "default_language": "en",
    "available_languages": "it,en,fr,de,es,pt",
    "registration_enabled": "true",
    "standard_storage_limit_bytes": str(config.STANDARD_STORAGE_LIMIT_BYTES),
    "standard_mailbox_limit": str(config.STANDARD_MAILBOX_LIMIT),
    "standard_retention_days": str(config.STANDARD_RETENTION_DAYS),
    "permanent_mailbox_limit": "1",
    "permanent_mailbox_lock_days": str(config.PERMANENT_MAILBOX_LOCK_DAYS),
    "backup_concurrency": "1",
    "backup_queue_enabled": "true",
    "default_backup_retention_versions": "3",
    "backup_anomaly_threshold": str(config.BACKUP_ANOMALY_THRESHOLD),
    "standard_imap_transfer_limit": "2",
    "imap_transfer_concurrency": "2",
    "email_logo_url": "",
    "email_footer_text": "MissionF",
    "seo_default_title": "Emboxa Web — email backup and IMAP Transfer",
    "seo_default_description": "Versioned IMAP email backup, searchable email archive and IMAP Transfer restore.",
    "export_ttl_hours": str(config.EXPORT_TTL_HOURS),
    "export_max_bytes": str(10 * 1024**3),
    "cleanup_enabled": "true",
    "analytics_enabled": str(bool(config.GOOGLE_ANALYTICS_ID)).lower(),
    "google_analytics_id": config.GOOGLE_ANALYTICS_ID,
    "last_cleanup_at": "",
}

SECRET_SETTING_KEYS = {"smtp_password", "telegram_bot_token", "telegram_webhook_secret"}


def get_setting(key: str, default: str | None = None, db: Session | None = None) -> str:
    fallback = SETTING_DEFAULTS.get(key, "") if default is None else default
    owns_session = db is None
    session = db or SessionLocal()
    try:
        item = session.get(AppSetting, key)
        if not item:
            return fallback
        return decrypt_secret(item.value) if item.encrypted and item.value else item.value
    finally:
        if owns_session:
            session.close()


def get_bool_setting(key: str, default: bool | None = None, db: Session | None = None) -> bool:
    fallback = str(default).lower() if default is not None else None
    return get_setting(key, fallback, db).strip().lower() in {"1", "true", "yes", "on"}


def get_int_setting(key: str, default: int | None = None, db: Session | None = None) -> int:
    fallback = str(default) if default is not None else None
    try:
        return int(get_setting(key, fallback, db))
    except (TypeError, ValueError):
        return int(fallback or 0)


def get_float_setting(key: str, default: float | None = None, db: Session | None = None) -> float:
    fallback = str(default) if default is not None else None
    try:
        return float(get_setting(key, fallback, db))
    except (TypeError, ValueError):
        return float(fallback or 0)


def save_setting(db: Session, key: str, value: str, encrypted: bool | None = None) -> None:
    use_encryption = key in SECRET_SETTING_KEYS if encrypted is None else encrypted
    item = db.get(AppSetting, key) or AppSetting(key=key)
    item.value = encrypt_secret(value) if use_encryption and value else value
    item.encrypted = use_encryption
    db.add(item)
