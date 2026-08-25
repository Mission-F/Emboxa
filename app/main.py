from __future__ import annotations

import json
import hashlib
import logging
import mimetypes
import re
import secrets
import shutil
import tempfile
import smtplib
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote
from urllib.request import Request as URLRequest, urlopen

import bleach
from bleach.css_sanitizer import CSSSanitizer
from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from .archive import ArchiveError, build_export, clear_account_archive, delete_account, import_archive
from .backup import backup_manager, next_backup_time, recover_interrupted_jobs, rotate_versions, snapshot_root
from .config import (
    ADMIN_EMAIL, ADMIN_PASSWORD, COOKIE_SECURE, DATA_DIR, EXPORTS_DIR, EXPORT_TTL_HOURS, IMPORTS_DIR, IMPORT_MAX_BYTES,
    GITHUB_REPOSITORY_URL, GOOGLE_ANALYTICS_ID, LEGAL_ADDRESS, LEGAL_CONTACT_EMAIL, LEGAL_ENTITY_NAME, LEGAL_VAT_ID,
    LOG_LEVEL, PERMANENT_MAILBOX_LOCK_DAYS, PUBLIC_APP_URL, SMTP_FROM_EMAIL, SMTP_FROM_NAME,
    SMTP_HOST, SMTP_PASSWORD, SMTP_PORT, SMTP_SECURITY, SMTP_USERNAME, STANDARD_MAILBOX_LIMIT,
    STANDARD_RETENTION_DAYS, STANDARD_STORAGE_LIMIT_BYTES, TELEGRAM_BOT_TOKEN,
    TELEGRAM_BOT_USERNAME, ensure_data_dirs,
)
from .database import SessionLocal, get_db
from .imap_adapter import test_imap_connection
from .migrations import run_migrations
from .models import (
    Account, AdminAudit, AppSetting, ArchiveDeletionAudit, Attachment, BackupJob, Folder, Message,
    PermanentMailboxHistory, SecurityToken, Snapshot, TelegramLink, User, WebExport, utcnow,
)
from .scheduler import scheduler
from .settings_service import get_bool_setting, get_float_setting, get_int_setting, get_setting, save_setting
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
    scheduler.start()
    log.info("EMBOXA avviato")
    yield
    scheduler.stop()
    backup_manager.shutdown()
    log.info("EMBOXA arrestato")


app = FastAPI(title="EMBOXA Web", version="1.0.0", lifespan=lifespan, docs_url=None, redoc_url=None)
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


class PreferencesPayload(BaseModel):
    locale: Literal["auto", "it", "en"] | None = None
    tutorial_completed: bool | None = None


class AdminUserPayload(BaseModel):
    plan: Literal["STANDARD", "PLUS"] | None = None
    status: Literal["active", "suspended"] | None = None
    storage_limit_bytes: int | None = Field(default=None, ge=1)
    confirm_downgrade: bool = False


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
    public_domain: str = Field(default="https://emboxa.eu", min_length=1, max_length=500)
    support_email: str = Field(default="info@missionf.it", max_length=320)
    default_language: Literal["it", "en"] = "it"
    available_languages: str = Field(default="it,en", max_length=50)
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
    export_ttl_hours: int = Field(default=24, ge=1, le=8760)
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


class ConnectionPayload(BaseModel):
    imap_host: str = Field(min_length=1, max_length=255)
    imap_port: int = Field(ge=1, le=65535)
    security: Literal["ssl", "starttls", "plain"] = "ssl"
    imap_username: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


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


def _send_email(to: str, subject: str, text_body: str) -> None:
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
    _send_email(user.email, "Your EMBOXA verification code", f"Your verification code is {code}. It expires in 15 minutes.")


