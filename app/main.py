from __future__ import annotations

import base64
import json
import hashlib
import logging
import mimetypes
import os
import re
import secrets
import shutil
import tempfile
import smtplib
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal
from urllib.parse import quote, urlparse
from urllib.request import Request as URLRequest, urlopen

import bleach
from bleach.css_sanitizer import CSSSanitizer
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask
from starlette.middleware.sessions import SessionMiddleware
from webauthn import (
    base64url_to_bytes,
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from .archive import ArchiveError, build_export, clear_account_archive, delete_account, import_archive
from .backup import backup_manager, next_backup_time, recover_interrupted_jobs, rotate_versions, snapshot_root
from .config import (
    ADMIN_EMAIL, ADMIN_PASSWORD, ARCHIVES_DIR, COOKIE_SECURE, DATA_DIR, EXPORTS_DIR, EXPORT_TTL_HOURS, IMPORTS_DIR, IMPORT_MAX_BYTES,
    LOCAL_EXPORTS_DIR, LOCAL_IMPORTS_DIR,
    GOOGLE_ANALYTICS_ID, LEGAL_CONTACT_EMAIL,
    LOG_LEVEL, PERMANENT_MAILBOX_LOCK_DAYS, PUBLIC_APP_URL, PUBLIC_SITE_URL, SMTP_FROM_EMAIL, SMTP_FROM_NAME,
    SMTP_HOST, SMTP_PASSWORD, SMTP_PORT, SMTP_SECURITY, SMTP_USERNAME, STANDARD_MAILBOX_LIMIT,
    STANDARD_RETENTION_DAYS, STANDARD_STORAGE_LIMIT_BYTES, TELEGRAM_BOT_TOKEN, MICROSOFT_CLIENT_ID,
    TELEGRAM_BOT_USERNAME, ensure_data_dirs,
)
from .database import SessionLocal, get_db
from .email_templates import password_reset_email, test_email, verification_email
from .graph_adapter import exchange_code, graph_json, microsoft_authorize_url, microsoft_profile, refresh_access_token
from .imap_adapter import test_imap_connection
from .imap_transfer import recover_interrupted_transfers, transfer_manager
from .migrations import run_migrations
from .mbox_import import MboxImportError, MboxSource, folder_name_from_upload, import_mbox_sources
from .models import (
    Account, AdminAudit, AppSetting, ArchiveDeletionAudit, Attachment, BackupJob, Folder, IMAPTransferJob, Message,
    PasskeyCredential, PermanentMailboxHistory, SecurityToken, Snapshot, TelegramLink, User, WebExport, utcnow,
)
from .scheduler import scheduler
from .settings_service import get_bool_setting, get_float_setting, get_int_setting, get_setting, save_setting
from .storage import account_active_archive_size, account_storage_used, snapshot_disk_size, total_archive_storage_used, user_storage_used
from .security import (
    clear_login_failures,
    csrf_matches,
    decrypt_secret,
    encrypt_secret,
    get_session_secret,
    hash_password,
    login_rate_limited,
    make_csrf_token,
    password_needs_rehash,
    record_failed_login,
    safe_resolve,
    verify_password,
)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("emboxa")
BASE_DIR = Path(__file__).resolve().parent
ASSET_VERSION = "20260826-2118"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_data_dirs()
    run_migrations()
    with SessionLocal() as db:
        if ADMIN_EMAIL and ADMIN_PASSWORD and not db.scalar(select(User).where(User.email == ADMIN_EMAIL.lower())):
            db.add(User(username=ADMIN_EMAIL.lower(), email=ADMIN_EMAIL.lower(), password_hash=hash_password(ADMIN_PASSWORD),
                        verified_at=utcnow(), role="admin", plan="PLUS", storage_limit_bytes=0))
            db.commit()
            log.info("Initial Web administrator created")
    recover_interrupted_jobs()
    recover_interrupted_transfers()
    scheduler.start()
    log.info("EMBOXA avviato")
    yield
    scheduler.stop()
    backup_manager.shutdown()
    transfer_manager.shutdown()
    log.info("EMBOXA arrestato")


app = FastAPI(title="Emboxa Web", version="1.0.0", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=get_session_secret(),
    session_cookie="emboxa_web_session",
    max_age=60 * 60 * 24 * 7,
    same_site="lax",
    https_only=COOKIE_SECURE,
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    is_mail_render = request.url.path.startswith("/api/messages/") and request.url.path.endswith("/render")
    is_embedded_file = request.url.path.startswith("/api/attachments/") and request.query_params.get("inline") == "1"
    is_public = request.url.path == "/" or request.url.path.startswith(("/it/", "/en/"))
    response.headers["X-Frame-Options"] = "SAMEORIGIN" if is_mail_render or is_embedded_file else "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if is_mail_render:
        remote = request.query_params.get("remote_images") == "1"
        image_sources = "'self' data: http: https:" if remote else "'self' data:"
        response.headers["Content-Security-Policy"] = (
            f"default-src 'none'; img-src {image_sources}; style-src 'unsafe-inline'; font-src data:; "
            "media-src 'self' data:; frame-ancestors 'self'; base-uri 'none'; form-action 'none'; "
            "sandbox allow-same-origin"
        )
    elif is_embedded_file:
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; img-src 'self' data:; media-src 'self'; style-src 'unsafe-inline'; "
            "frame-ancestors 'self'; sandbox"
        )
    elif is_public:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data: https://www.google-analytics.com; style-src 'self'; "
            "script-src 'self' https://www.googletagmanager.com; connect-src 'self' https://www.google-analytics.com; "
            "font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
    else:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "font-src 'self'; connect-src 'self'; frame-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, max-age=0, must-revalidate"
    if request.url.path.startswith(("/app", "/admin", "/api/", "/login", "/register", "/verify", "/reset-password")):
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user_id = request.session.get("user_id")
    user = db.get(User, user_id) if user_id else None
    if not user:
        raise HTTPException(401, "Autenticazione richiesta")
    if user.status != "active":
        raise HTTPException(403, "Account suspended")
    if not user.verified_at:
        raise HTTPException(403, "Email verification required")
    path = request.url.path
    owner_id = None
    match = re.match(r"^/api/accounts/(\d+)", path)
    if match:
        account = db.get(Account, int(match.group(1)))
        owner_id = account.owner_id if account else None
    match = match or re.match(r"^/api/snapshots/(\d+)", path)
    if match and path.startswith("/api/snapshots/"):
        snapshot = db.get(Snapshot, int(match.group(1)))
        owner_id = snapshot.account.owner_id if snapshot else None
    match = re.match(r"^/api/jobs/(\d+)", path)
    if match:
        job = db.get(BackupJob, int(match.group(1)))
        owner_id = job.account.owner_id if job else None
    match = re.match(r"^/api/imap-transfers/(\d+)", path)
    if match:
        transfer = db.get(IMAPTransferJob, int(match.group(1)))
        owner_id = transfer.owner_id if transfer else None
    match = re.match(r"^/api/messages/(\d+)", path)
    if match:
        message = db.get(Message, int(match.group(1)))
        owner_id = message.snapshot.account.owner_id if message else None
    match = re.match(r"^/api/attachments/(\d+)", path)
    if match:
        attachment = db.get(Attachment, int(match.group(1)))
        owner_id = attachment.message.snapshot.account.owner_id if attachment else None
    if owner_id is not None and owner_id != user.id:
        raise HTTPException(404, "Resource not found")
    return user


def admin_user(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(404, "Not found")
    return user


def csrf_guard(request: Request, _user: User = Depends(current_user)) -> None:
    if not csrf_matches(request.session.get("csrf"), request.headers.get("X-CSRF-Token")):
        raise HTTPException(403, "Token CSRF non valido")


def _csrf(request: Request) -> str:
    if "csrf" not in request.session:
        request.session["csrf"] = make_csrf_token()
    return request.session["csrf"]


def _client_key(request: Request, username: str) -> str:
    host = request.client.host if request.client else "unknown"
    return f"{host}:{username.lower()}"


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _webauthn_origin_and_rp_id(request: Request, db: Session | None = None) -> tuple[str, str]:
    request_origin = f"{request.url.scheme}://{request.url.netloc}"
    request_host = request.url.hostname or urlparse(request_origin).hostname or "localhost"
    if request_host in {"localhost", "127.0.0.1", "::1"}:
        return request_origin, request_host
    configured = get_setting("public_domain", PUBLIC_APP_URL, db).rstrip("/")
    parsed = urlparse(configured if "://" in configured else f"https://{configured}")
    if parsed.scheme and parsed.netloc and parsed.hostname:
        return f"{parsed.scheme}://{parsed.netloc}", parsed.hostname
    return request_origin, request_host


def _login_user_session(request: Request, user: User) -> None:
    request.session.clear()
    request.session["user_id"] = user.id
    request.session["csrf"] = make_csrf_token()


def _running_job(db: Session, account_id: int) -> BackupJob | None:
    return db.scalar(select(BackupJob).where(
        BackupJob.account_id == account_id,
        BackupJob.status.in_(["queued", "running", "cancelling"]),
    ).order_by(BackupJob.id.desc()))


def _account_or_404(db: Session, account_id: int) -> Account:
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(404, "Account non trovato")
    return account


def _active_snapshot(db: Session, account_id: int, snapshot_id: int | None = None) -> tuple[Account, Snapshot]:
    account = _account_or_404(db, account_id)
    selected_id = snapshot_id or account.active_snapshot_id
    snapshot = db.get(Snapshot, selected_id) if selected_id else None
    if not snapshot or snapshot.account_id != account.id or snapshot.status not in {"completed", "active"}:
        raise HTTPException(404, "Archivio non disponibile")
    return account, snapshot


class RegisterPayload(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=256)
    locale: Literal["it", "en", "fr", "de", "es", "pt"] = "en"


class LoginPayload(BaseModel):
    username: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=256)


class VerifyPayload(BaseModel):
    email: EmailStr
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class ResetRequestPayload(BaseModel):
    email: EmailStr


class ResetConfirmPayload(BaseModel):
    token: str = Field(min_length=32, max_length=200)
    password: str = Field(min_length=10, max_length=256)


class PasskeyRegisterVerifyPayload(BaseModel):
    credential: dict
    name: str = Field(default="Passkey", max_length=200)


class PasskeyAuthenticationOptionsPayload(BaseModel):
    email: EmailStr | None = None


class PasskeyAuthenticationVerifyPayload(BaseModel):
    credential: dict


class PreferencesPayload(BaseModel):
    locale: Literal["auto", "it", "en", "fr", "de", "es", "pt"] | None = None
    tutorial_completed: bool | None = None


class AdminUserPayload(BaseModel):
    plan: Literal["STANDARD", "PLUS"] | None = None
    status: Literal["active", "suspended"] | None = None
    storage_limit_bytes: int | None = Field(default=None, ge=1)
    confirm_downgrade: bool = False


class AdminDeleteUserPayload(BaseModel):
    confirmation: str = Field(min_length=3, max_length=320)


class AdminSettingsPayload(BaseModel):
    smtp_enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_security: Literal["plain", "starttls", "ssl"] = "starttls"
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_name: str = "EMBOXA"
    smtp_from_email: str = ""
    smtp_reply_to: str = ""
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_bot_username: str = ""
    telegram_mode: Literal["webhook"] = "webhook"
    telegram_webhook_url: str = ""
    public_app_name: str = Field(default="Emboxa Web", min_length=1, max_length=80)
    public_domain: str = Field(default=PUBLIC_APP_URL, min_length=1, max_length=500)
    support_email: str = Field(default="info@missionf.it", max_length=320)
    default_language: Literal["it", "en", "fr", "de", "es", "pt"] = "en"
    available_languages: str = Field(default="it,en,fr,de,es,pt", max_length=50)
    registration_enabled: bool = True
    standard_storage_limit_bytes: int = Field(ge=1)
    standard_mailbox_limit: int = Field(ge=1, le=1000)
    standard_retention_days: int = Field(ge=1, le=3650)
    permanent_mailbox_limit: int = Field(ge=1, le=1000)
    permanent_mailbox_lock_days: int = Field(ge=0, le=3650)
    backup_concurrency: int = Field(default=1, ge=1, le=16)
    backup_queue_enabled: bool = True
    default_backup_retention_versions: int = Field(default=3, ge=1, le=100)
    backup_anomaly_threshold: float = Field(default=.2, ge=.01, le=.9)
    standard_imap_transfer_limit: int = Field(default=2, ge=0, le=1000)
    imap_transfer_concurrency: int = Field(default=2, ge=1, le=8)
    email_logo_url: str = Field(default="", max_length=500)
    email_footer_text: str = Field(default="", max_length=500)
    seo_default_title: str = Field(default="Emboxa Web — email backup and IMAP Transfer", max_length=120)
    seo_default_description: str = Field(default="", max_length=320)
    export_ttl_hours: int = Field(default=24, ge=0, le=8760)
    export_max_bytes: int = Field(default=10 * 1024**3, ge=1)
    cleanup_enabled: bool = True
    analytics_enabled: bool = False
    google_analytics_id: str = Field(default="", max_length=40)


class SMTPTestPayload(BaseModel):
    email: EmailStr | None = None


class TelegramLinkPayload(BaseModel):
    chat_id: str = Field(min_length=1, max_length=64, pattern=r"^-?\d+$")


class TelegramConnectPayload(BaseModel):
    token: str = Field(min_length=20, max_length=256)


class TelegramPreferencesPayload(BaseModel):
    notify_completed: bool
    notify_failed: bool
    notify_expiring: bool
    notify_storage: bool


class AccountPayload(BaseModel):
    display_name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    imap_host: str = Field(min_length=1, max_length=255)
    imap_port: int = Field(ge=1, le=65535)
    security: Literal["ssl", "starttls", "plain"] = "ssl"
    imap_username: str = Field(min_length=1, max_length=320)
    password: str | None = Field(default=None, max_length=1024)
    root_folder: str | None = Field(default=None, max_length=500)
    schedule_mode: Literal["disabled", "daily", "weekly", "interval"] = "disabled"
    schedule_interval_hours: int | None = Field(default=None, ge=1, le=8760)
    retention_versions: int | None = Field(default=None, ge=1, le=100)


class AccountSettingsPayload(BaseModel):
    retention_versions: int = Field(ge=1, le=100)


class ConnectionPayload(BaseModel):
    imap_host: str = Field(min_length=1, max_length=255)
    imap_port: int = Field(ge=1, le=65535)
    security: Literal["ssl", "starttls", "plain"] = "ssl"
    imap_username: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class IMAPTransferDestinationPayload(BaseModel):
    account_id: int | None = None
    label: str = Field(default="Destination mailbox", min_length=1, max_length=200)
    imap_host: str | None = Field(default=None, max_length=255)
    imap_port: int | None = Field(default=None, ge=1, le=65535)
    security: Literal["ssl", "starttls", "plain"] = "ssl"
    imap_username: str | None = Field(default=None, max_length=320)
    password: str | None = Field(default=None, max_length=1024)


class IMAPTransferTestPayload(BaseModel):
    destination: IMAPTransferDestinationPayload


class IMAPTransferPayload(BaseModel):
    snapshot_id: int | None = None
    destination: IMAPTransferDestinationPayload
    mode: Literal["preserve", "single"] = "preserve"
    single_folder: str | None = Field(default=None, max_length=500)
    mappings: dict[str, str] = Field(default_factory=dict)
    skip_duplicates: bool = True


class MboxLinkPayload(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    display_name: str = Field(default="Archivio MBOX importato", min_length=1, max_length=200)
    email: str = Field(default="mbox-import@local.invalid", max_length=320)


class ImportLinkPayload(BaseModel):
    url: str = Field(min_length=8, max_length=2048)


class LocalImportPayload(BaseModel):
    mode: Literal["auto", "mailvault", "mbox"] = "auto"
    display_name: str = Field(default="Import NAS", min_length=1, max_length=200)
    email: str = Field(default="nas-import@local.invalid", max_length=320)


@app.get("/api/health")
def health():
    return {"status": "ok"}


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _setting(key: str, default: str = "") -> str:
    return get_setting(key, default)


def _save_setting(db: Session, key: str, value: str, encrypted: bool = False) -> None:
    save_setting(db, key, value, encrypted)


def _contact_email() -> str:
    return get_setting("support_email") or get_setting("smtp_from_email") or LEGAL_CONTACT_EMAIL


def _smtp_client():
    if not get_bool_setting("smtp_enabled"):
        raise HTTPException(409, "SMTP is not enabled")
    host = get_setting("smtp_host")
    if not host:
        raise HTTPException(409, "SMTP is not configured")
    port = get_int_setting("smtp_port", SMTP_PORT)
    security = get_setting("smtp_security", SMTP_SECURITY)
    username = get_setting("smtp_username", SMTP_USERNAME)
    password = get_setting("smtp_password", SMTP_PASSWORD)
    client = (smtplib.SMTP_SSL if security == "ssl" else smtplib.SMTP)(host, port, timeout=20)
    try:
        if security == "starttls":
            client.starttls()
        if username:
            client.login(username, password)
        return client
    except Exception:
        try:
            client.close()
        except Exception:
            pass
        raise


def _send_email(to: str, subject: str, text_body: str, html_body: str | None = None) -> None:
    from_email = get_setting("smtp_from_email", SMTP_FROM_EMAIL)
    from_name = get_setting("smtp_from_name", SMTP_FROM_NAME)
    if not from_email:
        raise HTTPException(503, "Email delivery is not configured")
    message = EmailMessage()
    message["From"] = f"{from_name} <{from_email}>"
    message["To"] = to
    message["Subject"] = subject
    reply_to = get_setting("smtp_reply_to")
    if reply_to:
        message["Reply-To"] = reply_to
    message.set_content(text_body)
    if html_body:
        message.add_alternative(html_body, subtype="html")
    with _smtp_client() as client:
        client.send_message(message)


def _issue_verification(db: Session, user: User) -> None:
    recent = db.scalar(select(func.count(SecurityToken.id)).where(
        SecurityToken.user_id == user.id, SecurityToken.purpose == "verify",
        SecurityToken.created_at >= utcnow() - timedelta(minutes=15))) or 0
    if recent >= 3:
        raise HTTPException(429, "Too many verification requests")
    code = f"{secrets.randbelow(1_000_000):06d}"
    token = SecurityToken(user_id=user.id, purpose="verify", token_hash=_token_hash(f"{user.id}:{code}"),
                          expires_at=utcnow() + timedelta(minutes=15))
    db.add(token)
    db.flush()
    text_body, html_body = verification_email(
        code,
        support_email=_contact_email(),
        public_url=get_setting("public_domain", PUBLIC_APP_URL),
        logo_url=get_setting("email_logo_url"),
        footer_text=get_setting("email_footer_text", "MissionF"),
        locale=user.locale if user.locale != "auto" else "en",
    )
    _send_email(user.email, "Your EMBOXA verification code", text_body, html_body)


@app.get("/", response_class=HTMLResponse)
def public_home(request: Request):
    return RedirectResponse("/app" if request.session.get("user_id") else "/login", status_code=303)


def _marketing_url(path: str = "") -> str:
    return f"{get_setting('public_site_url', PUBLIC_SITE_URL).rstrip('/')}{path}"


PUBLIC_PAGES = {"features", "self-hosted", "imap-email-backup", "email-archive", "restore-email-to-mailbox", "truenas-email-backup",
                "privacy", "cookies", "legal", "terms"}

SEO_PAGES = {
    "home": {
        "it": ("Emboxa Web — backup email e IMAP Transfer", "Crea backup versionati, cerca messaggi e allegati e usa IMAP Transfer per ripristinare gli originali in Gmail, Outlook, Yahoo, iCloud o caselle IMAP custom.", "backup email IMAP, IMAP Transfer, archivio email, ripristino casella email, trasferimento email IMAP"),
        "en": ("Emboxa Web — email backup and IMAP Transfer", "Create versioned backups, search messages and attachments, then use IMAP Transfer to restore originals to Gmail, Outlook, Yahoo, iCloud or custom IMAP mailboxes.", "IMAP email backup, IMAP Transfer, email archive, restore email to mailbox, mailbox migration"),
    },
    "imap-email-backup": {
        "it": ("Backup email IMAP completo e versionato — Emboxa", "Copia messaggi, cartelle e allegati da qualsiasi provider IMAP in un archivio ricercabile e ripristinabile.", "backup email IMAP, backup posta elettronica, copia casella IMAP"),
        "en": ("Complete versioned IMAP email backup — Emboxa", "Copy messages, folders and attachments from any IMAP provider into a searchable, restorable archive.", "IMAP email backup, mailbox backup, email backup service"),
    },
    "email-archive": {
        "it": ("Archivio email ricercabile con allegati originali — Emboxa", "Conserva versioni separate della casella, cerca messaggi e allegati e ripristina gli originali RFC822.", "archivio email, conservazione email, ricerca allegati email"),
        "en": ("Searchable email archive with original attachments — Emboxa", "Keep separate mailbox versions, search messages and attachments, and restore original RFC822 content.", "email archive, searchable email backup, email attachments archive"),
    },
    "restore-email-to-mailbox": {
        "it": ("Ripristina un archivio email in un'altra casella — Emboxa", "Copia i messaggi originali RFC822 da un backup Emboxa a Gmail, Outlook, Yahoo, iCloud o una casella IMAP personalizzata, preservando le cartelle.", "ripristino email IMAP, trasferire email tra caselle, restore mailbox, migrazione email"),
        "en": ("Restore an email archive to another mailbox — Emboxa", "Copy original RFC822 messages from an Emboxa backup to Gmail, Outlook, Yahoo, iCloud or a custom IMAP mailbox while preserving folders.", "restore email to mailbox, IMAP email transfer, mailbox migration, RFC822 restore"),
    },
    "truenas-email-backup": {
        "it": ("Backup email self-hosted su TrueNAS — Emboxa", "Installa Emboxa con Docker Compose o TrueNAS Community e conserva database, chiavi e archivi sulla tua NAS.", "TrueNAS email backup, self hosted email archive, Docker IMAP backup"),
        "en": ("Self-hosted email backup for TrueNAS — Emboxa", "Install Emboxa with Docker Compose or TrueNAS Community and keep its database, keys and archives on your NAS.", "TrueNAS email backup, self-hosted email archive, Docker IMAP backup"),
    },
}


@app.get("/it/", response_class=HTMLResponse)
@app.get("/en/", response_class=HTMLResponse)
@app.get("/it/{page}", response_class=HTMLResponse)
@app.get("/en/{page}", response_class=HTMLResponse)
def localized_public(request: Request, page: str = "home"):
    locale = request.url.path.split("/")[1]
    if page != "home" and page not in PUBLIC_PAGES:
        raise HTTPException(404, "Page not found")
    path = f"/{locale}/" + ("" if page == "home" else page)
    return RedirectResponse(_marketing_url(path), status_code=308)


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    return "User-agent: *\nDisallow: /\n"


@app.get("/sitemap.xml")
def sitemap():
    return RedirectResponse(_marketing_url("/sitemap.xml"), status_code=308)


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse(request, "register.html", {
        "registration_enabled": get_bool_setting("registration_enabled"),
        "marketing_url": _marketing_url(),
    })


@app.post("/api/register")
def register(payload: RegisterPayload, db: Session = Depends(get_db)):
    if not get_bool_setting("registration_enabled", db=db):
        raise HTTPException(403, "Registration is currently disabled")
    email = str(payload.email).lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "An account already exists for this email")
    user = User(username=email, email=email, password_hash=hash_password(payload.password), locale=payload.locale,
                storage_limit_bytes=get_int_setting("standard_storage_limit_bytes", STANDARD_STORAGE_LIMIT_BYTES, db))
    db.add(user)
    try:
        db.flush()
        _issue_verification(db, user)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"ok": True, "next": "/verify"}