@app.get("/", response_class=HTMLResponse)
def public_home(request: Request):
    available = [item.strip() for item in get_setting("available_languages", "it,en").split(",") if item.strip() in {"it", "en"}]
    if not available:
        available = ["it", "en"]
    preferred = next((part.split(";")[0].strip().lower().split("-")[0]
                      for part in request.headers.get("accept-language", "").split(",")
                      if part.split(";")[0].strip().lower().split("-")[0] in available), None)
    locale = preferred or get_setting("default_language", "it")
    return RedirectResponse(f"/{locale if locale in available else available[0]}/", status_code=307)


PUBLIC_PAGES = {"features", "self-hosted", "privacy", "cookies", "legal", "terms"}


@app.get("/it/", response_class=HTMLResponse)
@app.get("/en/", response_class=HTMLResponse)
@app.get("/it/{page}", response_class=HTMLResponse)
@app.get("/en/{page}", response_class=HTMLResponse)
def localized_public(request: Request, page: str = "home"):
    locale = request.url.path.split("/")[1]
    if page != "home" and page not in PUBLIC_PAGES:
        raise HTTPException(404, "Page not found")
    public_url = get_setting("public_domain", PUBLIC_APP_URL).rstrip("/")
    analytics_id = get_setting("google_analytics_id", GOOGLE_ANALYTICS_ID) if get_bool_setting("analytics_enabled") else ""
    canonical = f"{public_url}/{locale}/" + ("" if page == "home" else page)
    return templates.TemplateResponse(request, "public.html", {
        "locale": locale, "page": page, "canonical": canonical, "public_url": public_url,
        "app_name": get_setting("public_app_name"), "analytics_id": analytics_id, "from_email": _contact_email(),
        "github_url": GITHUB_REPOSITORY_URL,
        "retention_days": get_int_setting("standard_retention_days", STANDARD_RETENTION_DAYS),
        "storage_limit_gb": round(get_int_setting("standard_storage_limit_bytes", STANDARD_STORAGE_LIMIT_BYTES) / 1024**3),
        "mailbox_limit": get_int_setting("standard_mailbox_limit", STANDARD_MAILBOX_LIMIT),
        "version_limit": get_int_setting("default_backup_retention_versions", 3),
        "permanent_limit": get_int_setting("permanent_mailbox_limit", 1),
        "legal_entity": LEGAL_ENTITY_NAME, "legal_address": LEGAL_ADDRESS, "legal_vat": LEGAL_VAT_ID,
        "legal_email": LEGAL_CONTACT_EMAIL,
    })


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    public_url = get_setting("public_domain", PUBLIC_APP_URL).rstrip("/")
    return f"User-agent: *\nAllow: /it/\nAllow: /en/\nDisallow: /app\nDisallow: /admin\nDisallow: /api\nSitemap: {public_url}/sitemap.xml\n"


@app.get("/sitemap.xml")
def sitemap():
    public_url = get_setting("public_domain", PUBLIC_APP_URL).rstrip("/")
    urls = []
    for page in ["", *sorted(PUBLIC_PAGES)]:
        for locale in ("it", "en"):
            path = f"/{locale}/" + page
            alternate = "en" if locale == "it" else "it"
            urls.append(f"<url><loc>{public_url}{path}</loc><xhtml:link rel='alternate' hreflang='{alternate}' href='{public_url}/{alternate}/{page}'/></url>")
    xml = "<?xml version='1.0' encoding='UTF-8'?><urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9' xmlns:xhtml='http://www.w3.org/1999/xhtml'>" + "".join(urls) + "</urlset>"
    return Response(xml, media_type="application/xml")


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse(request, "register.html", {"registration_enabled": get_bool_setting("registration_enabled")})


@app.post("/api/register")
def register(payload: RegisterPayload, db: Session = Depends(get_db)):
    if not get_bool_setting("registration_enabled", db=db):
        raise HTTPException(403, "Registration is currently disabled")
    email = str(payload.email).lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "An account already exists for this email")
    user = User(username=email, email=email, password_hash=hash_password(payload.password),
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
    return templates.TemplateResponse(request, "verify.html", {})


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
    request.session.clear(); request.session["user_id"] = user.id; request.session["csrf"] = make_csrf_token()
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
    return templates.TemplateResponse(request, "login.html", {})


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
    request.session.clear()
    request.session["user_id"] = user.id
    request.session["csrf"] = make_csrf_token()
    return {"ok": True}


@app.get("/reset-password", response_class=HTMLResponse)
def reset_page(request: Request):
    return templates.TemplateResponse(request, "reset.html", {})


@app.post("/api/password-reset/request")
def request_password_reset(payload: ResetRequestPayload, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == str(payload.email).lower()))
    if user:
        raw = secrets.token_urlsafe(36)
        db.add(SecurityToken(user_id=user.id, purpose="reset", token_hash=_token_hash(raw),
                             expires_at=utcnow() + timedelta(hours=1)))
        db.flush()
        public_url = get_setting("public_domain", PUBLIC_APP_URL, db).rstrip("/")
        _send_email(user.email, "Reset your EMBOXA password", f"Open {public_url}/reset-password?token={quote(raw)} within one hour.")
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
    return templates.TemplateResponse(request, "app.html", {"csrf_token": _csrf(request), "web_user": user, "admin_email": _contact_email()})


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
        "default_language": get_setting("default_language", "it", db),
        "available_languages": get_setting("available_languages", "it,en", db),
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
    return {"ok": True, "telegram": {"connected": telegram_configured, "username": telegram_username,
            "webhook_status": get_setting("telegram_webhook_status", "not_configured", db), "warning": telegram_warning}}


@app.post("/api/admin/smtp/test", dependencies=[Depends(csrf_guard)])
def admin_test_smtp(payload: SMTPTestPayload, admin: User = Depends(admin_user)):
    try:
        with _smtp_client():
            pass
        if payload.email:
            _send_email(str(payload.email), "EMBOXA SMTP test", "SMTP delivery from Emboxa Web is working.")
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
            "storage_total": db.scalar(select(func.coalesce(func.sum(Account.archive_size), 0))) or 0,
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
    text_value = f"EMBOXA\n\nPlan\n{user.plan}\n\nStorage\n{storage}\n\nMailboxes\n{mailbox_text}\n\nBackup\n{backup}"
    keyboard = {"inline_keyboard": [[{"text": "Mailboxes", "callback_data": "mailboxes"}, {"text": "Backup", "callback_data": "backup"}],
                                    [{"text": "Status", "callback_data": "status"}, {"text": "Storage", "callback_data": "storage"}],
                                    [{"text": "Refresh", "callback_data": "dashboard"}]]}
    return text_value, keyboard


def _telegram_render(db: Session, user: User, chat_id: str, message_id: str | None, view: str) -> dict:
    text_value, keyboard = _telegram_dashboard(db, user)
    accounts = db.scalars(select(Account).where(Account.owner_id == user.id).order_by(Account.display_name)).all()
    if view in {"mailboxes", "backup"}:
        text_value = "Mailboxes\n\n" + ("\n".join(f"• {item.display_name}" for item in accounts) or "No mailboxes")
        rows = [[{"text": f"Backup {item.display_name}", "callback_data": f"backup:{item.id}"}] for item in accounts] if view == "backup" else []
        keyboard = {"inline_keyboard": rows + [[{"text": "Back", "callback_data": "dashboard"}]]}
    elif view == "status":
        jobs = db.scalars(select(BackupJob).join(Account).where(Account.owner_id == user.id).order_by(BackupJob.id.desc()).limit(5)).all()
        text_value = "Backup status\n\n" + ("\n".join(f"{job.account.display_name}: {job.status} · {job.percent}% · ETA {job.eta_seconds or '—'}" for job in jobs) or "No backup history")
        keyboard = {"inline_keyboard": [[{"text": "Back", "callback_data": "dashboard"}]]}
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
        _telegram_call("sendMessage", {"chat_id": chat_id, "text": f"Welcome to EMBOXA\n\nYour Chat ID:\n{chat_id}\n\nAdd this ID in EMBOXA → Settings → Telegram.",
                                       "reply_markup": {"inline_keyboard": [[{"text": "Open dashboard", "callback_data": "dashboard"}]]}})
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
        backup_manager.start(account.id); data = "status"
    _telegram_render(db, user, chat_id, message_id, data)
    if callback.get("id"):
        _telegram_call("answerCallbackQuery", {"callback_query_id": callback["id"]})
    link.dashboard_message_id = message_id; db.commit()
    return {"ok": True}