@app.get("/verify", response_class=HTMLResponse)
def verify_page(request: Request):
    return templates.TemplateResponse(request, "verify.html", {"marketing_url": _marketing_url()})


@app.post("/api/verify")
def verify_email(payload: VerifyPayload, request: Request, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == str(payload.email).lower()))
    if not user:
        raise HTTPException(400, "Invalid or expired code")
    token = db.scalar(select(SecurityToken).where(
        SecurityToken.user_id == user.id, SecurityToken.purpose == "verify",
        SecurityToken.token_hash == _token_hash(f"{user.id}:{payload.code}"), SecurityToken.used_at.is_(None)
    ).order_by(SecurityToken.id.desc()))
    if not token or token.expires_at < utcnow() or token.attempts >= 8:
        raise HTTPException(400, "Invalid or expired code")
    token.used_at = utcnow(); user.verified_at = utcnow(); user.last_login_at = utcnow()
    db.commit()
    _login_user_session(request, user)
    return {"ok": True, "next": "/app"}


@app.post("/api/verification/resend")
def resend_verification(payload: ResetRequestPayload, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == str(payload.email).lower()))
    if user and not user.verified_at:
        _issue_verification(db, user); db.commit()
    return {"ok": True}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    if request.session.get("user_id"):
        return RedirectResponse("/app", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"marketing_url": _marketing_url()})


@app.post("/api/login")
def login(payload: LoginPayload, request: Request, db: Session = Depends(get_db)):
    key = _client_key(request, payload.username)
    if login_rate_limited(key):
        log.warning("Login limitato per indirizzo client")
        raise HTTPException(429, "Troppi tentativi. Riprova più tardi.")
    user = db.scalar(select(User).where(User.email == payload.username.strip().lower()))
    if not user or not verify_password(user.password_hash, payload.password):
        record_failed_login(key)
        log.warning("Login fallito")
        raise HTTPException(401, "Credenziali non valide")
    if not user.verified_at:
        raise HTTPException(403, "Verify your email before signing in")
    if user.status != "active":
        raise HTTPException(403, "Account suspended")
    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password)
    user.last_login_at = utcnow()
    db.commit()
    clear_login_failures(key)
    _login_user_session(request, user)
    return {"ok": True}


@app.get("/api/passkeys")
def list_passkeys(user: User = Depends(current_user), db: Session = Depends(get_db)):
    credentials = db.scalars(
        select(PasskeyCredential)
        .where(PasskeyCredential.user_id == user.id)
        .order_by(PasskeyCredential.created_at.desc())
    ).all()
    return [{
        "id": item.id,
        "name": item.name,
        "transports": json.loads(item.transports_json or "[]"),
        "device_type": item.device_type,
        "backed_up": item.backed_up,
        "created_at": item.created_at,
        "last_used_at": item.last_used_at,
    } for item in credentials]


@app.post("/api/passkeys/register/options", dependencies=[Depends(csrf_guard)])
def passkey_registration_options(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    origin, rp_id = _webauthn_origin_and_rp_id(request, db)
    existing = db.scalars(select(PasskeyCredential).where(PasskeyCredential.user_id == user.id)).all()
    options = generate_registration_options(
        rp_id=rp_id,
        rp_name=get_setting("public_app_name", "Emboxa Web", db),
        user_id=str(user.id).encode(),
        user_name=user.email,
        user_display_name=user.email,
        attestation=AttestationConveyancePreference.NONE,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(item.credential_id))
            for item in existing
        ],
    )
    request.session["passkey_registration_challenge"] = _b64url(options.challenge)
    request.session["passkey_registration_origin"] = origin
    request.session["passkey_registration_rp_id"] = rp_id
    return JSONResponse(json.loads(options_to_json(options)))


@app.post("/api/passkeys/register/verify", dependencies=[Depends(csrf_guard)])
def verify_passkey_registration(payload: PasskeyRegisterVerifyPayload, request: Request,
                                user: User = Depends(current_user), db: Session = Depends(get_db)):
    challenge = request.session.get("passkey_registration_challenge")
    origin = request.session.get("passkey_registration_origin")
    rp_id = request.session.get("passkey_registration_rp_id")
    if not challenge or not origin or not rp_id:
        raise HTTPException(400, "Passkey registration expired. Try again.")
    try:
        verification = verify_registration_response(
            credential=payload.credential,
            expected_challenge=base64url_to_bytes(challenge),
            expected_origin=origin,
            expected_rp_id=rp_id,
            require_user_verification=True,
        )
    except Exception as exc:
        log.warning("Passkey registration failed: %s", exc)
        raise HTTPException(400, "Passkey registration failed")
    credential_id = _b64url(verification.credential_id)
    if db.scalar(select(PasskeyCredential).where(PasskeyCredential.credential_id == credential_id)):
        raise HTTPException(409, "Passkey already registered")
    transports = payload.credential.get("response", {}).get("transports") or []
    db.add(PasskeyCredential(
        user_id=user.id,
        credential_id=credential_id,
        public_key=_b64url(verification.credential_public_key),
        sign_count=getattr(verification, "sign_count", 0) or 0,
        name=(payload.name or "Passkey").strip()[:200] or "Passkey",
        transports_json=json.dumps(transports),
        device_type=str(getattr(verification, "credential_device_type", "") or ""),
        backed_up=bool(getattr(verification, "credential_backed_up", False)),
    ))
    db.commit()
    request.session.pop("passkey_registration_challenge", None)
    request.session.pop("passkey_registration_origin", None)
    request.session.pop("passkey_registration_rp_id", None)
    return {"ok": True}


@app.delete("/api/passkeys/{passkey_id}", dependencies=[Depends(csrf_guard)])
def delete_passkey(passkey_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    credential = db.get(PasskeyCredential, passkey_id)
    if not credential or credential.user_id != user.id:
        raise HTTPException(404, "Passkey not found")
    db.delete(credential)
    db.commit()
    return {"ok": True}


@app.post("/api/passkeys/authentication/options")
def passkey_authentication_options(payload: PasskeyAuthenticationOptionsPayload, request: Request, db: Session = Depends(get_db)):
    origin, rp_id = _webauthn_origin_and_rp_id(request, db)
    user_id = None
    allow_credentials = None
    if payload.email:
        user = db.scalar(select(User).where(User.email == str(payload.email).lower()))
        if not user or user.status != "active" or not user.verified_at:
            raise HTTPException(404, "No passkey is available for this account")
        credentials = db.scalars(select(PasskeyCredential).where(PasskeyCredential.user_id == user.id)).all()
        if not credentials:
            raise HTTPException(404, "No passkey is available for this account")
        user_id = user.id
        allow_credentials = [PublicKeyCredentialDescriptor(id=base64url_to_bytes(item.credential_id)) for item in credentials]
    options = generate_authentication_options(
        rp_id=rp_id,
        allow_credentials=allow_credentials,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    request.session["passkey_authentication_challenge"] = _b64url(options.challenge)
    request.session["passkey_authentication_origin"] = origin
    request.session["passkey_authentication_rp_id"] = rp_id
    request.session["passkey_authentication_user_id"] = user_id
    return JSONResponse(json.loads(options_to_json(options)))


@app.post("/api/passkeys/authentication/verify")
def verify_passkey_authentication(payload: PasskeyAuthenticationVerifyPayload, request: Request, db: Session = Depends(get_db)):
    challenge = request.session.get("passkey_authentication_challenge")
    origin = request.session.get("passkey_authentication_origin")
    rp_id = request.session.get("passkey_authentication_rp_id")
    if not challenge or not origin or not rp_id:
        raise HTTPException(400, "Passkey login expired. Try again.")
    credential_id = payload.credential.get("rawId") or payload.credential.get("id")
    credential = db.scalar(select(PasskeyCredential).where(PasskeyCredential.credential_id == credential_id)) if credential_id else None
    expected_user_id = request.session.get("passkey_authentication_user_id")
    if not credential or (expected_user_id and credential.user_id != expected_user_id):
        raise HTTPException(401, "Passkey not recognized")
    user = db.get(User, credential.user_id)
    if not user or user.status != "active" or not user.verified_at:
        raise HTTPException(403, "Account is not available")
    try:
        verification = verify_authentication_response(
            credential=payload.credential,
            expected_challenge=base64url_to_bytes(challenge),
            expected_origin=origin,
            expected_rp_id=rp_id,
            credential_public_key=base64url_to_bytes(credential.public_key),
            credential_current_sign_count=credential.sign_count,
            require_user_verification=True,
        )
    except Exception as exc:
        log.warning("Passkey authentication failed: %s", exc)
        raise HTTPException(401, "Passkey authentication failed")
    credential.sign_count = getattr(verification, "new_sign_count", credential.sign_count) or credential.sign_count
    credential.last_used_at = utcnow()
    user.last_login_at = utcnow()
    db.commit()
    _login_user_session(request, user)
    return {"ok": True}


@app.get("/reset-password", response_class=HTMLResponse)
def reset_page(request: Request):
    return templates.TemplateResponse(request, "reset.html", {"marketing_url": _marketing_url()})


@app.post("/api/password-reset/request")
def request_password_reset(payload: ResetRequestPayload, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == str(payload.email).lower()))
    if user:
        raw = secrets.token_urlsafe(36)
        db.add(SecurityToken(user_id=user.id, purpose="reset", token_hash=_token_hash(raw),
                             expires_at=utcnow() + timedelta(hours=1)))
        db.flush()
        public_url = get_setting("public_domain", PUBLIC_APP_URL, db).rstrip("/")
        reset_url = f"{public_url}/reset-password?token={quote(raw)}"
        text_body, html_body = password_reset_email(
            reset_url,
            support_email=_contact_email(),
            public_url=public_url,
            logo_url=get_setting("email_logo_url"),
            footer_text=get_setting("email_footer_text", "MissionF"),
            locale=user.locale if user.locale != "auto" else "en",
        )
        _send_email(user.email, "Reset your EMBOXA password", text_body, html_body)
        db.commit()
    return {"ok": True}


@app.post("/api/password-reset/confirm")
def confirm_password_reset(payload: ResetConfirmPayload, db: Session = Depends(get_db)):
    token = db.scalar(select(SecurityToken).where(SecurityToken.purpose == "reset",
        SecurityToken.token_hash == _token_hash(payload.token), SecurityToken.used_at.is_(None)))
    if not token or token.expires_at < utcnow():
        raise HTTPException(400, "Invalid or expired reset link")
    user = db.get(User, token.user_id); user.password_hash = hash_password(payload.password); token.used_at = utcnow(); db.commit()
    return {"ok": True}


@app.post("/api/logout", dependencies=[Depends(csrf_guard)])
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/app", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)):
    user_id = request.session.get("user_id")
    user = db.get(User, user_id) if user_id else None
    if not user or not user.verified_at or user.status != "active":
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request,
        "app.html",
        {"csrf_token": _csrf(request), "web_user": user, "admin_email": _contact_email(), "asset_version": ASSET_VERSION},
    )


@app.get("/api/preferences")
def preferences(user: User = Depends(current_user)):
    return {"locale": user.locale, "tutorial_completed": user.tutorial_completed}


@app.patch("/api/preferences", dependencies=[Depends(csrf_guard)])
def update_preferences(payload: PreferencesPayload, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if payload.locale is not None:
        user.locale = payload.locale
    if payload.tutorial_completed is not None:
        user.tutorial_completed = payload.tutorial_completed
    db.commit()
    return {"locale": user.locale, "tutorial_completed": user.tutorial_completed}


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, admin: User = Depends(admin_user)):
    return templates.TemplateResponse(request, "admin.html", {"csrf_token": _csrf(request), "admin": admin})