def _account_json(account: Account, job: BackupJob | None = None) -> dict:
    return {
        "id": account.id,
        "display_name": account.display_name,
        "email": account.email,
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
        "archive_size": account.archive_size,
        "retention_versions": account.retention_versions,
        "is_permanent": account.is_permanent,
        "permanent_locked_until": account.permanent_locked_until,
        "has_archive": bool(account.active_snapshot_id),
        "job": _job_json(job) if job else None,
    }


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
    return int(db.scalar(select(func.coalesce(func.sum(Snapshot.archive_size), 0)).join(Account, Snapshot.account_id == Account.id).where(
        Account.owner_id == user_id, Snapshot.status.in_(["completed", "active"]))) or 0)


@app.get("/api/accounts", dependencies=[Depends(current_user)])
def list_accounts(user: User = Depends(current_user), db: Session = Depends(get_db)):
    accounts = db.scalars(select(Account).where(Account.owner_id == user.id).order_by(Account.display_name.collate("NOCASE"))).all()
    return [_account_json(account, _running_job(db, account.id)) for account in accounts]


@app.get("/api/web/usage")
def web_usage(user: User = Depends(current_user), db: Session = Depends(get_db)):
    used = _storage_used(db, user.id)
    count = db.scalar(select(func.count(Account.id)).where(Account.owner_id == user.id)) or 0
    return {"plan": user.plan, "storage_used": used, "storage_limit": None if user.plan == "PLUS" else user.storage_limit_bytes,
            "mailbox_count": count, "mailbox_limit": None if user.plan == "PLUS" else get_int_setting("standard_mailbox_limit", STANDARD_MAILBOX_LIMIT, db),
            "contact": _contact_email(), "over_quota": user.plan != "PLUS" and used >= user.storage_limit_bytes}


@app.post("/api/accounts/test", dependencies=[Depends(csrf_guard)])
def test_connection(payload: ConnectionPayload):
    try:
        return test_imap_connection(payload.imap_host, payload.imap_port, payload.security, payload.imap_username, payload.password)
    except Exception as exc:
        raise HTTPException(400, f"Connessione IMAP fallita: {exc}") from exc


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
        archive_uuid=str(__import__("uuid").uuid4()),
        display_name=payload.display_name.strip(),
        email=str(payload.email),
        imap_host=payload.imap_host.strip(),
        imap_port=payload.imap_port,
        security=payload.security,
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
    return _account_json(account)


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
    return _account_json(account, _running_job(db, account.id))


@app.post("/api/accounts/{account_id}/test", dependencies=[Depends(csrf_guard)])
def test_saved_connection(account_id: int, db: Session = Depends(get_db)):
    account = _account_or_404(db, account_id)
    if not account.imap_enabled or not account.encrypted_password:
        raise HTTPException(409, "Configura prima l'accesso IMAP")
    try:
        return test_imap_connection(account.imap_host or "", account.imap_port or 993, account.security,
                                    account.imap_username or account.email, decrypt_secret(account.encrypted_password))
    except Exception as exc:
        raise HTTPException(400, f"Connessione IMAP fallita: {exc}") from exc


@app.post("/api/accounts/{account_id}/backup", dependencies=[Depends(csrf_guard)])
def start_backup(account_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    account = _account_or_404(db, account_id)
    if not account.imap_enabled or not account.encrypted_password:
        raise HTTPException(409, "Account IMAP non configurato")
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
        "message_count": row.message_count, "archive_size": row.archive_size,
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
        return _account_json(target, _running_job(db, target.id))
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
    return _account_json(target, _running_job(db, target.id))


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


@app.get("/api/accounts/{account_id}/export", dependencies=[Depends(current_user)])
def export_account(account_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    account = _account_or_404(db, account_id)
    temporary = db.scalar(select(func.coalesce(func.sum(WebExport.size), 0)).where(
        WebExport.owner_id == user.id, WebExport.expires_at > utcnow())) or 0
    if user.plan != "PLUS" and _storage_used(db, user.id) + temporary + account.archive_size > user.storage_limit_bytes:
        raise HTTPException(409, f"Storage limit reached. Contact the administrator at {_contact_email()}")
    try:
        path, filename = build_export(account_id)
    except ArchiveError as exc:
        raise HTTPException(400, str(exc)) from exc
    export_max = get_int_setting("export_max_bytes", 10 * 1024**3, db)
    if path.stat().st_size > export_max:
        path.unlink(missing_ok=True)
        shutil.rmtree(path.parent, ignore_errors=True)
        raise HTTPException(413, "Export exceeds the configured maximum size")
    public_id = str(__import__("uuid").uuid4())
    destination_dir = EXPORTS_DIR / f"user-{user.id}"; destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"{public_id}.mailvault"
    shutil.move(str(path), destination); shutil.rmtree(path.parent, ignore_errors=True)
    item = WebExport(public_id=public_id, owner_id=user.id, account_id=account_id, filename=filename,
                     relpath=str(destination.relative_to(EXPORTS_DIR)), size=destination.stat().st_size,
                     expires_at=utcnow() + timedelta(hours=get_int_setting("export_ttl_hours", EXPORT_TTL_HOURS, db)))
    db.add(item); db.commit()
    return RedirectResponse(f"/api/exports/{public_id}/download", status_code=303)


@app.get("/api/exports/{public_id}/download")
def download_export(public_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    item = db.scalar(select(WebExport).where(WebExport.public_id == public_id, WebExport.owner_id == user.id))
    if not item or item.expires_at < utcnow():
        raise HTTPException(404, "Export expired or unavailable")
    path = safe_resolve(EXPORTS_DIR, item.relpath)
    if not path.is_file():
        raise HTTPException(404, "Export unavailable")
    return FileResponse(path, filename=item.filename, media_type="application/vnd.mailvault+zip")


@app.post("/api/import", dependencies=[Depends(csrf_guard)])
async def upload_import(file: Annotated[UploadFile, File()], user: User = Depends(current_user), db: Session = Depends(get_db)):
    if user.plan != "PLUS" and (db.scalar(select(func.count(Account.id)).where(Account.owner_id == user.id)) or 0) >= get_int_setting("standard_mailbox_limit", STANDARD_MAILBOX_LIMIT, db):
        raise HTTPException(409, "Mailbox limit reached")
    if not (file.filename or "").lower().endswith(".mailvault"):
        raise HTTPException(400, "Seleziona un file .mailvault")
    IMPORTS_DIR.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix="upload-", suffix=".mailvault", dir=IMPORTS_DIR)
    import os
    os.close(descriptor)
    temp_path = Path(temp_name)
    size = 0
    try:
        with temp_path.open("wb") as target:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > IMPORT_MAX_BYTES:
                    raise HTTPException(413, "Il file supera il limite di import configurato")
                target.write(chunk)
        try:
            account_id = await run_in_threadpool(import_archive, temp_path, user.id)
        except ArchiveError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True, "account_id": account_id}
    finally:
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
            "attachments": attachments, "deleted": deleted, "archive_size": snapshot.archive_size, "oldest": date_min, "newest": date_max}


@app.exception_handler(ArchiveError)
async def archive_error_handler(_request: Request, exc: ArchiveError):
    return JSONResponse({"detail": str(exc)}, status_code=400)