@app.get("/api/admin/users")
def admin_users(q: str = "", _admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    query = select(User).order_by(User.created_at.desc())
    if q:
        query = query.where(User.email.ilike(f"%{q[:200]}%"))
    users = db.scalars(query.limit(500)).all()
    return [{"id": item.id, "email": item.email, "verified": bool(item.verified_at), "plan": item.plan,
             "role": item.role, "status": item.status, "storage_used": _storage_used(db, item.id),
             "storage_limit": None if item.plan == "PLUS" else item.storage_limit_bytes,
             "mailbox_count": db.scalar(select(func.count(Account.id)).where(Account.owner_id == item.id)) or 0,
             "created_at": item.created_at, "last_login_at": item.last_login_at} for item in users]


@app.patch("/api/admin/users/{user_id}", dependencies=[Depends(csrf_guard)])
def admin_update_user(user_id: int, payload: AdminUserPayload, admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(404, "User not found")
    if payload.plan == "STANDARD" and target.plan == "PLUS":
        mailbox_count = db.scalar(select(func.count(Account.id)).where(Account.owner_id == target.id)) or 0
        used = _storage_used(db, target.id)
        permanent_count = db.scalar(select(func.count(Account.id)).where(Account.owner_id == target.id, Account.is_permanent.is_(True))) or 0
        mailbox_limit = get_int_setting("standard_mailbox_limit", STANDARD_MAILBOX_LIMIT, db)
        permanent_limit = get_int_setting("permanent_mailbox_limit", 1, db)
        violations = {"mailboxes": max(0, mailbox_count - mailbox_limit),
                      "storage_over_bytes": max(0, used - target.storage_limit_bytes),
                      "permanent_mailboxes": max(0, permanent_count - permanent_limit)}
        if not payload.confirm_downgrade:
            raise HTTPException(409, {"message": "Downgrade confirmation required", "violations": violations})
        for account in db.scalars(select(Account).where(Account.owner_id == target.id, Account.is_permanent.is_(False))).all():
            for snapshot in account.snapshots:
                if snapshot.status in {"completed", "active"}:
                    snapshot.expires_at = snapshot.created_at + timedelta(days=get_int_setting("standard_retention_days", STANDARD_RETENTION_DAYS, db))
    if payload.plan is not None:
        target.plan = payload.plan
    if payload.status is not None:
        target.status = payload.status
    if payload.storage_limit_bytes is not None:
        target.storage_limit_bytes = payload.storage_limit_bytes
    db.add(AdminAudit(admin_id=admin.id, action="user_update", target_type="user", target_id=str(target.id),
                      detail=json.dumps(payload.model_dump(), default=str)))
    db.commit()
    return {"ok": True, "over_quota": target.plan != "PLUS" and _storage_used(db, target.id) > target.storage_limit_bytes}


@app.delete("/api/admin/users/{user_id}", dependencies=[Depends(csrf_guard)])
def admin_delete_user(user_id: int, payload: AdminDeleteUserPayload, admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(404, "User not found")
    if target.id == admin.id:
        raise HTTPException(409, "You cannot delete the account used for this administrator session")
    confirmation = payload.confirmation.strip()
    if confirmation != "DELETE" and confirmation.lower() != target.email.lower():
        raise HTTPException(422, "Type DELETE or the user's full email address to confirm")

    accounts = db.scalars(select(Account).where(Account.owner_id == target.id)).all()
    target.status = "deleting"
    for account in accounts:
        account.imap_enabled = False
        account.next_backup_at = None

    active = False
    account_ids = [item.id for item in accounts]
    if account_ids:
        for job in db.scalars(select(BackupJob).where(BackupJob.account_id.in_(account_ids), BackupJob.status.in_(["queued", "running", "cancelling"]))).all():
            job.cancel_requested = True
            if job.status == "queued":
                job.status, job.finished_at = "cancelled", utcnow()
            else:
                job.status, active = "cancelling", True
    for job in db.scalars(select(IMAPTransferJob).where(IMAPTransferJob.owner_id == target.id, IMAPTransferJob.status.in_(["queued", "running", "cancelling"]))).all():
        job.cancel_requested = True
        if job.status == "queued":
            job.status, job.finished_at, job.encrypted_password, job.quota_period = "cancelled", utcnow(), None, None
        else:
            job.status, active = "cancelling", True
    db.commit()
    if active:
        raise HTTPException(409, "Running backup or restore jobs are being cancelled. Retry deletion when cancellation finishes.")

    archive_paths = [ARCHIVES_DIR / item.archive_uuid for item in accounts]
    export_paths = [safe_resolve(EXPORTS_DIR, item.relpath) for item in db.scalars(select(WebExport).where(WebExport.owner_id == target.id)).all()]
    db.execute(text("DELETE FROM message_fts WHERE snapshot_id IN (SELECT snapshots.id FROM snapshots JOIN accounts ON accounts.id=snapshots.account_id WHERE accounts.owner_id=:uid)"), {"uid": target.id})
    for account in accounts:
        account.active_snapshot_id = None
    db.flush()
    db.add(AdminAudit(admin_id=admin.id, action="user_delete", target_type="user", target_id=str(target.id),
                      detail="User account and all owned archive data permanently deleted"))
    db.delete(target)
    db.commit()
    for path in export_paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            log.warning("Could not remove deleted-user export %s", path)
    for path in archive_paths:
        shutil.rmtree(path, ignore_errors=True)
    shutil.rmtree(EXPORTS_DIR / f"user-{user_id}", ignore_errors=True)
    return {"ok": True}


@app.get("/api/admin/settings")
def admin_settings(_admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    telegram_token_set = bool(get_setting("telegram_bot_token", db=db))
    return {
        "smtp_enabled": get_bool_setting("smtp_enabled", db=db),
        "smtp_host": get_setting("smtp_host", db=db),
        "smtp_port": get_int_setting("smtp_port", SMTP_PORT, db),
        "smtp_security": get_setting("smtp_security", SMTP_SECURITY, db),
        "smtp_username": get_setting("smtp_username", db=db),
        "smtp_password_set": bool(get_setting("smtp_password", db=db)),
        "smtp_password_masked": "••••••••••••" if get_setting("smtp_password", db=db) else "",
        "smtp_from_name": get_setting("smtp_from_name", SMTP_FROM_NAME, db),
        "smtp_from_email": get_setting("smtp_from_email", SMTP_FROM_EMAIL, db),
        "smtp_reply_to": get_setting("smtp_reply_to", db=db),
        "telegram_enabled": get_bool_setting("telegram_enabled", db=db),
        "telegram_connected": telegram_token_set and get_bool_setting("telegram_enabled", db=db),
        "telegram_bot_token_set": telegram_token_set,
        "telegram_bot_token_masked": "••••••••••••" if telegram_token_set else "",
        "telegram_bot_username": get_setting("telegram_bot_username", db=db),
        "telegram_mode": get_setting("telegram_mode", "webhook", db),
        "telegram_webhook_url": get_setting("telegram_webhook_url", db=db),
        "telegram_webhook_status": get_setting("telegram_webhook_status", "not_configured", db),
        "telegram_webhook_error": get_setting("telegram_webhook_error", db=db),
        "telegram_links": db.scalar(select(func.count(TelegramLink.id))) or 0,
        "public_app_name": get_setting("public_app_name", db=db),
        "public_domain": get_setting("public_domain", PUBLIC_APP_URL, db),
        "support_email": get_setting("support_email", LEGAL_CONTACT_EMAIL, db),
        "default_language": get_setting("default_language", "en", db),
        "available_languages": get_setting("available_languages", "it,en,fr,de,es,pt", db),
        "registration_enabled": get_bool_setting("registration_enabled", db=db),
        "standard_storage_limit_bytes": get_int_setting("standard_storage_limit_bytes", STANDARD_STORAGE_LIMIT_BYTES, db),
        "standard_mailbox_limit": get_int_setting("standard_mailbox_limit", STANDARD_MAILBOX_LIMIT, db),
        "standard_retention_days": get_int_setting("standard_retention_days", STANDARD_RETENTION_DAYS, db),
        "permanent_mailbox_limit": get_int_setting("permanent_mailbox_limit", 1, db),
        "permanent_mailbox_lock_days": get_int_setting("permanent_mailbox_lock_days", PERMANENT_MAILBOX_LOCK_DAYS, db),
        "backup_concurrency": get_int_setting("backup_concurrency", 1, db),
        "backup_queue_enabled": get_bool_setting("backup_queue_enabled", db=db),
        "default_backup_retention_versions": get_int_setting("default_backup_retention_versions", 3, db),
        "backup_anomaly_threshold": get_float_setting("backup_anomaly_threshold", .2, db),
        "standard_imap_transfer_limit": get_int_setting("standard_imap_transfer_limit", 2, db),
        "imap_transfer_concurrency": get_int_setting("imap_transfer_concurrency", 2, db),
        "email_logo_url": get_setting("email_logo_url", db=db),
        "email_footer_text": get_setting("email_footer_text", db=db),
        "seo_default_title": get_setting("seo_default_title", "Emboxa Web — email backup and IMAP Transfer", db),
        "seo_default_description": get_setting("seo_default_description", db=db),
        "export_ttl_hours": get_int_setting("export_ttl_hours", EXPORT_TTL_HOURS, db),
        "export_max_bytes": get_int_setting("export_max_bytes", 10 * 1024**3, db),
        "cleanup_enabled": get_bool_setting("cleanup_enabled", db=db),
        "analytics_enabled": get_bool_setting("analytics_enabled", db=db),
        "google_analytics_id": get_setting("google_analytics_id", GOOGLE_ANALYTICS_ID, db),
    }


@app.put("/api/admin/settings", dependencies=[Depends(csrf_guard)])
def admin_save_settings(payload: AdminSettingsPayload, admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    if payload.google_analytics_id and not re.fullmatch(r"G-[A-Z0-9]+", payload.google_analytics_id, re.I):
        raise HTTPException(422, "Measurement ID must use the G-XXXXXXXX format")
    if not payload.public_domain.startswith(("https://", "http://")):
        raise HTTPException(422, "Public domain must be an absolute HTTP(S) URL")
    old_webhook_url = get_setting("telegram_webhook_url", db=db)
    telegram_username = get_setting("telegram_bot_username", db=db)
    telegram_token = payload.telegram_bot_token.strip()
    if telegram_token:
        try:
            identity = _telegram_request(telegram_token, "getMe", {})
        except HTTPException as exc:
            raise HTTPException(422, "Invalid Telegram bot token") from exc
        telegram_username = str(identity.get("username") or "")
    telegram_configured = bool(telegram_token or get_setting("telegram_bot_token", db=db))
    webhook_url = f"{payload.public_domain.rstrip('/')}/api/telegram/webhook"
    values = {
        "smtp_enabled": str(payload.smtp_enabled).lower(), "smtp_host": payload.smtp_host.strip(),
        "smtp_port": str(payload.smtp_port), "smtp_security": payload.smtp_security,
        "smtp_username": payload.smtp_username.strip(), "smtp_from_name": payload.smtp_from_name.strip(),
        "smtp_from_email": payload.smtp_from_email.strip(), "smtp_reply_to": payload.smtp_reply_to.strip(),
        "telegram_enabled": str(telegram_configured).lower(),
        "telegram_bot_username": telegram_username,
        "telegram_mode": "webhook", "telegram_webhook_url": webhook_url,
        "public_app_name": payload.public_app_name.strip(), "public_domain": payload.public_domain.rstrip("/"),
        "support_email": payload.support_email.strip(), "default_language": payload.default_language,
        "available_languages": payload.available_languages, "registration_enabled": str(payload.registration_enabled).lower(),
        "standard_storage_limit_bytes": str(payload.standard_storage_limit_bytes),
        "standard_mailbox_limit": str(payload.standard_mailbox_limit),
        "standard_retention_days": str(payload.standard_retention_days),
        "permanent_mailbox_limit": str(payload.permanent_mailbox_limit),
        "permanent_mailbox_lock_days": str(payload.permanent_mailbox_lock_days),
        "backup_concurrency": str(payload.backup_concurrency),
        "backup_queue_enabled": str(payload.backup_queue_enabled).lower(),
        "default_backup_retention_versions": str(payload.default_backup_retention_versions),
        "backup_anomaly_threshold": str(payload.backup_anomaly_threshold),
        "standard_imap_transfer_limit": str(payload.standard_imap_transfer_limit),
        "imap_transfer_concurrency": str(payload.imap_transfer_concurrency),
        "email_logo_url": payload.email_logo_url.strip(),
        "email_footer_text": payload.email_footer_text.strip(),
        "seo_default_title": payload.seo_default_title.strip(),
        "seo_default_description": payload.seo_default_description.strip(),
        "export_ttl_hours": str(payload.export_ttl_hours), "export_max_bytes": str(payload.export_max_bytes),
        "cleanup_enabled": str(payload.cleanup_enabled).lower(),
        "analytics_enabled": str(payload.analytics_enabled).lower(),
        "google_analytics_id": payload.google_analytics_id.strip().upper(),
    }
    for key, value in values.items():
        _save_setting(db, key, value)
    if payload.smtp_password:
        _save_setting(db, "smtp_password", payload.smtp_password, True)
    if telegram_token:
        _save_setting(db, "telegram_bot_token", telegram_token, True)
    if telegram_configured and not db.get(AppSetting, "telegram_webhook_secret"):
        _save_setting(db, "telegram_webhook_secret", secrets.token_urlsafe(32), True)
    changed_sections = "SMTP, Telegram, application, limits, backup, export and analytics settings updated"
    db.add(AdminAudit(admin_id=admin.id, action="settings_update", target_type="app", target_id="global", detail=changed_sections))
    db.commit()
    telegram_warning = ""
    if telegram_configured and (telegram_token or old_webhook_url != webhook_url or get_setting("telegram_webhook_status", db=db) != "connected"):
        effective_token = telegram_token or get_setting("telegram_bot_token", db=db)
        try:
            _telegram_request(effective_token, "setWebhook", {
                "url": webhook_url,
                "secret_token": get_setting("telegram_webhook_secret", db=db),
            })
            _save_setting(db, "telegram_webhook_status", "connected")
            _save_setting(db, "telegram_webhook_error", "")
        except HTTPException:
            telegram_warning = "Bot connected, but the webhook is not active. Check that the public HTTPS URL reaches Emboxa, then retry it."
            _save_setting(db, "telegram_webhook_status", "failed")
            _save_setting(db, "telegram_webhook_error", telegram_warning)
            db.add(AdminAudit(admin_id=admin.id, action="telegram_webhook_failed", target_type="app",
                              target_id="global", detail="Telegram webhook configuration failed"))
        db.commit()
    backup_manager.refresh()
    transfer_manager.refresh()
    return {"ok": True, "telegram": {"connected": telegram_configured, "username": telegram_username,
            "webhook_status": get_setting("telegram_webhook_status", "not_configured", db), "warning": telegram_warning}}


@app.post("/api/admin/smtp/test", dependencies=[Depends(csrf_guard)])
def admin_test_smtp(payload: SMTPTestPayload, admin: User = Depends(admin_user)):
    try:
        with _smtp_client():
            pass
        if payload.email:
            public_url = get_setting("public_domain", PUBLIC_APP_URL).rstrip("/")
            text_body, html_body = test_email(
                public_url=public_url,
                logo_url=get_setting("email_logo_url") or None,
                footer_text=get_setting("email_footer_text") or None,
                support_email=_contact_email(),
            )
            _send_email(str(payload.email), "EMBOXA SMTP test", text_body, html_body)
    except smtplib.SMTPAuthenticationError as exc:
        raise HTTPException(502, "Unable to authenticate with SMTP server") from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, "Unable to connect to SMTP server") from exc
    return {"ok": True, "message": "Test email sent" if payload.email else "Connection successful"}


@app.post("/api/admin/telegram/test", dependencies=[Depends(csrf_guard)])
def admin_test_telegram(_admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    token = get_setting("telegram_bot_token", db=db)
    if not token:
        raise HTTPException(409, "Paste and save a BotFather token first")
    identity = _telegram_request(token, "getMe", {})
    username = str(identity.get("username") or "")
    _save_setting(db, "telegram_enabled", "true")
    _save_setting(db, "telegram_bot_username", username)
    webhook_url = get_setting("telegram_webhook_url", db=db)
    webhook_status = "unknown"
    webhook_error = ""
    try:
        info = _telegram_request(token, "getWebhookInfo", {})
        actual_url = str(info.get("url") or "")
        webhook_error = str(info.get("last_error_message") or "")[:500]
        webhook_status = "connected" if webhook_url and actual_url == webhook_url else "not_configured"
        if webhook_error:
            webhook_status = "warning"
    except HTTPException:
        webhook_error = "The bot is connected, but Telegram webhook status is temporarily unavailable."
    _save_setting(db, "telegram_webhook_status", webhook_status)
    _save_setting(db, "telegram_webhook_error", webhook_error)
    db.commit()
    return {"ok": True, "username": username, "connected": True, "message": "Bot connected",
            "webhook_status": webhook_status, "webhook_error": webhook_error}


@app.post("/api/admin/telegram/connect", dependencies=[Depends(csrf_guard)])
def admin_connect_telegram(payload: TelegramConnectPayload, admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    token = payload.token.strip()
    try:
        identity = _telegram_request(token, "getMe", {})
    except HTTPException as exc:
        raise HTTPException(422, "Invalid Telegram bot token") from exc
    username = str(identity.get("username") or "")
    webhook_url = f"{get_setting('public_domain', PUBLIC_APP_URL, db).rstrip('/')}/api/telegram/webhook"
    secret = get_setting("telegram_webhook_secret", db=db) or secrets.token_urlsafe(32)
    _save_setting(db, "telegram_bot_token", token, True)
    _save_setting(db, "telegram_enabled", "true")
    _save_setting(db, "telegram_bot_username", username)
    _save_setting(db, "telegram_mode", "webhook")
    _save_setting(db, "telegram_webhook_url", webhook_url)
    _save_setting(db, "telegram_webhook_secret", secret, True)
    db.commit()
    warning = ""
    try:
        _telegram_request(token, "setWebhook", {"url": webhook_url, "secret_token": secret})
        _save_setting(db, "telegram_webhook_status", "connected")
        _save_setting(db, "telegram_webhook_error", "")
    except HTTPException:
        warning = "Bot connected, but the webhook is not active. Check the public HTTPS route, then retry it."
        _save_setting(db, "telegram_webhook_status", "failed")
        _save_setting(db, "telegram_webhook_error", warning)
    db.add(AdminAudit(admin_id=admin.id, action="telegram_connected", target_type="app",
                      target_id="global", detail=f"Telegram bot @{username} connected; webhook={'failed' if warning else 'connected'}"))
    db.commit()
    return {"ok": True, "connected": True, "username": username,
            "webhook_status": "failed" if warning else "connected", "warning": warning}


@app.post("/api/admin/telegram/webhook", dependencies=[Depends(csrf_guard)])
def admin_retry_telegram_webhook(admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    token = get_setting("telegram_bot_token", db=db)
    webhook_url = get_setting("telegram_webhook_url", db=db)
    secret = get_setting("telegram_webhook_secret", db=db)
    if not token:
        raise HTTPException(409, "Paste and save a BotFather token first")
    if not webhook_url or not secret:
        raise HTTPException(409, "Save the public application URL first")
    try:
        _telegram_request(token, "setWebhook", {"url": webhook_url, "secret_token": secret})
    except HTTPException as exc:
        message = "Webhook setup failed. Verify the public HTTPS route and that Cloudflare does not challenge Telegram."
        _save_setting(db, "telegram_webhook_status", "failed")
        _save_setting(db, "telegram_webhook_error", message)
        db.commit()
        raise HTTPException(502, message) from exc
    _save_setting(db, "telegram_enabled", "true")
    _save_setting(db, "telegram_webhook_status", "connected")
    _save_setting(db, "telegram_webhook_error", "")
    db.add(AdminAudit(admin_id=admin.id, action="telegram_webhook_connected", target_type="app",
                      target_id="global", detail="Telegram webhook configured"))
    db.commit()
    return {"ok": True, "message": "Webhook active", "webhook_status": "connected"}


@app.delete("/api/admin/telegram", dependencies=[Depends(csrf_guard)])
def admin_disconnect_telegram(admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    token = get_setting("telegram_bot_token", db=db)
    if token:
        try:
            _telegram_request(token, "deleteWebhook", {"drop_pending_updates": False})
        except HTTPException:
            pass
    _save_setting(db, "telegram_enabled", "false")
    _save_setting(db, "telegram_bot_token", "", True)
    _save_setting(db, "telegram_bot_username", "")
    _save_setting(db, "telegram_webhook_url", "")
    _save_setting(db, "telegram_webhook_status", "not_configured")
    _save_setting(db, "telegram_webhook_error", "")
    db.add(AdminAudit(admin_id=admin.id, action="telegram_disconnected", target_type="app",
                      target_id="global", detail="Telegram bot disconnected"))
    db.commit()
    return {"ok": True, "message": "Telegram bot disconnected"}


@app.get("/api/admin/operations")
def admin_operations(_admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    jobs = db.scalars(select(BackupJob).order_by(BackupJob.id.desc()).limit(100)).all()
    queue = {status: db.scalar(select(func.count(BackupJob.id)).where(BackupJob.status == status)) or 0
             for status in ("queued", "running", "failed")}
    try:
        db.execute(text("SELECT 1"))
        database_status = "Connected"
    except Exception:
        database_status = "Unavailable"
    disk = shutil.disk_usage(DATA_DIR)
    return {"app_version": app.version, "database_status": database_status,
            "worker_status": "Running" if scheduler.is_running else "Stopped",
            "backup_queue_status": queue, "last_cleanup": get_setting("last_cleanup_at", db=db) or None,
            "storage_used": disk.used, "storage_capacity": disk.total,
            "storage_total": total_archive_storage_used(db),
            "users": db.scalar(select(func.count(User.id))) or 0,
            "jobs": [{"id": job.id, "status": job.status, "account_id": job.account_id, "error": job.error,
                      "created_at": job.created_at, "finished_at": job.finished_at} for job in jobs]}


@app.post("/api/admin/maintenance/cleanup", dependencies=[Depends(csrf_guard)])
def admin_cleanup(admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    scheduler.cleanup()
    db.add(AdminAudit(admin_id=admin.id, action="cleanup_run", target_type="app", target_id="global",
                      detail="Cleanup run from Administration"))
    db.commit()
    return {"ok": True, "message": "Cleanup completed"}


def _telegram_token() -> str:
    return _setting("telegram_bot_token", TELEGRAM_BOT_TOKEN)


def _telegram_request(token: str, method: str, payload: dict):
    request = URLRequest(f"https://api.telegram.org/bot{token}/{method}",
                         data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=15) as response:
            result = json.loads(response.read())
    except Exception as exc:
        raise HTTPException(502, "Telegram connection failed") from exc
    if not result.get("ok"):
        raise HTTPException(502, "Telegram rejected the request")
    return result.get("result") if result.get("result") is not None else {}


def _telegram_call(method: str, payload: dict):
    token = _telegram_token()
    if not token:
        raise HTTPException(409, "Telegram is not configured")
    return _telegram_request(token, method, payload)


def _telegram_dashboard(db: Session, user: User) -> tuple[str, dict]:
    used = _storage_used(db, user.id); mailboxes = db.scalar(select(func.count(Account.id)).where(Account.owner_id == user.id)) or 0
    job = db.scalar(select(BackupJob).join(Account).where(Account.owner_id == user.id,
        BackupJob.status.in_(["queued", "running", "cancelling"])).order_by(BackupJob.id))
    storage = "Unlimited" if user.plan == "PLUS" else f"{used / 1024**3:.1f} / {user.storage_limit_bytes / 1024**3:.0f} GB"
    mailbox_text = "Unlimited" if user.plan == "PLUS" else f"{mailboxes} / {get_int_setting('standard_mailbox_limit', STANDARD_MAILBOX_LIMIT, db)}"
    queue_position = (db.scalar(select(func.count(BackupJob.id)).where(BackupJob.status == "queued", BackupJob.id <= job.id)) or 0) if job and job.status == "queued" else 0
    backup = "No active backup" if not job else (f"{job.status.title()} · {job.percent}%" if job.status != "queued" else f"Queued · Position {queue_position}")
    transfers = db.scalar(select(func.count(IMAPTransferJob.id)).where(
        IMAPTransferJob.owner_id == user.id, IMAPTransferJob.status.in_(["queued", "running", "cancelling"])
    )) or 0
    text_value = f"EMBOXA · PRIVATE EMAIL ARCHIVE\n\nPlan  {user.plan}\nStorage  {storage}\nMailboxes  {mailbox_text}\n\nBackup\n{backup}\n\nMailbox restores active  {transfers}"
    keyboard = {"inline_keyboard": [[{"text": "📬 Mailboxes", "callback_data": "mailboxes"}, {"text": "▶️ Backup", "callback_data": "backup"}],
                                    [{"text": "📊 Activity", "callback_data": "status"}, {"text": "💾 Storage", "callback_data": "storage"}],
                                    [{"text": "🕘 History", "callback_data": "history"}, {"text": "⚙️ Settings", "callback_data": "settings"}],
                                    [{"text": "↻ Refresh", "callback_data": "dashboard"}]]}
    return text_value, keyboard


def _telegram_render(db: Session, user: User, chat_id: str, message_id: str | None, view: str) -> dict:
    text_value, keyboard = _telegram_dashboard(db, user)
    accounts = db.scalars(select(Account).where(Account.owner_id == user.id).order_by(Account.display_name)).all()
    if view in {"mailboxes", "backup"}:
        text_value = "Mailboxes\n\n" + ("\n".join(f"• {item.display_name}\n  {item.email} · {item.message_count:,} messages" for item in accounts) or "No mailboxes yet")
        rows = [[{"text": f"Backup {item.display_name}", "callback_data": f"backup:{item.id}"}] for item in accounts] if view == "backup" else []
        keyboard = {"inline_keyboard": rows + [[{"text": "Back", "callback_data": "dashboard"}]]}
    elif view == "status":
        jobs = db.scalars(select(BackupJob).join(Account).where(Account.owner_id == user.id).order_by(BackupJob.id.desc()).limit(5)).all()
        text_value = "Backup status\n\n" + ("\n".join(f"{job.account.display_name}: {job.status} · {job.percent}% · ETA {job.eta_seconds or '—'}" for job in jobs) or "No backup history")
        keyboard = {"inline_keyboard": [[{"text": "Back", "callback_data": "dashboard"}]]}
    elif view == "storage":
        used = _storage_used(db, user.id)
        limit = "Unlimited" if user.plan == "PLUS" else f"{user.storage_limit_bytes / 1024**3:.0f} GB"
        text_value = f"Storage\n\nUsed  {used / 1024**3:.2f} GB\nLimit  {limit}\n\nOriginal RFC822 messages and attachments are included."
        keyboard = {"inline_keyboard": [[{"text": "Back", "callback_data": "dashboard"}]]}
    elif view == "history":
        backups = db.scalars(select(BackupJob).join(Account).where(Account.owner_id == user.id).order_by(BackupJob.id.desc()).limit(4)).all()
        transfers = db.scalars(select(IMAPTransferJob).where(IMAPTransferJob.owner_id == user.id).order_by(IMAPTransferJob.id.desc()).limit(4)).all()
        lines = [f"Backup · {item.account.display_name} · {item.status}" for item in backups]
        lines += [f"Restore · {item.destination_label} · {item.status}" for item in transfers]
        text_value = "Recent history\n\n" + ("\n".join(lines) or "No completed operations yet")
        keyboard = {"inline_keyboard": [[{"text": "Back", "callback_data": "dashboard"}]]}
    elif view == "settings":
        link = db.scalar(select(TelegramLink).where(TelegramLink.user_id == user.id))
        enabled = [] if not link else [label for value, label in ((link.notify_completed,"Completed"),(link.notify_failed,"Failed"),(link.notify_expiring,"Expiring"),(link.notify_storage,"Storage")) if value]
        text_value = "Notification settings\n\nEnabled: " + (", ".join(enabled) or "None") + "\n\nChange these preferences in Emboxa Web → Preferences."
        keyboard = {"inline_keyboard": [[{"text": "Open Emboxa Web", "url": get_setting("public_domain", PUBLIC_APP_URL).rstrip("/") + "/app"}], [{"text": "Back", "callback_data": "dashboard"}]]}
    payload = {"chat_id": chat_id, "text": text_value, "reply_markup": keyboard}
    if message_id:
        payload["message_id"] = int(message_id)
        return _telegram_call("editMessageText", payload)
    return _telegram_call("sendMessage", payload)


@app.get("/api/telegram/link")
def telegram_link(user: User = Depends(current_user), db: Session = Depends(get_db)):
    item = db.scalar(select(TelegramLink).where(TelegramLink.user_id == user.id))
    return {"linked": bool(item), "chat_id": item.chat_id if item else None,
            "bot_configured": bool(_telegram_token()),
            "bot_username": _setting("telegram_bot_username", TELEGRAM_BOT_USERNAME),
            "preferences": {"notify_completed": item.notify_completed, "notify_failed": item.notify_failed,
                            "notify_expiring": item.notify_expiring, "notify_storage": item.notify_storage} if item else None}


@app.patch("/api/telegram/preferences", dependencies=[Depends(csrf_guard)])
def telegram_preferences(payload: TelegramPreferencesPayload, user: User = Depends(current_user), db: Session = Depends(get_db)):
    item = db.scalar(select(TelegramLink).where(TelegramLink.user_id == user.id))
    if not item:
        raise HTTPException(409, "Connect Telegram first")
    for key, value in payload.model_dump().items():
        setattr(item, key, value)
    db.commit(); return {"ok": True}


@app.put("/api/telegram/link", dependencies=[Depends(csrf_guard)])
def save_telegram_link(payload: TelegramLinkPayload, user: User = Depends(current_user), db: Session = Depends(get_db)):
    occupied = db.scalar(select(TelegramLink).where(TelegramLink.chat_id == payload.chat_id, TelegramLink.user_id != user.id))
    if occupied:
        raise HTTPException(409, "This Chat ID is already linked")
    item = db.scalar(select(TelegramLink).where(TelegramLink.user_id == user.id)) or TelegramLink(user_id=user.id, chat_id=payload.chat_id)
    item.chat_id = payload.chat_id; db.add(item); db.commit()
    return {"linked": True, "chat_id": item.chat_id}


@app.post("/api/telegram/test", dependencies=[Depends(csrf_guard)])
def test_telegram(user: User = Depends(current_user), db: Session = Depends(get_db)):
    item = db.scalar(select(TelegramLink).where(TelegramLink.user_id == user.id))
    if not item:
        raise HTTPException(409, "Connect a Chat ID first")
    result = _telegram_render(db, user, item.chat_id, item.dashboard_message_id, "dashboard")
    item.dashboard_message_id = str(result.get("message_id") or item.dashboard_message_id or "") or None; db.commit()
    return {"ok": True}


@app.delete("/api/telegram/link", dependencies=[Depends(csrf_guard)])
def disconnect_telegram(user: User = Depends(current_user), db: Session = Depends(get_db)):
    item = db.scalar(select(TelegramLink).where(TelegramLink.user_id == user.id))
    if item:
        db.delete(item); db.commit()
    return {"ok": True}


@app.post("/api/telegram/webhook")
async def telegram_webhook(request: Request, db: Session = Depends(get_db)):
    expected = _setting("telegram_webhook_secret", "")
    if not expected or not secrets.compare_digest(request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), expected):
        raise HTTPException(403, "Invalid webhook secret")
    update = await request.json(); message = update.get("message") or {}; callback = update.get("callback_query") or {}
    chat_id = str((callback.get("message") or message).get("chat", {}).get("id", ""))
    if message.get("text") == "/start":
        _telegram_call("sendMessage", {"chat_id": chat_id, "text": f"Welcome to EMBOXA\n\nYour Chat ID:\n{chat_id}\n\nCopy it into Emboxa Web → Preferences → Telegram. Then use Send test dashboard to create your single interactive dashboard message.",
                                       "reply_markup": {"inline_keyboard": [[{"text": "Open Emboxa Web", "url": get_setting("public_domain", PUBLIC_APP_URL).rstrip("/") + "/app"}]]}})
        return {"ok": True}
    link = db.scalar(select(TelegramLink).where(TelegramLink.chat_id == chat_id))
    if not link:
        return {"ok": True}
    user = db.get(User, link.user_id)
    if not user or user.status != "active" or not user.verified_at:
        return {"ok": True}
    data = str(callback.get("data") or "dashboard")
    message_id = str((callback.get("message") or {}).get("message_id") or link.dashboard_message_id or "") or None
    if data.startswith("backup:"):
        account_id = int(data.split(":", 1)[1]); account = db.get(Account, account_id)
        if not account or account.owner_id != user.id:
            return {"ok": True}
        if user.plan != "PLUS" and _storage_used(db, user.id) >= user.storage_limit_bytes:
            _telegram_call("answerCallbackQuery", {"callback_query_id": callback.get("id"), "text": "Storage limit reached", "show_alert": True})
            return {"ok": True}
        try:
            _job_id, created = backup_manager.start(account.id)
            if callback.get("id") and not created:
                _telegram_call("answerCallbackQuery", {"callback_query_id": callback["id"], "text": "A backup is already queued or running for this mailbox.", "show_alert": True})
                return {"ok": True}
            data = "status"
        except Exception:
            if callback.get("id"):
                _telegram_call("answerCallbackQuery", {"callback_query_id": callback["id"], "text": "The backup could not be queued. Check the mailbox in Emboxa Web.", "show_alert": True})
            return {"ok": True}
    result = _telegram_render(db, user, chat_id, message_id, data)
    if callback.get("id"):
        _telegram_call("answerCallbackQuery", {"callback_query_id": callback["id"]})
    link.dashboard_message_id = str(result.get("message_id") or message_id or "") or None; db.commit()
    return {"ok": True}


def _account_json(account: Account, job: BackupJob | None = None, db: Session | None = None) -> dict:
    return {
        "id": account.id,
        "display_name": account.display_name,
        "email": account.email,
        "auth_provider": account.auth_provider,
        "imap_host": account.imap_host,
        "imap_port": account.imap_port,
        "security": account.security,
        "imap_username": account.imap_username,
        "root_folder": account.root_folder,
        "imap_enabled": account.imap_enabled,
        "schedule_mode": account.schedule_mode,
        "schedule_interval_hours": account.schedule_interval_hours,
        "next_backup_at": account.next_backup_at,
        "last_backup_at": account.last_backup_at,
        "last_backup_status": account.last_backup_status,
        "last_backup_error": account.last_backup_error,
        "message_count": account.message_count,
        "archive_size": account_storage_used(db, account) if db else account.archive_size,
        "retention_versions": account.retention_versions,
        "is_permanent": account.is_permanent,
        "permanent_locked_until": account.permanent_locked_until,
        "has_archive": bool(account.active_snapshot_id),
        "job": _job_json(job) if job else None,
    }


def _microsoft_redirect_uri(db: Session | None = None) -> str:
    return f"{get_setting('public_domain', PUBLIC_APP_URL, db).rstrip('/')}/api/auth/microsoft/callback"


@app.get("/api/auth/microsoft/status")
def microsoft_status(_user: User = Depends(current_user), db: Session = Depends(get_db)):
    return {"configured": bool(MICROSOFT_CLIENT_ID), "provider": "microsoft", "redirect_uri": _microsoft_redirect_uri(db)}


@app.get("/api/auth/microsoft/start")
def microsoft_start(request: Request, _user: User = Depends(current_user), db: Session = Depends(get_db)):
    state = secrets.token_urlsafe(32)
    request.session["microsoft_oauth_state"] = state
    return RedirectResponse(microsoft_authorize_url(_microsoft_redirect_uri(db), state), status_code=303)


@app.get("/api/auth/microsoft/callback")
def microsoft_callback(request: Request, code: str = "", state: str = "", error: str = "",
                       error_description: str = "", db: Session = Depends(get_db)):
    user_id = request.session.get("user_id")
    expected = request.session.pop("microsoft_oauth_state", None)
    if error:
        return RedirectResponse(f"/app?microsoft=error&reason={quote(error_description or error)}", status_code=303)
    if not user_id or not expected or not state or not secrets.compare_digest(expected, state):
        return RedirectResponse("/login?microsoft=expired", status_code=303)
    user = db.get(User, user_id)
    if not user or user.status != "active" or not user.verified_at:
        return RedirectResponse("/login?microsoft=account", status_code=303)
    try:
        token = exchange_code(code, _microsoft_redirect_uri(db))
        access_token = token["access_token"]
        refresh_token = token.get("refresh_token")
        if not refresh_token:
            raise HTTPException(400, "Microsoft did not return a refresh token")
        profile = microsoft_profile(access_token)
        email = (profile.get("mail") or profile.get("userPrincipalName") or "").strip().lower()
        if not email:
            raise HTTPException(400, "Microsoft account email unavailable")
        mailbox_limit = get_int_setting("standard_mailbox_limit", STANDARD_MAILBOX_LIMIT, db)
        existing = db.scalar(select(Account).where(
            Account.owner_id == user.id,
            Account.auth_provider == "microsoft",
            Account.email == email,
        ))
        if not existing and user.plan != "PLUS" and (db.scalar(select(func.count(Account.id)).where(Account.owner_id == user.id)) or 0) >= mailbox_limit:
            raise HTTPException(409, f"Mailbox limit reached. Standard accounts can connect up to {mailbox_limit} mailboxes.")
        account = existing or Account(
            owner_id=user.id,
            archive_uuid=str(uuid.uuid4()),
            display_name=profile.get("displayName") or email,
            email=email,
            mailbox_identity=hashlib.sha256(f"{user.id}:microsoft:{profile.get('id') or email}".encode()).hexdigest(),
        )
        account.auth_provider = "microsoft"
        account.imap_host = "graph.microsoft.com"
        account.imap_port = 443
        account.security = "oauth2"
        account.imap_username = email
        account.encrypted_password = encrypt_secret(refresh_token)
        account.imap_enabled = True
        account.next_backup_at = next_backup_time(account)
        db.add(account)
        db.commit()
        return RedirectResponse("/app?microsoft=connected", status_code=303)
    except HTTPException as exc:
        return RedirectResponse(f"/app?microsoft=error&reason={quote(str(exc.detail))}", status_code=303)
    except Exception as exc:
        log.warning("Microsoft OAuth callback failed: %s", exc)
        return RedirectResponse("/app?microsoft=error&reason=Microsoft%20OAuth%20failed", status_code=303)


def _job_json(job: BackupJob) -> dict:
    return {
        "id": job.id,
        "status": job.status,
        "current_folder": job.current_folder,
        "processed_messages": job.processed_messages,
        "total_messages": job.total_messages,
        "attachment_count": job.attachment_count,
        "percent": job.percent,
        "throughput": job.throughput,
        "eta_seconds": job.eta_seconds,
        "cancel_requested": job.cancel_requested,
        "error": job.error,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "created_at": job.created_at,
        "account": {"id": job.account.id, "display_name": job.account.display_name, "email": job.account.email},
    }


def _storage_used(db: Session, user_id: int) -> int:
    return user_storage_used(db, user_id)


@app.get("/api/accounts", dependencies=[Depends(current_user)])
def list_accounts(user: User = Depends(current_user), db: Session = Depends(get_db)):
    accounts = db.scalars(select(Account).where(Account.owner_id == user.id).order_by(Account.display_name.collate("NOCASE"))).all()
    return [_account_json(account, _running_job(db, account.id), db) for account in accounts]


@app.get("/api/web/usage")
def web_usage(user: User = Depends(current_user), db: Session = Depends(get_db)):
    used = _storage_used(db, user.id)
    count = db.scalar(select(func.count(Account.id)).where(Account.owner_id == user.id)) or 0
    transfer_quota = _transfer_quota(db, user)
    active_backups = db.scalar(select(func.count(BackupJob.id)).join(Account).where(
        Account.owner_id == user.id, BackupJob.status.in_(["queued", "running", "cancelling"])
    )) or 0
    active_transfers = db.scalar(select(func.count(IMAPTransferJob.id)).where(
        IMAPTransferJob.owner_id == user.id, IMAPTransferJob.status.in_(["queued", "running", "cancelling"])
    )) or 0
    return {"plan": user.plan, "storage_used": used, "storage_limit": None if user.plan == "PLUS" else user.storage_limit_bytes,
            "mailbox_count": count, "mailbox_limit": None if user.plan == "PLUS" else get_int_setting("standard_mailbox_limit", STANDARD_MAILBOX_LIMIT, db),
            "contact": _contact_email(), "over_quota": user.plan != "PLUS" and used >= user.storage_limit_bytes,
            "active_backups": active_backups, "active_transfers": active_transfers, "imap_transfer_quota": transfer_quota}


@app.post("/api/accounts/test", dependencies=[Depends(csrf_guard)])
def test_connection(payload: ConnectionPayload):
    try:
        return test_imap_connection(payload.imap_host, payload.imap_port, payload.security, payload.imap_username, payload.password)
    except Exception as exc:
        raise HTTPException(400, f"Connessione IMAP fallita: {exc}") from exc


def _transfer_quota(db: Session, user: User) -> dict:
    period = utcnow().strftime("%Y-%m")
    used = db.scalar(select(func.count(IMAPTransferJob.id)).where(
        IMAPTransferJob.owner_id == user.id, IMAPTransferJob.quota_period == period
    )) or 0
    limit = None if user.plan == "PLUS" else get_int_setting("standard_imap_transfer_limit", 2, db)
    return {"period": period, "used": used, "limit": limit, "remaining": None if limit is None else max(0, limit - used)}


def _destination_credentials(db: Session, user: User, destination: IMAPTransferDestinationPayload) -> dict:
    if destination.account_id is not None:
        account = db.get(Account, destination.account_id)
        if not account or account.owner_id != user.id:
            raise HTTPException(404, "Destination mailbox not found")
        if account.auth_provider != "imap":
            raise HTTPException(409, "This destination does not support IMAP restore yet")
        if not account.imap_enabled or not account.encrypted_password:
            raise HTTPException(409, "Destination mailbox has no IMAP credentials")
        return {
            "account": account,
            "label": account.display_name,
            "host": account.imap_host or "",
            "port": int(account.imap_port or 993),
            "security": account.security,
            "username": account.imap_username or account.email,
            "password": decrypt_secret(account.encrypted_password),
        }
    if not all((destination.imap_host, destination.imap_port, destination.imap_username, destination.password)):
        raise HTTPException(422, "Complete the temporary IMAP destination credentials")
    return {
        "account": None,
        "label": destination.label.strip(),
        "host": destination.imap_host.strip(),
        "port": destination.imap_port,
        "security": destination.security,
        "username": destination.imap_username.strip(),
        "password": destination.password,
    }


def _test_destination(credentials: dict) -> dict:
    try:
        return test_imap_connection(
            credentials["host"], credentials["port"], credentials["security"],
            credentials["username"], credentials["password"],
        )
    except Exception as exc:
        raise HTTPException(400, f"Destination IMAP connection failed: {exc}") from exc


def _transfer_json(job: IMAPTransferJob) -> dict:
    return {
        "id": job.id, "account_id": job.account_id, "snapshot_id": job.snapshot_id,
        "destination_account_id": job.destination_account_id, "destination_label": job.destination_label,
        "mode": job.mode, "single_folder": job.single_folder, "mappings": json.loads(job.mappings_json or "{}"),
        "skip_duplicates": job.skip_duplicates, "status": job.status, "current_folder": job.current_folder,
        "processed_messages": job.processed_messages, "total_messages": job.total_messages,
        "skipped_messages": job.skipped_messages, "failed_messages": job.failed_messages,
        "percent": job.percent, "throughput": job.throughput, "eta_seconds": job.eta_seconds,
        "cancel_requested": job.cancel_requested, "quota_period": job.quota_period, "error": job.error,
        "started_at": job.started_at, "finished_at": job.finished_at, "created_at": job.created_at,
    }


@app.post("/api/imap-transfer/test", dependencies=[Depends(csrf_guard)])
def test_transfer_destination(
    payload: IMAPTransferTestPayload, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    credentials = _destination_credentials(db, user, payload.destination)
    result = _test_destination(credentials)
    return {**result, "quota_consumed": False, "destination": credentials["label"]}


@app.get("/api/accounts/{account_id}/transfer-preview", dependencies=[Depends(current_user)])
def transfer_preview(
    account_id: int, snapshot_id: int | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    account, snapshot = _active_snapshot(db, account_id, snapshot_id)
    folders = db.scalars(select(Folder).where(Folder.snapshot_id == snapshot.id).order_by(Folder.name)).all()
    return {
        "account": {"id": account.id, "display_name": account.display_name},
        "snapshot": {"id": snapshot.id, "messages": snapshot.message_count, "size": snapshot.archive_size},
        "folders": [{"id": folder.id, "name": folder.name, "messages": folder.message_count} for folder in folders],
        "destinations": [{"id": item.id, "display_name": item.display_name, "email": item.email}
                         for item in db.scalars(select(Account).where(
                             Account.owner_id == user.id, Account.auth_provider == "imap"
                         ).order_by(Account.display_name)).all()],
        "quota": _transfer_quota(db, user),
    }


@app.post("/api/accounts/{account_id}/transfers", dependencies=[Depends(csrf_guard)])
def create_transfer(
    account_id: int, payload: IMAPTransferPayload, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    account, snapshot = _active_snapshot(db, account_id, payload.snapshot_id)
    quota = _transfer_quota(db, user)
    if quota["limit"] is not None and quota["remaining"] <= 0:
        raise HTTPException(409, f"Monthly mailbox restore limit reached ({quota['limit']}). The quota resets next month.")
    if payload.mode == "single" and not (payload.single_folder or "").strip():
        raise HTTPException(422, "Choose a destination folder")
    source_folders = {item.name for item in db.scalars(select(Folder).where(Folder.snapshot_id == snapshot.id)).all()}
    mappings = {str(key).strip(): str(value).strip() for key, value in payload.mappings.items()
                if str(key).strip() in source_folders and str(value).strip()}
    if any("\x00" in value or len(value) > 500 for value in mappings.values()):
        raise HTTPException(422, "Invalid folder mapping")
    credentials = _destination_credentials(db, user, payload.destination)
    _test_destination(credentials)  # Validation/test never consumes quota; only the queued job below does.
    job = IMAPTransferJob(
        owner_id=user.id, account_id=account.id, snapshot_id=snapshot.id,
        destination_account_id=credentials["account"].id if credentials["account"] else None,
        destination_label=credentials["label"],
        destination_host=None if credentials["account"] else credentials["host"],
        destination_port=None if credentials["account"] else credentials["port"],
        destination_security=None if credentials["account"] else credentials["security"],
        destination_username=None if credentials["account"] else credentials["username"],
        encrypted_password=None if credentials["account"] else encrypt_secret(credentials["password"]),
        mode=payload.mode, single_folder=(payload.single_folder or "").strip() or None,
        mappings_json=json.dumps(mappings, ensure_ascii=False), skip_duplicates=payload.skip_duplicates,
        total_messages=snapshot.message_count, quota_period=quota["period"], status="queued",
    )
    db.add(job)
    db.commit()
    transfer_manager.submit(job.id)
    return {"ok": True, "job": _transfer_json(job), "quota": _transfer_quota(db, user)}


@app.get("/api/imap-transfers", dependencies=[Depends(current_user)])
def list_transfers(user: User = Depends(current_user), db: Session = Depends(get_db)):
    jobs = db.scalars(select(IMAPTransferJob).where(
        IMAPTransferJob.owner_id == user.id
    ).order_by(IMAPTransferJob.id.desc()).limit(100)).all()
    return {"items": [_transfer_json(job) for job in jobs], "quota": _transfer_quota(db, user)}


@app.get("/api/imap-transfers/{transfer_id}", dependencies=[Depends(current_user)])
def get_transfer(transfer_id: int, db: Session = Depends(get_db)):
    job = db.get(IMAPTransferJob, transfer_id)
    if not job:
        raise HTTPException(404, "IMAP transfer not found")
    return _transfer_json(job)


@app.post("/api/imap-transfers/{transfer_id}/cancel", dependencies=[Depends(csrf_guard)])
def cancel_transfer(transfer_id: int, db: Session = Depends(get_db)):
    job = db.get(IMAPTransferJob, transfer_id)
    if not job or job.status not in {"queued", "running", "cancelling"}:
        raise HTTPException(409, "The transfer can no longer be cancelled")
    job.cancel_requested = True
    if job.status == "queued":
        job.status = "cancelled"
        job.finished_at = utcnow()
        job.encrypted_password = None
        job.quota_period = None
    else:
        job.status = "cancelling"
    db.commit()
    return {"ok": True, "job": _transfer_json(job)}


@app.post("/api/accounts", dependencies=[Depends(csrf_guard)])
def create_account(payload: AccountPayload, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not payload.password:
        raise HTTPException(422, "La password IMAP è obbligatoria")
    mailbox_limit = get_int_setting("standard_mailbox_limit", STANDARD_MAILBOX_LIMIT, db)
    if user.plan != "PLUS" and (db.scalar(select(func.count(Account.id)).where(Account.owner_id == user.id)) or 0) >= mailbox_limit:
        raise HTTPException(409, f"Mailbox limit reached. Standard accounts can connect up to {mailbox_limit} mailboxes.")
    identity = hashlib.sha256(f"{user.id}:{str(payload.email).strip().lower()}:{payload.imap_username.strip().lower()}".encode()).hexdigest()
    account = Account(
        owner_id=user.id,
        archive_uuid=str(uuid.uuid4()),
        display_name=payload.display_name.strip(),
        email=str(payload.email),
        imap_host=payload.imap_host.strip(),
        imap_port=payload.imap_port,
        security=payload.security,
        auth_provider="imap",
        imap_username=payload.imap_username.strip(),
        encrypted_password=encrypt_secret(payload.password),
        root_folder=payload.root_folder.strip() if payload.root_folder else None,
        imap_enabled=True,
        schedule_mode=payload.schedule_mode,
        schedule_interval_hours=payload.schedule_interval_hours,
        retention_versions=payload.retention_versions or get_int_setting("default_backup_retention_versions", 3, db),
        mailbox_identity=identity,
    )
    account.next_backup_at = next_backup_time(account)
    db.add(account)
    db.commit()
    return _account_json(account, db=db)


@app.put("/api/accounts/{account_id}", dependencies=[Depends(csrf_guard)])
def update_account(account_id: int, payload: AccountPayload, db: Session = Depends(get_db)):
    account = _account_or_404(db, account_id)
    account.display_name = payload.display_name.strip()
    account.email = str(payload.email)
    account.imap_host = payload.imap_host.strip()
    account.imap_port = payload.imap_port
    account.security = payload.security
    account.imap_username = payload.imap_username.strip()
    account.root_folder = payload.root_folder.strip() if payload.root_folder else None
    account.imap_enabled = True
    account.schedule_mode = payload.schedule_mode
    account.schedule_interval_hours = payload.schedule_interval_hours
    account.retention_versions = payload.retention_versions or account.retention_versions or get_int_setting("default_backup_retention_versions", 3, db)
    if payload.password:
        account.encrypted_password = encrypt_secret(payload.password)
    elif not account.encrypted_password:
        raise HTTPException(422, "Inserisci una password IMAP per attivare questo archivio importato")
    account.next_backup_at = next_backup_time(account)
    db.commit()
    return _account_json(account, _running_job(db, account.id), db)


@app.patch("/api/accounts/{account_id}/settings", dependencies=[Depends(csrf_guard)])
def update_account_settings(account_id: int, payload: AccountSettingsPayload, user: User = Depends(current_user), db: Session = Depends(get_db)):
    account = _account_or_404(db, account_id)
    if account.owner_id != user.id:
        raise HTTPException(404, "Account non trovato")
    account.retention_versions = payload.retention_versions
    db.commit()
    return _account_json(account, _running_job(db, account.id), db)


@app.post("/api/accounts/{account_id}/test", dependencies=[Depends(csrf_guard)])
def test_saved_connection(account_id: int, db: Session = Depends(get_db)):
    account = _account_or_404(db, account_id)
    if not account.imap_enabled or not account.encrypted_password:
        raise HTTPException(409, "Configura prima l'accesso alla casella")
    try:
        if account.auth_provider == "microsoft":
            token = refresh_access_token(decrypt_secret(account.encrypted_password))
            if token.get("refresh_token"):
                account.encrypted_password = encrypt_secret(token["refresh_token"])
                db.commit()
            profile = microsoft_profile(token["access_token"])
            folders = graph_json(token["access_token"], "/me/mailFolders?$top=1")
            return {"ok": True, "provider": "microsoft", "email": profile.get("mail") or profile.get("userPrincipalName"),
                    "folders": len(folders.get("value", [])), "capabilities": ["MICROSOFT_GRAPH", "OAUTH2"]}
        return test_imap_connection(account.imap_host or "", account.imap_port or 993, account.security,
                                    account.imap_username or account.email, decrypt_secret(account.encrypted_password))
    except Exception as exc:
        raise HTTPException(400, f"Connessione casella fallita: {exc}") from exc


@app.delete("/api/accounts/{account_id}/microsoft", dependencies=[Depends(csrf_guard)])
def disconnect_microsoft_account(account_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    account = _account_or_404(db, account_id)
    if account.owner_id != user.id or account.auth_provider != "microsoft":
        raise HTTPException(404, "Microsoft mailbox not found")
    if _running_job(db, account.id):
        raise HTTPException(409, "Interrompi il backup prima di scollegare Microsoft")
    account.encrypted_password = None
    account.imap_enabled = False
    account.last_backup_status = "disconnected"
    account.last_backup_error = "Microsoft account disconnected. Reconnect with OAuth to run future backups."
    account.next_backup_at = None
    db.commit()
    return {"ok": True}


@app.post("/api/accounts/{account_id}/backup", dependencies=[Depends(csrf_guard)])
def start_backup(account_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    account = _account_or_404(db, account_id)
    if not account.imap_enabled or not account.encrypted_password:
        raise HTTPException(409, "Account non configurato o scollegato")
    if user.plan != "PLUS" and _storage_used(db, user.id) >= user.storage_limit_bytes:
        raise HTTPException(409, f"Storage limit reached. Contact the administrator at {_contact_email()}")
    if not get_bool_setting("backup_queue_enabled", db=db):
        raise HTTPException(409, "Backup queue is temporarily disabled")
    job_id, created = backup_manager.start(account_id)
    position = db.scalar(select(func.count(BackupJob.id)).where(BackupJob.status == "queued", BackupJob.id <= job_id)) or 0
    return {"job_id": job_id, "created": created, "position": position,
            "message": f"Queued · Position {position}" if position else "Backup running"}


@app.get("/api/backup-activity", dependencies=[Depends(current_user)])
def backup_activity(user: User = Depends(current_user), db: Session = Depends(get_db)):
    jobs = db.scalars(select(BackupJob).where(
        BackupJob.status.in_(["queued", "running", "cancelling"]),
        BackupJob.account_id.in_(select(Account.id).where(Account.owner_id == user.id)),
    ).order_by(BackupJob.created_at, BackupJob.id)).all()
    return {
        "running": sum(job.status in {"running", "cancelling"} for job in jobs),
        "queued": sum(job.status == "queued" for job in jobs),
        "jobs": [_job_json(job) for job in jobs],
    }


@app.get("/api/accounts/{account_id}/versions", dependencies=[Depends(current_user)])
def list_versions(account_id: int, db: Session = Depends(get_db)):
    account = _account_or_404(db, account_id)
    rows = db.scalars(select(Snapshot).where(
        Snapshot.account_id == account.id,
        Snapshot.status.in_(["completed", "active"]),
    ).order_by(Snapshot.completed_at.desc(), Snapshot.id.desc())).all()
    return [{
        "id": row.id, "created_at": row.created_at, "completed_at": row.completed_at,
        "message_count": row.message_count, "archive_size": snapshot_disk_size(account, row),
        "attachment_count": row.attachment_count, "status": row.status,
        "expires_at": row.expires_at,
        "protected": row.protected, "protection_reason": row.protection_reason,
        "current": row.id == account.active_snapshot_id,
        "comparison": json.loads(row.comparison_json) if row.comparison_json else None,
    } for row in rows]


@app.post("/api/accounts/{account_id}/permanent", dependencies=[Depends(csrf_guard)])
def make_permanent(account_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    target = _account_or_404(db, account_id)
    if target.is_permanent:
        return _account_json(target, _running_job(db, target.id), db)
    now = utcnow()
    current_items = db.scalars(select(Account).where(Account.owner_id == user.id, Account.is_permanent.is_(True))).all()
    current = current_items[0] if current_items else None
    permanent_limit = get_int_setting("permanent_mailbox_limit", 1, db)
    latest = db.scalar(select(PermanentMailboxHistory).where(PermanentMailboxHistory.user_id == user.id)
                       .order_by(PermanentMailboxHistory.id.desc()))
    if user.plan != "PLUS" and permanent_limit == 1 and latest and latest.locked_until > now and latest.mailbox_identity != target.mailbox_identity:
        raise HTTPException(409, f"Permanent mailbox locked until {latest.locked_until.date().isoformat()}")
    if user.plan != "PLUS" and permanent_limit > 1 and len(current_items) >= permanent_limit:
        raise HTTPException(409, f"Permanent mailbox limit reached ({permanent_limit})")
    if current and user.plan != "PLUS" and permanent_limit == 1:
        current.is_permanent = False
        for snapshot in current.snapshots:
            if snapshot.status in {"completed", "active"}:
                snapshot.expires_at = snapshot.created_at + timedelta(days=get_int_setting("standard_retention_days", STANDARD_RETENTION_DAYS, db))
        if latest and latest.mailbox_identity == current.mailbox_identity:
            latest.released_at = now
    lock_until = now if user.plan == "PLUS" else now + timedelta(days=get_int_setting("permanent_mailbox_lock_days", PERMANENT_MAILBOX_LOCK_DAYS, db))
    target.is_permanent = True; target.permanent_since = now; target.permanent_locked_until = lock_until
    for snapshot in target.snapshots:
        snapshot.expires_at = None
    db.add(PermanentMailboxHistory(user_id=user.id, mailbox_identity=target.mailbox_identity,
                                   designated_at=now, locked_until=lock_until))
    db.commit()
    return _account_json(target, _running_job(db, target.id), db)


class ProtectionPayload(BaseModel):
    action: Literal["keep", "replace"]


@app.post("/api/snapshots/{snapshot_id}/protection", dependencies=[Depends(csrf_guard)])
def resolve_protection(snapshot_id: int, payload: ProtectionPayload, db: Session = Depends(get_db)):
    snapshot = db.get(Snapshot, snapshot_id)
    if not snapshot or not snapshot.protected:
        raise HTTPException(404, "Versione protetta non trovata")
    if payload.action == "keep":
        snapshot.protection_reason = (snapshot.protection_reason or "") + " Confermata dall'utente."
        db.commit()
    else:
        snapshot.protected = False
        snapshot.protection_reason = None
        db.commit()
        rotate_versions(db, snapshot.account)
    return {"ok": True}


@app.get("/api/jobs/{job_id}", dependencies=[Depends(current_user)])
def get_job(job_id: int, db: Session = Depends(get_db)):
    job = db.get(BackupJob, job_id)
    if not job:
        raise HTTPException(404, "Operazione non trovata")
    return _job_json(job)


@app.post("/api/jobs/{job_id}/cancel", dependencies=[Depends(csrf_guard)])
def cancel_job(job_id: int):
    if not backup_manager.cancel(job_id):
        raise HTTPException(409, "Il backup non è più interrompibile")
    return {"ok": True}


@app.delete("/api/accounts/{account_id}/archive", dependencies=[Depends(csrf_guard)])
def clear_archive(account_id: int, db: Session = Depends(get_db)):
    _account_or_404(db, account_id)
    if _running_job(db, account_id):
        raise HTTPException(409, "Interrompi il backup prima di cancellare l'archivio")
    clear_account_archive(account_id)
    return {"ok": True}


@app.delete("/api/accounts/{account_id}", dependencies=[Depends(csrf_guard)])
def remove_account(account_id: int, db: Session = Depends(get_db)):
    _account_or_404(db, account_id)
    if _running_job(db, account_id):
        raise HTTPException(409, "Interrompi il backup prima di cancellare l'account")
    delete_account(account_id)
    return {"ok": True}


def _active_export_size(db: Session, user_id: int) -> int:
    return int(db.scalar(select(func.coalesce(func.sum(WebExport.size), 0)).where(
        WebExport.owner_id == user_id,
        (WebExport.expires_at.is_(None) | (WebExport.expires_at > utcnow())),
    )) or 0)


def _export_expiry(user: User, db: Session) -> datetime | None:
    if user.plan == "PLUS":
        return None
    ttl_hours = get_int_setting("export_ttl_hours", EXPORT_TTL_HOURS, db)
    return None if ttl_hours <= 0 else utcnow() + timedelta(hours=ttl_hours)


def _existing_export(account: Account, user: User, db: Session) -> WebExport | None:
    latest_backup = account.last_backup_at
    rows = db.scalars(
        select(WebExport)
        .where(
            WebExport.owner_id == user.id,
            WebExport.account_id == account.id,
            (WebExport.expires_at.is_(None) | (WebExport.expires_at > utcnow())),
        )
        .order_by(WebExport.id.desc())
    ).all()
    for item in rows:
        path = safe_resolve(EXPORTS_DIR, item.relpath)
        if not path.is_file():
            continue
        if latest_backup and datetime.fromtimestamp(path.stat().st_mtime) < latest_backup:
            continue
        return item
    return None


def _create_export(account_id: int, user: User, db: Session, progress=None) -> WebExport:
    account = _account_or_404(db, account_id)
    if account.owner_id != user.id:
        raise HTTPException(404, "Account non trovato")
    existing = _existing_export(account, user, db)
    if existing:
        if progress:
            progress(96, "Uso l'export già pronto più recente.")
        return existing
    active_size = account_active_archive_size(db, account)
    if user.plan != "PLUS" and _storage_used(db, user.id) + _active_export_size(db, user.id) + active_size > user.storage_limit_bytes:
        raise HTTPException(409, f"Storage limit reached. Contact the administrator at {_contact_email()}")
    try:
        try:
            path, filename = build_export(account_id, progress=progress)
        except TypeError as exc:
            if "progress" not in str(exc):
                raise
            path, filename = build_export(account_id)
    except ArchiveError as exc:
        raise HTTPException(400, str(exc)) from exc
    export_max = get_int_setting("export_max_bytes", 10 * 1024**3, db)
    if user.plan != "PLUS" and path.stat().st_size > export_max:
        path.unlink(missing_ok=True)
        shutil.rmtree(path.parent, ignore_errors=True)
        raise HTTPException(413, "Export exceeds the configured maximum size")
    public_id = str(uuid.uuid4())
    destination_dir = EXPORTS_DIR / f"user-{user.id}"; destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"{public_id}.mailvault"
    shutil.move(str(path), destination); shutil.rmtree(path.parent, ignore_errors=True)
    item = WebExport(public_id=public_id, owner_id=user.id, account_id=account_id, filename=filename,
                     relpath=str(destination.relative_to(EXPORTS_DIR)), size=destination.stat().st_size,
                     expires_at=_export_expiry(user, db))
    db.add(item); db.commit()
    db.refresh(item)
    return item


EXPORT_JOB_LOCK = threading.Lock()
EXPORT_JOBS: dict[str, dict] = {}
EXPORT_JOB_RETENTION = timedelta(hours=6)


def _export_payload(item: WebExport) -> dict:
    return {
        "id": item.public_id,
        "filename": item.filename,
        "size": item.size,
        "expires_at": item.expires_at,
        "persistent": item.expires_at is None,
        "download_url": f"/api/exports/{item.public_id}/download",
    }


def _cleanup_export_jobs() -> None:
    cutoff = utcnow() - EXPORT_JOB_RETENTION
    with EXPORT_JOB_LOCK:
        for job_id, job in list(EXPORT_JOBS.items()):
            finished_at = job.get("finished_at")
            if finished_at and finished_at < cutoff:
                EXPORT_JOBS.pop(job_id, None)


def _export_job_response(job: dict) -> dict:
    response = {
        "job_id": job["id"],
        "account_id": job["account_id"],
        "status": job["status"],
        "percent": job["percent"],
        "detail": job["detail"],
        "status_url": f"/api/exports/jobs/{job['id']}",
        "created_at": job["created_at"],
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
    }
    if job.get("error"):
        response["error"] = job["error"]
    if job.get("export"):
        response["export"] = job["export"]
    if job.get("local_path"):
        response["local_path"] = job["local_path"]
    return response


def _set_export_job(job_id: str, **changes) -> None:
    with EXPORT_JOB_LOCK:
        job = EXPORT_JOBS.get(job_id)
        if job:
            job.update(changes)


def _run_export_job(job_id: str, user_id: int, account_id: int) -> None:
    _set_export_job(job_id, status="running", percent=12, detail="Export avviato in background.", started_at=utcnow())
    try:
        with SessionLocal() as db:
            user = db.get(User, user_id)
            if not user or user.status != "active" or not user.verified_at:
                raise HTTPException(403, "Account is not available")
            def progress(percent: int, detail: str) -> None:
                _set_export_job(job_id, percent=percent, detail=detail)
            _set_export_job(job_id, percent=18, detail="Creazione del pacchetto .mailvault sul server.")
            item = _create_export(account_id, user, db, progress=progress)
            _set_export_job(
                job_id,
                status="completed",
                percent=100,
                detail="Export pronto per il download.",
                export=_export_payload(item),
                finished_at=utcnow(),
            )
    except Exception as exc:
        detail = getattr(exc, "detail", None) or str(exc) or "Export failed"
        log.warning("Export job %s failed: %s", job_id, detail)
        _set_export_job(job_id, status="failed", percent=100, detail=detail, error=detail, finished_at=utcnow())


def _run_export_local_job(job_id: str, user_id: int, account_id: int) -> None:
    _set_export_job(job_id, status="running", percent=12, detail="Export locale NAS avviato.", started_at=utcnow())
    try:
        with SessionLocal() as db:
            user = db.get(User, user_id)
            if not user or user.status != "active" or not user.verified_at:
                raise HTTPException(403, "Account is not available")
            if user.plan != "PLUS" and user.role != "admin":
                raise HTTPException(403, "Export su cartella NAS disponibile per PLUS/admin")

            def progress(percent: int, detail: str) -> None:
                _set_export_job(job_id, percent=min(82, percent), detail=detail)

            item = _create_export(account_id, user, db, progress=progress)
            source = safe_resolve(EXPORTS_DIR, item.relpath)
            if not source.is_file():
                raise ArchiveError("Export non disponibile sul server")
            LOCAL_EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
            destination = LOCAL_EXPORTS_DIR / item.filename
            if destination.exists():
                stem, suffix = destination.stem, destination.suffix
                destination = LOCAL_EXPORTS_DIR / f"{stem}-{utcnow().strftime('%Y%m%d-%H%M%S')}{suffix}"
            _set_export_job(job_id, percent=88, detail="Copia del file nella cartella locale NAS…")
            shutil.copyfile(source, destination)
            _set_export_job(
                job_id,
                status="completed",
                percent=100,
                detail=f"Export salvato su NAS: {destination}",
                export=_export_payload(item),
                local_path=str(destination),
                finished_at=utcnow(),
            )
    except Exception as exc:
        detail = getattr(exc, "detail", None) or str(exc) or "Export failed"
        log.warning("Local export job %s failed: %s", job_id, detail)
        _set_export_job(job_id, status="failed", percent=100, detail=detail, error=detail, finished_at=utcnow())


def _start_export_job(account_id: int, user: User, db: Session) -> dict:
    _cleanup_export_jobs()
    account = _account_or_404(db, account_id)
    if account.owner_id != user.id:
        raise HTTPException(404, "Account non trovato")
    active_size = account_active_archive_size(db, account)
    if user.plan != "PLUS" and _storage_used(db, user.id) + _active_export_size(db, user.id) + active_size > user.storage_limit_bytes:
        raise HTTPException(409, f"Storage limit reached. Contact the administrator at {_contact_email()}")
    with EXPORT_JOB_LOCK:
        for job in EXPORT_JOBS.values():
            if job["user_id"] == user.id and job["account_id"] == account_id and job["status"] in {"queued", "running"}:
                return job
        job_id = str(uuid.uuid4())
        job = {
            "id": job_id,
            "user_id": user.id,
            "account_id": account_id,
            "status": "queued",
            "percent": 5,
            "detail": "Export aggiunto alla coda locale.",
            "created_at": utcnow(),
            "started_at": None,
            "finished_at": None,
            "error": "",
            "export": None,
        }
        EXPORT_JOBS[job_id] = job
    thread = threading.Thread(target=_run_export_job, args=(job_id, user.id, account_id), name=f"emboxa-export-{job_id[:8]}", daemon=True)
    thread.start()
    return job


def _start_export_local_job(account_id: int, user: User, db: Session) -> dict:
    if user.plan != "PLUS" and user.role != "admin":
        raise HTTPException(403, "Export su cartella NAS disponibile per PLUS/admin")
    account = _account_or_404(db, account_id)
    if account.owner_id != user.id:
        raise HTTPException(404, "Account non trovato")
    _cleanup_export_jobs()
    with EXPORT_JOB_LOCK:
        job_id = str(uuid.uuid4())
        job = {
            "id": job_id,
            "user_id": user.id,
            "account_id": account_id,
            "status": "queued",
            "percent": 5,
            "detail": "Export su cartella NAS aggiunto alla coda.",
            "created_at": utcnow(),
            "started_at": None,
            "finished_at": None,
            "error": "",
            "export": None,
            "local_path": None,
        }
        EXPORT_JOBS[job_id] = job
    thread = threading.Thread(target=_run_export_local_job, args=(job_id, user.id, account_id), name=f"emboxa-export-local-{job_id[:8]}", daemon=True)
    thread.start()
    return job


@app.post("/api/accounts/{account_id}/export", dependencies=[Depends(csrf_guard)])
def create_export(account_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return _export_job_response(_start_export_job(account_id, user, db))


@app.post("/api/accounts/{account_id}/export/local", dependencies=[Depends(csrf_guard)])
def create_local_export(account_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return _export_job_response(_start_export_local_job(account_id, user, db))


@app.get("/api/accounts/{account_id}/export", dependencies=[Depends(current_user)])
def export_account(account_id: int, response: Response, user: User = Depends(current_user), db: Session = Depends(get_db)):
    response.status_code = 202
    return _export_job_response(_start_export_job(account_id, user, db))


@app.get("/api/exports/jobs/{job_id}", dependencies=[Depends(current_user)])
def export_job_status(job_id: str, user: User = Depends(current_user)):
    with EXPORT_JOB_LOCK:
        job = EXPORT_JOBS.get(job_id)
        if not job or job["user_id"] != user.id:
            raise HTTPException(404, "Export job not found")
        return _export_job_response(dict(job))


@app.get("/api/exports/{public_id}/download")
def download_export(public_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    item = db.scalar(select(WebExport).where(WebExport.public_id == public_id, WebExport.owner_id == user.id))
    if not item or (item.expires_at is not None and item.expires_at < utcnow()):
        raise HTTPException(404, "Export expired or unavailable")
    path = safe_resolve(EXPORTS_DIR, item.relpath)
    if not path.is_file():
        raise HTTPException(404, "Export unavailable")
    return FileResponse(path, filename=item.filename, media_type="application/vnd.mailvault+zip",
                        headers={"Cache-Control": "no-store"})


MBOX_IMPORT_LOCK = threading.Lock()
MBOX_IMPORT_JOBS: dict[str, dict] = {}
MBOX_IMPORT_JOB_RETENTION = timedelta(hours=6)


def _is_mbox_upload(filename: str) -> bool:
    clean = (filename or "").replace("\\", "/").strip("/")
    if not clean:
        return False
    parts = [part.lower() for part in PurePosixPath(clean).parts if part and part != "."]
    if not parts or parts[-1] in {".ds_store", "table_of_contents"}:
        return False
    return parts[-1] == "mbox" or parts[-1].endswith(".mbox") or any(part.endswith(".mbox") for part in parts[:-1])


def _cleanup_mbox_import_jobs() -> None:
    cutoff = utcnow() - MBOX_IMPORT_JOB_RETENTION
    with MBOX_IMPORT_LOCK:
        for job_id, job in list(MBOX_IMPORT_JOBS.items()):
            finished_at = job.get("finished_at")
            if finished_at and finished_at < cutoff:
                MBOX_IMPORT_JOBS.pop(job_id, None)


def _mbox_import_job_response(job: dict) -> dict:
    response = {
        "job_id": job["id"],
        "status": job["status"],
        "percent": job["percent"],
        "detail": job["detail"],
        "status_url": f"/api/import/mbox/jobs/{job['id']}",
        "created_at": job["created_at"],
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "messages": job.get("messages", 0),
    }
    if job.get("error"):
        response["error"] = job["error"]
    if job.get("account"):
        response["account"] = job["account"]
    return response


def _set_mbox_import_job(job_id: str, **changes) -> None:
    with MBOX_IMPORT_LOCK:
        job = MBOX_IMPORT_JOBS.get(job_id)
        if job:
            job.update(changes)


def _run_mbox_import_job(job_id: str, user_id: int, upload_dir: Path, sources: list[MboxSource], display_name: str, email: str) -> None:
    _set_mbox_import_job(job_id, status="running", percent=35, detail="Import MBOX avviato.", started_at=utcnow())
    try:
        def progress(percent: int, messages: int, folder: str) -> None:
            mapped = 35 + int(min(63, max(0, percent - 15) / 80 * 63))
            _set_mbox_import_job(
                job_id,
                percent=mapped,
                messages=messages,
                detail=f"{folder} · {messages} messaggi importati",
            )

        account_id = import_mbox_sources(sources, user_id, display_name=display_name, email=email, progress=progress)
        with SessionLocal() as db:
            account = db.get(Account, account_id)
            if not account:
                raise MboxImportError("Account importato non trovato")
            _set_mbox_import_job(
                job_id,
                status="completed",
                percent=100,
                detail="Import MBOX completato.",
                account=_account_json(account, db=db),
                finished_at=utcnow(),
            )
    except Exception as exc:
        detail = getattr(exc, "detail", None) or str(exc) or "Import MBOX failed"
        log.warning("MBOX import job %s failed: %s", job_id, detail)
        _set_mbox_import_job(job_id, status="failed", percent=100, detail=detail, error=detail, finished_at=utcnow())
    finally:
        shutil.rmtree(upload_dir, ignore_errors=True)


def _download_name_from_response(url: str, headers) -> str:
    disposition = headers.get("content-disposition", "") if headers else ""
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', disposition, flags=re.I)
    if match:
        return Path(match.group(1).strip()).name
    name = Path(urlparse(url).path).name
    return name or "download.mbox"


def _run_mbox_link_import_job(job_id: str, user_id: int, payload: MboxLinkPayload, upload_dir: Path) -> None:
    _set_mbox_import_job(job_id, status="running", percent=3, detail="Download MBOX dal link…", started_at=utcnow())
    try:
        parsed = urlparse(payload.url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise MboxImportError("Inserisci un link http/https diretto a un file MBOX")
        request = URLRequest(payload.url, headers={"User-Agent": "Emboxa-Web/1.0"})
        with urlopen(request, timeout=60) as response:
            status = getattr(response, "status", 200)
            if status >= 400:
                raise MboxImportError(f"Download non riuscito: HTTP {status}")
            total = int(response.headers.get("content-length") or 0)
            original = _download_name_from_response(payload.url, response.headers)
            if not _is_mbox_upload(original):
                original = f"{Path(original).stem or 'download'}.mbox"
            destination = upload_dir / "00000.mbox"
            downloaded = 0
            with destination.open("wb") as target:
                while chunk := response.read(1024 * 1024):
                    downloaded += len(chunk)
                    if downloaded > IMPORT_MAX_BYTES:
                        raise MboxImportError("Il file MBOX supera il limite di import configurato")
                    target.write(chunk)
                    if total:
                        percent = 3 + int(min(32, downloaded / max(1, total) * 32))
                        _set_mbox_import_job(job_id, percent=percent, detail=f"Download MBOX · {downloaded / 1024**2:.1f} MB")
                    else:
                        _set_mbox_import_job(job_id, percent=12, detail=f"Download MBOX · {downloaded / 1024**2:.1f} MB")
        if not destination.exists() or destination.stat().st_size == 0:
            raise MboxImportError("Il link non ha scaricato un file valido")

        source = MboxSource(destination, original, folder_name_from_upload(original))

        def progress(percent: int, messages: int, folder: str) -> None:
            mapped = 35 + int(min(63, max(0, percent - 15) / 80 * 63))
            _set_mbox_import_job(job_id, percent=mapped, messages=messages, detail=f"{folder} · {messages} messaggi importati")

        _set_mbox_import_job(job_id, percent=35, detail="Download completato. Import MBOX avviato.")
        account_id = import_mbox_sources([source], user_id, display_name=payload.display_name, email=payload.email, progress=progress)
        with SessionLocal() as db:
            account = db.get(Account, account_id)
            if not account:
                raise MboxImportError("Account importato non trovato")
            _set_mbox_import_job(
                job_id,
                status="completed",
                percent=100,
                detail="Import MBOX completato.",
                account=_account_json(account, db=db),
                finished_at=utcnow(),
            )
    except Exception as exc:
        detail = getattr(exc, "detail", None) or str(exc) or "Import MBOX failed"
        log.warning("MBOX link import job %s failed: %s", job_id, detail)
        _set_mbox_import_job(job_id, status="failed", percent=100, detail=detail, error=detail, finished_at=utcnow())
    finally:
        shutil.rmtree(upload_dir, ignore_errors=True)


@app.post("/api/import/mbox", dependencies=[Depends(csrf_guard)])
async def upload_mbox_import(
    files: Annotated[list[UploadFile], File()],
    display_name: Annotated[str, Form()] = "Archivio MBOX importato",
    email: Annotated[str, Form()] = "mbox-import@local.invalid",
    user: User = Depends(current_user),
):
    if user.plan != "PLUS":
        raise HTTPException(403, "L'import MBOX è una funzione PLUS")
    _cleanup_mbox_import_jobs()
    IMPORTS_DIR.mkdir(parents=True, exist_ok=True)
    upload_dir = Path(tempfile.mkdtemp(prefix="mbox-upload-", dir=IMPORTS_DIR))
    sources: list[MboxSource] = []
    total_size = 0
    try:
        for index, file in enumerate(files):
            original = file.filename or f"upload-{index}.mbox"
            if not _is_mbox_upload(original):
                await file.close()
                continue
            destination = upload_dir / f"{index:05d}.mbox"
            size = 0
            with destination.open("wb") as target:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    total_size += len(chunk)
                    if total_size > IMPORT_MAX_BYTES:
                        raise HTTPException(413, "I file MBOX superano il limite di import configurato")
                    target.write(chunk)
            await file.close()
            sources.append(MboxSource(destination, original, folder_name_from_upload(original)))
        if not sources:
            raise HTTPException(400, "Seleziona uno o più file MBOX validi")
        job_id = str(uuid.uuid4())
        job = {
            "id": job_id,
            "user_id": user.id,
            "status": "queued",
            "percent": 33,
            "detail": f"{len(sources)} file MBOX caricati. Import in coda.",
            "created_at": utcnow(),
            "started_at": None,
            "finished_at": None,
            "messages": 0,
            "error": "",
            "account": None,
        }
        with MBOX_IMPORT_LOCK:
            MBOX_IMPORT_JOBS[job_id] = job
        thread = threading.Thread(
            target=_run_mbox_import_job,
            args=(job_id, user.id, upload_dir, sources, display_name, email),
            name=f"emboxa-mbox-{job_id[:8]}",
            daemon=True,
        )
        thread.start()
        return _mbox_import_job_response(job)
    except Exception:
        shutil.rmtree(upload_dir, ignore_errors=True)
        raise
    finally:
        for file in files:
            try:
                await file.close()
            except Exception:
                pass


@app.post("/api/import/mbox/link", dependencies=[Depends(csrf_guard)])
def link_mbox_import(payload: MboxLinkPayload, user: User = Depends(current_user)):
    if user.plan != "PLUS":
        raise HTTPException(403, "L'import MBOX è una funzione PLUS")
    _cleanup_mbox_import_jobs()
    IMPORTS_DIR.mkdir(parents=True, exist_ok=True)
    upload_dir = Path(tempfile.mkdtemp(prefix="mbox-link-", dir=IMPORTS_DIR))
    job_id = str(uuid.uuid4())
    job = {
        "id": job_id,
        "user_id": user.id,
        "status": "queued",
        "percent": 1,
        "detail": "Download MBOX in coda.",
        "created_at": utcnow(),
        "started_at": None,
        "finished_at": None,
        "messages": 0,
        "error": "",
        "account": None,
    }
    with MBOX_IMPORT_LOCK:
        MBOX_IMPORT_JOBS[job_id] = job
    thread = threading.Thread(
        target=_run_mbox_link_import_job,
        args=(job_id, user.id, payload, upload_dir),
        name=f"emboxa-mbox-link-{job_id[:8]}",
        daemon=True,
    )
    thread.start()
    return _mbox_import_job_response(job)


@app.get("/api/import/mbox/jobs/{job_id}", dependencies=[Depends(current_user)])
def mbox_import_job_status(job_id: str, user: User = Depends(current_user)):
    with MBOX_IMPORT_LOCK:
        job = MBOX_IMPORT_JOBS.get(job_id)
        if not job or job["user_id"] != user.id:
            raise HTTPException(404, "MBOX import job not found")
        return _mbox_import_job_response(dict(job))


IMPORT_JOB_LOCK = threading.Lock()
IMPORT_JOBS: dict[str, dict] = {}
IMPORT_JOB_RETENTION = timedelta(hours=6)


def _cleanup_import_jobs() -> None:
    cutoff = utcnow() - IMPORT_JOB_RETENTION
    with IMPORT_JOB_LOCK:
        for job_id, job in list(IMPORT_JOBS.items()):
            finished_at = job.get("finished_at")
            if finished_at and finished_at < cutoff:
                IMPORT_JOBS.pop(job_id, None)


def _import_job_response(job: dict) -> dict:
    response = {
        "job_id": job["id"],
        "status": job["status"],
        "percent": job["percent"],
        "detail": job["detail"],
        "status_url": f"/api/import/jobs/{job['id']}",
        "created_at": job["created_at"],
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
    }
    if job.get("error"):
        response["error"] = job["error"]
    if job.get("account_id"):
        response["account_id"] = job["account_id"]
    if job.get("account_ids"):
        response["account_ids"] = job["account_ids"]
    return response


def _set_import_job(job_id: str, **changes) -> None:
    with IMPORT_JOB_LOCK:
        job = IMPORT_JOBS.get(job_id)
        if job:
            job.update(changes)


def _run_mailvault_link_import_job(job_id: str, user_id: int, url: str, temp_path: Path) -> None:
    _set_import_job(job_id, status="running", percent=3, detail="Download archivio dal link…", started_at=utcnow())
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ArchiveError("Inserisci un link http/https diretto a un file .mailvault")
        request = URLRequest(url, headers={"User-Agent": "Emboxa-Web/1.0"})
        with urlopen(request, timeout=60) as response:
            status = getattr(response, "status", 200)
            if status >= 400:
                raise ArchiveError(f"Download non riuscito: HTTP {status}")
            total = int(response.headers.get("content-length") or 0)
            downloaded = 0
            with temp_path.open("wb") as target:
                while chunk := response.read(1024 * 1024):
                    downloaded += len(chunk)
                    if downloaded > IMPORT_MAX_BYTES:
                        raise ArchiveError("Il file supera il limite di import configurato")
                    target.write(chunk)
                    if total:
                        percent = 3 + int(min(62, downloaded / max(1, total) * 62))
                        detail = f"Download archivio · {downloaded / 1024**2:.1f} MB / {total / 1024**2:.1f} MB"
                    else:
                        percent = 20
                        detail = f"Download archivio · {downloaded / 1024**2:.1f} MB"
                    _set_import_job(job_id, percent=percent, detail=detail)
        if not temp_path.exists() or temp_path.stat().st_size == 0:
            raise ArchiveError("Il link non ha scaricato un archivio valido")
        _set_import_job(job_id, percent=72, detail="Verifica e importazione del pacchetto .mailvault…")
        account_id = import_archive(temp_path, user_id)
        _set_import_job(job_id, status="completed", percent=100, detail="Archivio importato.", account_id=account_id, finished_at=utcnow())
    except Exception as exc:
        detail = getattr(exc, "detail", None) or str(exc) or "Import failed"
        log.warning("Mailvault link import job %s failed: %s", job_id, detail)
        _set_import_job(job_id, status="failed", percent=100, detail=detail, error=detail, finished_at=utcnow())
    finally:
        temp_path.unlink(missing_ok=True)


def _run_mailvault_upload_import_job(job_id: str, user_id: int, temp_path: Path) -> None:
    _set_import_job(job_id, status="running", percent=74, detail="Verifica e importazione del pacchetto .mailvault…", started_at=utcnow())
    try:
        account_id = import_archive(temp_path, user_id)
        _set_import_job(job_id, status="completed", percent=100, detail="Archivio importato.", account_id=account_id, account_ids=[account_id], finished_at=utcnow())
    except Exception as exc:
        detail = getattr(exc, "detail", None) or str(exc) or "Import failed"
        log.warning("Mailvault upload import job %s failed: %s", job_id, detail)
        _set_import_job(job_id, status="failed", percent=100, detail=detail, error=detail, finished_at=utcnow())
    finally:
        temp_path.unlink(missing_ok=True)


def _local_mailvault_files() -> list[Path]:
    if not LOCAL_IMPORTS_DIR.is_dir():
        return []
    return sorted(path for path in LOCAL_IMPORTS_DIR.rglob("*.mailvault") if path.is_file())


def _local_mbox_sources() -> list[MboxSource]:
    if not LOCAL_IMPORTS_DIR.is_dir():
        return []
    sources: list[MboxSource] = []
    for path in sorted(LOCAL_IMPORTS_DIR.rglob("*")):
        if not path.is_file() or path.name.lower() in {".ds_store", "table_of_contents"}:
            continue
        rel = path.relative_to(LOCAL_IMPORTS_DIR).as_posix()
        if _is_mbox_upload(rel):
            sources.append(MboxSource(path, rel, folder_name_from_upload(rel)))
    return sources


def _run_local_import_job(job_id: str, user_id: int, payload: LocalImportPayload) -> None:
    _set_import_job(job_id, status="running", percent=3, detail=f"Scansione cartella NAS {LOCAL_IMPORTS_DIR}…", started_at=utcnow())
    try:
        imported: list[int] = []
        mailvaults = _local_mailvault_files() if payload.mode in {"auto", "mailvault"} else []
        mbox_sources = _local_mbox_sources() if payload.mode in {"auto", "mbox"} else []
        total_steps = len(mailvaults) + (1 if mbox_sources else 0)
        if not total_steps:
            raise ArchiveError(f"Nessun file importabile trovato in {LOCAL_IMPORTS_DIR}")

        done = 0
        for path in mailvaults:
            done += 1
            percent = 5 + int((done - 1) / max(1, total_steps) * 90)
            _set_import_job(job_id, percent=percent, detail=f"Import .mailvault da NAS: {path.name}")
            imported.append(import_archive(path, user_id))

        if mbox_sources:
            def progress(percent: int, messages: int, folder: str) -> None:
                base = 5 + int(done / max(1, total_steps) * 90)
                span = max(1, int(90 / max(1, total_steps)))
                mapped = min(98, base + int(percent / 100 * span))
                _set_import_job(job_id, percent=mapped, detail=f"{folder} · {messages} messaggi importati")

            imported.append(import_mbox_sources(
                mbox_sources,
                user_id,
                display_name=payload.display_name,
                email=payload.email,
                progress=progress,
            ))

        _set_import_job(
            job_id,
            status="completed",
            percent=100,
            detail=f"Import da cartella NAS completato · {len(imported)} archivio/i creati.",
            account_id=imported[-1] if imported else None,
            account_ids=imported,
            finished_at=utcnow(),
        )
    except Exception as exc:
        detail = getattr(exc, "detail", None) or str(exc) or "Import failed"
        log.warning("Local folder import job %s failed: %s", job_id, detail)
        _set_import_job(job_id, status="failed", percent=100, detail=detail, error=detail, finished_at=utcnow())


@app.post("/api/import/link", dependencies=[Depends(csrf_guard)])
def link_import(payload: ImportLinkPayload, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if user.plan != "PLUS" and (db.scalar(select(func.count(Account.id)).where(Account.owner_id == user.id)) or 0) >= get_int_setting("standard_mailbox_limit", STANDARD_MAILBOX_LIMIT, db):
        raise HTTPException(409, "Mailbox limit reached")
    _cleanup_import_jobs()
    IMPORTS_DIR.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix="link-", suffix=".mailvault", dir=IMPORTS_DIR)
    os.close(descriptor)
    temp_path = Path(temp_name)
    job_id = str(uuid.uuid4())
    job = {
        "id": job_id,
        "user_id": user.id,
        "status": "queued",
        "percent": 1,
        "detail": "Download archivio in coda.",
        "created_at": utcnow(),
        "started_at": None,
        "finished_at": None,
        "account_id": None,
        "error": "",
    }
    with IMPORT_JOB_LOCK:
        IMPORT_JOBS[job_id] = job
    thread = threading.Thread(
        target=_run_mailvault_link_import_job,
        args=(job_id, user.id, payload.url, temp_path),
        name=f"emboxa-import-link-{job_id[:8]}",
        daemon=True,
    )
    thread.start()
    return _import_job_response(job)


@app.post("/api/import/local", dependencies=[Depends(csrf_guard)])
def local_import(payload: LocalImportPayload, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if user.plan != "PLUS" and user.role != "admin":
        raise HTTPException(403, "Import da cartella NAS disponibile per PLUS/admin")
    if user.plan != "PLUS" and (db.scalar(select(func.count(Account.id)).where(Account.owner_id == user.id)) or 0) >= get_int_setting("standard_mailbox_limit", STANDARD_MAILBOX_LIMIT, db):
        raise HTTPException(409, "Mailbox limit reached")
    _cleanup_import_jobs()
    LOCAL_IMPORTS_DIR.mkdir(parents=True, exist_ok=True)
    job_id = str(uuid.uuid4())
    job = {
        "id": job_id,
        "user_id": user.id,
        "status": "queued",
        "percent": 1,
        "detail": f"Import da cartella NAS in coda: {LOCAL_IMPORTS_DIR}",
        "created_at": utcnow(),
        "started_at": None,
        "finished_at": None,
        "account_id": None,
        "account_ids": [],
        "error": "",
    }
    with IMPORT_JOB_LOCK:
        IMPORT_JOBS[job_id] = job
    thread = threading.Thread(
        target=_run_local_import_job,
        args=(job_id, user.id, payload),
        name=f"emboxa-import-local-{job_id[:8]}",
        daemon=True,
    )
    thread.start()
    return _import_job_response(job)


@app.get("/api/import/jobs/{job_id}", dependencies=[Depends(current_user)])
def import_job_status(job_id: str, user: User = Depends(current_user)):
    with IMPORT_JOB_LOCK:
        job = IMPORT_JOBS.get(job_id)
        if not job or job["user_id"] != user.id:
            raise HTTPException(404, "Import job not found")
        return _import_job_response(dict(job))


@app.post("/api/import", dependencies=[Depends(csrf_guard)])
async def upload_import(file: Annotated[UploadFile, File()], user: User = Depends(current_user), db: Session = Depends(get_db)):
    if user.plan != "PLUS" and (db.scalar(select(func.count(Account.id)).where(Account.owner_id == user.id)) or 0) >= get_int_setting("standard_mailbox_limit", STANDARD_MAILBOX_LIMIT, db):
        raise HTTPException(409, "Mailbox limit reached")
    if not (file.filename or "").lower().endswith(".mailvault"):
        raise HTTPException(400, "Seleziona un file .mailvault")
    IMPORTS_DIR.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix="upload-", suffix=".mailvault", dir=IMPORTS_DIR)
    os.close(descriptor)
    temp_path = Path(temp_name)
    size = 0
    keep_for_job = False
    try:
        with temp_path.open("wb") as target:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > IMPORT_MAX_BYTES:
                    raise HTTPException(413, "Il file supera il limite di import configurato")
                target.write(chunk)
        _cleanup_import_jobs()
        job_id = str(uuid.uuid4())
        job = {
            "id": job_id,
            "user_id": user.id,
            "status": "queued",
            "percent": 72,
            "detail": "Upload completato. Import in coda.",
            "created_at": utcnow(),
            "started_at": None,
            "finished_at": None,
            "account_id": None,
            "account_ids": [],
            "error": "",
        }
        with IMPORT_JOB_LOCK:
            IMPORT_JOBS[job_id] = job
        keep_for_job = True
        thread = threading.Thread(
            target=_run_mailvault_upload_import_job,
            args=(job_id, user.id, temp_path),
            name=f"emboxa-import-upload-{job_id[:8]}",
            daemon=True,
        )
        thread.start()
        return _import_job_response(job)
    finally:
        if not keep_for_job:
            temp_path.unlink(missing_ok=True)
        await file.close()


@app.get("/api/accounts/{account_id}/folders", dependencies=[Depends(current_user)])
def list_folders(account_id: int, snapshot_id: int | None = None, db: Session = Depends(get_db)):
    _account, snapshot = _active_snapshot(db, account_id, snapshot_id)
    folders = db.scalars(select(Folder).where(Folder.snapshot_id == snapshot.id).order_by(Folder.name.collate("NOCASE"))).all()
    return [{"id": folder.id, "name": folder.name, "delimiter": folder.delimiter,
             "flags": json.loads(folder.flags_json or "[]"), "message_count": folder.message_count} for folder in folders]


def _bool_param(value: str | None) -> bool | None:
    if value is None or value == "":
        return None
    return value.lower() in {"1", "true", "yes"}


def _fts_expression(value: str) -> str:
    tokens = re.findall(r"[\w@.+-]+", value, flags=re.UNICODE)
    return " AND ".join('"' + token.replace('"', '""') + '"' for token in tokens[:20])


@app.get("/api/accounts/{account_id}/messages", dependencies=[Depends(current_user)])
def list_messages(
    account_id: int,
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=10, le=100),
    q: str | None = Query(None, max_length=500),
    sender: str | None = Query(None, max_length=320),
    recipient: str | None = Query(None, max_length=320),
    subject: str | None = Query(None, max_length=500),
    folder_id: int | None = None,
    snapshot_id: int | None = None,
    trash: bool = False,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    has_attachments: str | None = None,
    is_read: str | None = None,
    is_starred: str | None = None,
    sort: Literal["date_desc", "date_asc", "sender", "subject"] = "date_desc",
):
    _account, snapshot = _active_snapshot(db, account_id, snapshot_id)
    query = select(Message).where(Message.snapshot_id == snapshot.id, Message.is_deleted.is_(trash))
    if folder_id:
        query = query.where(Message.folder_id == folder_id)
    if q:
        matches = [Message.id.in_(select(Attachment.message_id).where(Attachment.filename.ilike(f"%{q}%")))]
        if fts := _fts_expression(q):
            fts_ids = select(text("CAST(message_id AS INTEGER)")).select_from(text("message_fts")).where(
                text("snapshot_id=:fts_sid AND message_fts MATCH :fts_query")
            ).params(fts_sid=snapshot.id, fts_query=fts)
            matches.append(Message.id.in_(fts_ids))
        query = query.where(or_(*matches))
    if sender:
        query = query.where(Message.sender.ilike(f"%{sender}%"))
    if recipient:
        query = query.where(or_(Message.recipients_to.ilike(f"%{recipient}%"), Message.recipients_cc.ilike(f"%{recipient}%"), Message.recipients_bcc.ilike(f"%{recipient}%")))
    if subject:
        query = query.where(Message.subject.ilike(f"%{subject}%"))
    if date_from:
        query = query.where(Message.date_utc >= date_from)
    if date_to:
        query = query.where(Message.date_utc <= date_to)
    for value, column in ((_bool_param(has_attachments), Message.has_attachments), (_bool_param(is_read), Message.is_read), (_bool_param(is_starred), Message.is_starred)):
        if value is not None:
            query = query.where(column.is_(value))
    total = db.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
    order = {
        "date_desc": (Message.date_utc.desc().nullslast(), Message.id.desc()),
        "date_asc": (Message.date_utc.asc().nullsfirst(), Message.id.asc()),
        "sender": (Message.sender.collate("NOCASE"), Message.date_utc.desc()),
        "subject": (Message.subject.collate("NOCASE"), Message.date_utc.desc()),
    }[sort]
    rows = db.scalars(query.order_by(*order).offset((page - 1) * page_size).limit(page_size)).all()
    return {
        "page": page, "page_size": page_size, "total": total,
        "items": [{
            "id": item.id, "folder_id": item.folder_id, "subject": item.subject or "(senza oggetto)",
            "sender": item.sender, "date": item.date_utc or item.internal_date,
            "snippet": re.sub(r"\s+", " ", item.text_body or "").strip()[:220],
            "is_read": item.is_read, "is_starred": item.is_starred,
            "has_attachments": item.has_attachments, "thread_key": item.thread_key,
            "is_deleted": item.is_deleted,
        } for item in rows],
    }


ALLOWED_TAGS = set(bleach.sanitizer.ALLOWED_TAGS) | {
    "p", "div", "span", "br", "hr", "table", "thead", "tbody", "tfoot", "tr", "th", "td",
    "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "img", "ul", "ol", "li", "center",
    "style", "section", "header", "footer", "main", "article", "figure", "figcaption", "small", "big", "sup", "sub",
}
CSS = CSSSanitizer(allowed_css_properties=[
    "color", "background-color", "font-size", "font-family", "font-weight", "font-style", "text-align",
    "text-decoration", "margin", "margin-top", "margin-right", "margin-bottom", "margin-left", "padding",
    "padding-top", "padding-right", "padding-bottom", "padding-left", "border", "border-width", "border-style",
    "border-color", "border-radius", "border-collapse", "border-spacing", "width", "min-width", "max-width",
    "height", "min-height", "max-height", "line-height", "white-space", "display", "vertical-align",
    "letter-spacing", "word-break", "overflow-wrap", "float", "clear", "opacity", "list-style", "table-layout",
])


def _html_attributes(tag: str, name: str, value: str) -> bool:
    if name in {"title", "alt", "width", "height", "colspan", "rowspan", "style", "class", "id", "align",
                "valign", "bgcolor", "border", "cellpadding", "cellspacing", "dir", "lang", "role"}:
        return True
    if name.startswith("aria-"):
        return True
    if tag == "a" and name in {"href", "target", "rel"}:
        return True
    if tag == "img" and name == "src":
        return True
    return False


def _email_html(message: Message, remote_images: bool = False) -> str:
    cid_urls = {
        str(item.content_id).strip("<>").lower(): f"/api/attachments/{item.id}?inline=1"
        for item in message.attachments if item.content_id
    }
    html = re.sub(
        r"<\s*(script|iframe|object|embed|form|svg)\b[^>]*>.*?<\s*/\s*\1\s*>",
        "",
        message.html_body or "",
        flags=re.I | re.S,
    )
    html = re.sub(r"<\s*(?:script|iframe|object|embed|form|svg)\b[^>]*/?\s*>", "", html, flags=re.I | re.S)
    html = re.sub(
        r"cid:([^\"' >]+)",
        lambda match: cid_urls.get(match.group(1).strip("<>").lower(), ""),
        html,
        flags=re.I,
    )
    if not remote_images:
        html = re.sub(
            r"(<img\b[^>]*?\s)src\s*=\s*([\"'])(?:https?:)?//.*?\2",
            r"\1data-remote-blocked=\2true\2 alt=\2[Remote image blocked]\2",
            html,
            flags=re.I | re.S,
        )
        html = re.sub(r"\sbackground\s*=\s*([\"'])(?:https?:)?//.*?\1", "", html, flags=re.I | re.S)
    clean = bleach.clean(
        html, tags=ALLOWED_TAGS, attributes=_html_attributes,
        protocols={"http", "https", "mailto", "data"}, css_sanitizer=CSS, strip=True,
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<base target='_blank'><style>html{background:#fff;color:#111}body{margin:0;padding:16px;"
        "max-width:100%;overflow-x:auto;overflow-wrap:anywhere}img{max-width:100%;height:auto}"
        "table{max-width:100%}pre{white-space:pre-wrap}</style></head><body>" + clean + "</body></html>"
    )


def _message_json(message: Message, include_body: bool = True) -> dict:
    attachments = [{
        "id": item.id, "filename": item.filename, "content_type": item.content_type,
        "size": item.size, "content_id": item.content_id, "is_inline": item.is_inline,
        "extension": Path(item.filename).suffix.lower().lstrip("."), "category": _attachment_category(item),
        "open_url": f"/api/attachments/{item.id}?inline=1", "download_url": f"/api/attachments/{item.id}",
        "url": f"/api/attachments/{item.id}",
    } for item in message.attachments]
    result = {
        "id": message.id, "folder_id": message.folder_id, "folder": message.folder.name,
        "snapshot_id": message.snapshot_id, "is_deleted": message.is_deleted, "deleted_at": message.deleted_at,
        "message_id": message.message_id, "subject": message.subject or "(senza oggetto)",
        "sender": message.sender, "to": message.recipients_to, "cc": message.recipients_cc,
        "bcc": message.recipients_bcc, "reply_to": message.reply_to,
        "date": message.date_utc or message.internal_date, "is_read": message.is_read,
        "is_starred": message.is_starred, "is_answered": message.is_answered,
        "flags": json.loads(message.flags_json or "[]"), "attachments": attachments,
        "raw_url": f"/api/messages/{message.id}/raw",
    }
    if include_body:
        result["has_html"] = bool(message.html_body.strip())
        result["render_url"] = f"/api/messages/{message.id}/render"
        result["text_body"] = message.text_body
        result["headers"] = json.loads(message.headers_json or "[]")
    return result


@app.get("/api/messages/{message_id}", dependencies=[Depends(current_user)])
def get_message(message_id: int, db: Session = Depends(get_db)):
    message = db.get(Message, message_id)
    if not message:
        raise HTTPException(404, "Messaggio non trovato")
    account = db.get(Account, message.snapshot.account_id)
    if not account or message.snapshot.status not in {"completed", "active"}:
        raise HTTPException(404, "Messaggio non disponibile")
    return _message_json(message)


@app.get("/api/messages/{message_id}/render", response_class=HTMLResponse, dependencies=[Depends(current_user)])
def render_message_html(message_id: int, remote_images: bool = False, db: Session = Depends(get_db)):
    message = db.get(Message, message_id)
    if not message or message.snapshot.status not in {"completed", "active"}:
        raise HTTPException(404, "Messaggio non disponibile")
    if not message.html_body.strip():
        body = "<pre>" + bleach.clean(message.text_body or "") + "</pre>"
        return HTMLResponse(_email_html(message).replace("</body>", body + "</body>"))
    return HTMLResponse(_email_html(message, remote_images=remote_images))


@app.get("/api/messages/{message_id}/thread", dependencies=[Depends(current_user)])
def get_thread(message_id: int, db: Session = Depends(get_db)):
    message = db.get(Message, message_id)
    if not message:
        raise HTTPException(404, "Messaggio non trovato")
    account = db.get(Account, message.snapshot.account_id)
    if not account or message.snapshot.status not in {"completed", "active"}:
        raise HTTPException(404, "Conversazione non disponibile")
    rows = db.scalars(select(Message).where(
        Message.snapshot_id == message.snapshot_id,
        Message.thread_key == message.thread_key,
        Message.is_deleted == message.is_deleted,
    ).order_by(Message.date_utc.asc().nullsfirst(), Message.id.asc())).all()
    return [_message_json(item, include_body=True) for item in rows]


def _audit_identifier(message: Message) -> str:
    return (message.message_id or f"sha256:{message.raw_sha256}" or f"local:{message.id}")[:1000]


def _fts_insert(db: Session, message: Message) -> None:
    recipients = " ".join((message.recipients_to, message.recipients_cc, message.recipients_bcc))
    db.execute(text(
        "INSERT INTO message_fts(message_id,snapshot_id,subject,sender,recipients,body) "
        "VALUES (:mid,:sid,:subject,:sender,:recipients,:body)"
    ), {"mid": message.id, "sid": message.snapshot_id, "subject": message.subject,
        "sender": message.sender, "recipients": recipients, "body": message.text_body})


def _adjust_archive_counts(db: Session, message: Message, delta: int) -> None:
    snapshot = message.snapshot
    folder = message.folder
    attachment_delta = len(message.attachments) * delta
    snapshot.message_count = max(0, snapshot.message_count + delta)
    snapshot.attachment_count = max(0, snapshot.attachment_count + attachment_delta)
    folder.message_count = max(0, folder.message_count + delta)
    account = snapshot.account
    if account.active_snapshot_id == snapshot.id:
        account.message_count = snapshot.message_count


@app.post("/api/messages/{message_id}/trash", dependencies=[Depends(csrf_guard)])
def move_message_to_trash(message_id: int, db: Session = Depends(get_db)):
    message = db.get(Message, message_id)
    if not message or message.snapshot.status not in {"completed", "active"}:
        raise HTTPException(404, "Messaggio non disponibile")
    if message.is_deleted:
        return {"ok": True, "status": "trash"}
    message.is_deleted = True
    message.deleted_at = utcnow()
    _adjust_archive_counts(db, message, -1)
    db.execute(text("DELETE FROM message_fts WHERE message_id=:mid"), {"mid": message.id})
    db.add(ArchiveDeletionAudit(snapshot_id=message.snapshot_id, message_identifier=_audit_identifier(message), action="trash"))
    db.commit()
    return {"ok": True, "status": "trash"}


@app.post("/api/messages/{message_id}/restore", dependencies=[Depends(csrf_guard)])
def restore_message(message_id: int, db: Session = Depends(get_db)):
    message = db.get(Message, message_id)
    if not message or not message.is_deleted:
        raise HTTPException(404, "Messaggio non presente nel cestino")
    message.is_deleted = False
    message.deleted_at = None
    _adjust_archive_counts(db, message, 1)
    _fts_insert(db, message)
    db.add(ArchiveDeletionAudit(snapshot_id=message.snapshot_id, message_identifier=_audit_identifier(message), action="restore"))
    db.commit()
    return {"ok": True, "status": "restored"}


@app.delete("/api/messages/{message_id}/permanent", dependencies=[Depends(csrf_guard)])
def permanently_delete_message(message_id: int, db: Session = Depends(get_db)):
    message = db.get(Message, message_id)
    if not message or not message.is_deleted:
        raise HTTPException(409, "Sposta prima il messaggio nel cestino locale")
    snapshot, account = message.snapshot, message.snapshot.account
    paths: list[Path] = []
    raw_shared = db.scalar(select(func.count(Message.id)).where(
        Message.snapshot_id == snapshot.id, Message.raw_relpath == message.raw_relpath, Message.id != message.id
    )) or 0
    if not raw_shared:
        paths.append(_file_for_message(db, message, message.raw_relpath))
    for attachment in message.attachments:
        shared = db.scalar(select(func.count(Attachment.id)).join(Message).where(
            Message.snapshot_id == snapshot.id, Attachment.relpath == attachment.relpath,
            Attachment.message_id != message.id,
        )) or 0
        if not shared:
            paths.append(_file_for_message(db, message, attachment.relpath))
    db.add(ArchiveDeletionAudit(snapshot_id=snapshot.id, message_identifier=_audit_identifier(message), action="permanent"))
    db.delete(message)
    db.commit()
    for path in set(paths):
        path.unlink(missing_ok=True)
    root = snapshot_root(account.archive_uuid, snapshot.snapshot_uuid)
    snapshot.archive_size = sum(item.stat().st_size for item in root.rglob("*") if item.is_file())
    if account.active_snapshot_id == snapshot.id:
        account.archive_size = snapshot.archive_size
    db.commit()
    return {"ok": True, "status": "deleted"}


def _file_for_message(db: Session, message: Message, relpath: str) -> Path:
    snapshot = db.get(Snapshot, message.snapshot_id)
    account = db.get(Account, snapshot.account_id) if snapshot else None
    if not snapshot or not account or snapshot.status not in {"completed", "active"}:
        raise HTTPException(404, "File non disponibile")
    path = safe_resolve(snapshot_root(account.archive_uuid, snapshot.snapshot_uuid), relpath)
    if not path.is_file():
        raise HTTPException(404, "File non trovato")
    return path


def _attachment_category(attachment: Attachment) -> str:
    mime = (attachment.content_type or "").lower()
    extension = Path(attachment.filename or "").suffix.lower()
    if mime.startswith("image/") or extension in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"}:
        return "images"
    if mime == "application/pdf" or extension == ".pdf":
        return "pdf"
    if extension in {".doc", ".docx", ".odt", ".rtf", ".txt", ".md"} or "word" in mime:
        return "documents"
    if extension in {".xls", ".xlsx", ".ods", ".csv"} or any(value in mime for value in ("excel", "spreadsheet", "csv")):
        return "spreadsheets"
    if extension in {".ppt", ".pptx", ".odp"} or any(value in mime for value in ("powerpoint", "presentation")):
        return "presentations"
    if extension in {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2"} or any(value in mime for value in ("zip", "compressed", "archive")):
        return "archives"
    return "other"


def _category_predicate(category: str):
    mime = func.lower(Attachment.content_type)
    filename = func.lower(Attachment.filename)
    image = or_(mime.like("image/%"), *[filename.like(f"%{ext}") for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg")])
    pdf = or_(mime == "application/pdf", filename.like("%.pdf"))
    documents = or_(mime.like("%word%"), *[filename.like(f"%{ext}") for ext in (".doc", ".docx", ".odt", ".rtf", ".txt", ".md")])
    sheets = or_(mime.like("%excel%"), mime.like("%spreadsheet%"), mime.like("%csv%"), *[filename.like(f"%{ext}") for ext in (".xls", ".xlsx", ".ods", ".csv")])
    presentations = or_(mime.like("%powerpoint%"), mime.like("%presentation%"), *[filename.like(f"%{ext}") for ext in (".ppt", ".pptx", ".odp")])
    archives = or_(mime.like("%zip%"), mime.like("%compressed%"), mime.like("%archive%"), *[filename.like(f"%{ext}") for ext in (".zip", ".rar", ".7z", ".tar", ".gz", ".bz2")])
    mapping = {"images": image, "pdf": pdf, "documents": documents, "spreadsheets": sheets,
               "presentations": presentations, "archives": archives}
    if category == "other":
        return ~or_(image, pdf, documents, sheets, presentations, archives)
    return mapping.get(category)


@app.get("/api/accounts/{account_id}/attachments", dependencies=[Depends(current_user)])
def list_attachments(
    account_id: int,
    snapshot_id: int | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(60, ge=12, le=100),
    q: str | None = Query(None, max_length=300),
    category: Literal["all", "images", "pdf", "documents", "spreadsheets", "presentations", "archives", "other"] = "all",
    sender: str | None = Query(None, max_length=320),
    folder_id: int | None = None,
    db: Session = Depends(get_db),
):
    _account, snapshot = _active_snapshot(db, account_id, snapshot_id)
    query = select(Attachment).join(Message).where(
        Message.snapshot_id == snapshot.id,
        Message.is_deleted.is_(False),
        Attachment.is_inline.is_(False),
    )
    if q:
        query = query.where(Attachment.filename.ilike(f"%{q}%"))
    if sender:
        query = query.where(Message.sender.ilike(f"%{sender}%"))
    if folder_id:
        query = query.where(Message.folder_id == folder_id)
    predicate = _category_predicate(category)
    if predicate is not None:
        query = query.where(predicate)
    total = db.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
    rows = db.scalars(query.join(Folder, Message.folder_id == Folder.id).order_by(
        Message.date_utc.desc().nullslast(), Attachment.id.desc()
    ).offset((page - 1) * page_size).limit(page_size)).all()
    return {"page": page, "page_size": page_size, "total": total, "items": [{
        "id": item.id, "filename": item.filename, "content_type": item.content_type,
        "extension": Path(item.filename).suffix.lower().lstrip("."), "size": item.size,
        "category": _attachment_category(item), "date": item.message.date_utc or item.message.internal_date,
        "sender": item.message.sender, "subject": item.message.subject or "(senza oggetto)",
        "folder": item.message.folder.name, "message_id": item.message_id,
        "open_url": f"/api/attachments/{item.id}?inline=1", "download_url": f"/api/attachments/{item.id}",
        "text_preview_url": f"/api/attachments/{item.id}/text-preview",
    } for item in rows]}


TEXT_PREVIEW_EXTENSIONS = {".txt", ".csv", ".json", ".xml", ".log", ".md", ".yaml", ".yml"}


@app.get("/api/attachments/{attachment_id}/text-preview", response_class=PlainTextResponse, dependencies=[Depends(current_user)])
def attachment_text_preview(attachment_id: int, db: Session = Depends(get_db)):
    attachment = db.get(Attachment, attachment_id)
    if not attachment or attachment.message.is_deleted:
        raise HTTPException(404, "Allegato non disponibile")
    extension = Path(attachment.filename).suffix.lower()
    if not (attachment.content_type.startswith("text/") or extension in TEXT_PREVIEW_EXTENSIONS or attachment.content_type in {"application/json", "application/xml"}):
        raise HTTPException(415, "Anteprima testuale non supportata")
    if attachment.size > 2 * 1024 * 1024:
        raise HTTPException(413, "File troppo grande per l'anteprima; usa Download")
    payload = _file_for_message(db, attachment.message, attachment.relpath).read_bytes()
    if b"\x00" in payload[:4096]:
        raise HTTPException(415, "Il file non è testo leggibile")
    return PlainTextResponse(payload.decode("utf-8", "replace"), headers={"Content-Disposition": "inline"})


@app.get("/api/attachments/{attachment_id}", dependencies=[Depends(current_user)])
def download_attachment(attachment_id: int, inline: bool = False, db: Session = Depends(get_db)):
    attachment = db.get(Attachment, attachment_id)
    if not attachment:
        raise HTTPException(404, "Allegato non trovato")
    if attachment.message.is_deleted and not inline:
        raise HTTPException(404, "Allegato nel cestino locale")
    path = _file_for_message(db, attachment.message, attachment.relpath)
    mime = (attachment.content_type or mimetypes.guess_type(attachment.filename)[0] or "application/octet-stream").lower()
    extension = Path(attachment.filename).suffix.lower()
    safe_inline = (
        mime.startswith(("image/", "audio/", "video/")) or mime == "application/pdf" or
        mime.startswith("text/") or extension in TEXT_PREVIEW_EXTENSIONS
    ) and mime not in {"text/html", "application/xhtml+xml"}
    return FileResponse(path, media_type=mime if (not inline or safe_inline) else "application/octet-stream",
                        filename=attachment.filename, content_disposition_type="inline" if inline and safe_inline else "attachment")


@app.get("/api/messages/{message_id}/raw", dependencies=[Depends(current_user)])
def download_raw(message_id: int, db: Session = Depends(get_db)):
    message = db.get(Message, message_id)
    if not message:
        raise HTTPException(404, "Messaggio non trovato")
    path = _file_for_message(db, message, message.raw_relpath)
    filename = re.sub(r"[^\w.-]+", "_", (message.subject or "message")[:100]) + ".eml"
    return FileResponse(path, media_type="message/rfc822", filename=filename)


@app.get("/api/accounts/{account_id}/stats", dependencies=[Depends(current_user)])
def account_stats(account_id: int, snapshot_id: int | None = None, db: Session = Depends(get_db)):
    _account, snapshot = _active_snapshot(db, account_id, snapshot_id)
    attachments = db.scalar(select(func.count(Attachment.id)).join(Message).where(
        Message.snapshot_id == snapshot.id, Message.is_deleted.is_(False)
    )) or 0
    deleted = db.scalar(select(func.count(Message.id)).where(
        Message.snapshot_id == snapshot.id, Message.is_deleted.is_(True)
    )) or 0
    date_min, date_max = db.execute(select(func.min(Message.date_utc), func.max(Message.date_utc)).where(
        Message.snapshot_id == snapshot.id, Message.is_deleted.is_(False)
    )).one()
    return {"messages": snapshot.message_count, "folders": db.scalar(select(func.count(Folder.id)).where(Folder.snapshot_id == snapshot.id)) or 0,
            "attachments": attachments, "deleted": deleted, "archive_size": snapshot_disk_size(_account, snapshot), "oldest": date_min, "newest": date_max}


@app.exception_handler(ArchiveError)
async def archive_error_handler(_request: Request, exc: ArchiveError):
    return JSONResponse({"detail": str(exc)}, status_code=400)
