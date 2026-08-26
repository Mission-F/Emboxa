from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(120), unique=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    locale: Mapped[str] = mapped_column(String(10), default="auto")
    tutorial_completed: Mapped[bool] = mapped_column(Boolean, default=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    role: Mapped[str] = mapped_column(String(20), default="user", index=True)
    plan: Mapped[str] = mapped_column(String(20), default="STANDARD", index=True)
    storage_limit_bytes: Mapped[int] = mapped_column(BigInteger, default=15 * 1024**3)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    archive_uuid: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(320), index=True)
    imap_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    imap_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    security: Mapped[str] = mapped_column(String(20), default="ssl")
    auth_provider: Mapped[str] = mapped_column(String(20), default="imap", index=True)
    imap_username: Mapped[str | None] = mapped_column(String(320), nullable=True)
    encrypted_password: Mapped[str | None] = mapped_column(Text, nullable=True)
    root_folder: Mapped[str | None] = mapped_column(String(500), nullable=True)
    imap_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    schedule_mode: Mapped[str] = mapped_column(String(20), default="disabled")
    schedule_interval_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    next_backup_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_backup_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_backup_status: Mapped[str] = mapped_column(String(30), default="never")
    last_backup_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    active_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("snapshots.id", ondelete="SET NULL", use_alter=True), nullable=True
    )
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    archive_size: Mapped[int] = mapped_column(Integer, default=0)
    retention_versions: Mapped[int] = mapped_column(Integer, default=3)
    mailbox_identity: Mapped[str] = mapped_column(String(64), index=True)
    is_permanent: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    permanent_since: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    permanent_locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    snapshots: Mapped[list["Snapshot"]] = relationship(
        back_populates="account", foreign_keys="Snapshot.account_id", cascade="all, delete-orphan"
    )
    jobs: Mapped[list["BackupJob"]] = relationship(back_populates="account", cascade="all, delete-orphan")
    transfer_jobs: Mapped[list["IMAPTransferJob"]] = relationship(
        back_populates="account", foreign_keys="IMAPTransferJob.account_id", cascade="all, delete-orphan"
    )


class Snapshot(Base):
    __tablename__ = "snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    snapshot_uuid: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="staging", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    archive_size: Mapped[int] = mapped_column(Integer, default=0)
    attachment_count: Mapped[int] = mapped_column(Integer, default=0)
    protected: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    protection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    comparison_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    folder_counts_json: Mapped[str] = mapped_column(Text, default="{}")

    account: Mapped[Account] = relationship(back_populates="snapshots", foreign_keys=[account_id])
    folders: Mapped[list["Folder"]] = relationship(back_populates="snapshot", cascade="all, delete-orphan")
    messages: Mapped[list["Message"]] = relationship(back_populates="snapshot", cascade="all, delete-orphan")


class Folder(Base):
    __tablename__ = "folders"
    __table_args__ = (UniqueConstraint("snapshot_id", "name", name="uq_folder_snapshot_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("snapshots.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(1000))
    delimiter: Mapped[str | None] = mapped_column(String(10), nullable=True)
    flags_json: Mapped[str] = mapped_column(Text, default="[]")
    uidvalidity: Mapped[str | None] = mapped_column(String(100), nullable=True)
    message_count: Mapped[int] = mapped_column(Integer, default=0)

    snapshot: Mapped[Snapshot] = relationship(back_populates="folders")
    messages: Mapped[list["Message"]] = relationship(back_populates="folder", cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("folder_id", "imap_uid", name="uq_message_folder_uid"),
        Index("ix_messages_snapshot_date", "snapshot_id", "date_utc"),
        Index("ix_messages_thread", "snapshot_id", "thread_key"),
        Index("ix_messages_filters", "snapshot_id", "is_read", "is_starred", "has_attachments"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("snapshots.id", ondelete="CASCADE"), index=True)
    folder_id: Mapped[int] = mapped_column(ForeignKey("folders.id", ondelete="CASCADE"), index=True)
    imap_uid: Mapped[str] = mapped_column(String(100))
    message_id: Mapped[str | None] = mapped_column(String(1000), nullable=True, index=True)
    in_reply_to: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    references_json: Mapped[str] = mapped_column(Text, default="[]")
    thread_key: Mapped[str] = mapped_column(String(1000), index=True)
    subject: Mapped[str] = mapped_column(Text, default="")
    sender: Mapped[str] = mapped_column(Text, default="")
    recipients_to: Mapped[str] = mapped_column(Text, default="")
    recipients_cc: Mapped[str] = mapped_column(Text, default="")
    recipients_bcc: Mapped[str] = mapped_column(Text, default="")
    reply_to: Mapped[str] = mapped_column(Text, default="")
    date_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    internal_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    headers_json: Mapped[str] = mapped_column(Text, default="[]")
    text_body: Mapped[str] = mapped_column(Text, default="")
    html_body: Mapped[str] = mapped_column(Text, default="")
    mime_json: Mapped[str] = mapped_column(Text, default="{}")
    flags_json: Mapped[str] = mapped_column(Text, default="[]")
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    is_starred: Mapped[bool] = mapped_column(Boolean, default=False)
    is_answered: Mapped[bool] = mapped_column(Boolean, default=False)
    has_attachments: Mapped[bool] = mapped_column(Boolean, default=False)
    size: Mapped[int] = mapped_column(Integer, default=0)
    raw_sha256: Mapped[str] = mapped_column(String(64))
    raw_relpath: Mapped[str] = mapped_column(String(500))
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    snapshot: Mapped[Snapshot] = relationship(back_populates="messages")
    folder: Mapped[Folder] = relationship(back_populates="messages")
    attachments: Mapped[list["Attachment"]] = relationship(back_populates="message", cascade="all, delete-orphan")


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(500), default="attachment")
    content_type: Mapped[str] = mapped_column(String(255), default="application/octet-stream")
    size: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64))
    relpath: Mapped[str] = mapped_column(String(500))
    content_id: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    is_inline: Mapped[bool] = mapped_column(Boolean, default=False)

    message: Mapped[Message] = relationship(back_populates="attachments")


class BackupJob(Base):
    __tablename__ = "backup_jobs"
    __table_args__ = (Index("ix_jobs_account_status", "account_id", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="queued")
    current_folder: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    processed_messages: Mapped[int] = mapped_column(Integer, default=0)
    total_messages: Mapped[int] = mapped_column(Integer, default=0)
    attachment_count: Mapped[int] = mapped_column(Integer, default=0)
    percent: Mapped[int] = mapped_column(Integer, default=0)
    throughput: Mapped[float] = mapped_column(Float, default=0.0)
    eta_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    account: Mapped[Account] = relationship(back_populates="jobs")


class IMAPTransferJob(Base):
    """Persistent RFC822 restore/transfer queue item.

    Temporary destination credentials are encrypted and erased as soon as the job
    reaches a terminal state. Existing destinations reference another owned account.
    """

    __tablename__ = "imap_transfer_jobs"
    __table_args__ = (
        Index("ix_imap_transfer_owner_status", "owner_id", "status"),
        Index("ix_imap_transfer_account_status", "account_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("snapshots.id", ondelete="CASCADE"), index=True)
    destination_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    destination_label: Mapped[str] = mapped_column(String(200), default="Destination mailbox")
    destination_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    destination_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    destination_security: Mapped[str | None] = mapped_column(String(20), nullable=True)
    destination_username: Mapped[str | None] = mapped_column(String(320), nullable=True)
    encrypted_password: Mapped[str | None] = mapped_column(Text, nullable=True)
    mode: Mapped[str] = mapped_column(String(20), default="preserve")
    single_folder: Mapped[str | None] = mapped_column(String(500), nullable=True)
    mappings_json: Mapped[str] = mapped_column(Text, default="{}")
    skip_duplicates: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    current_folder: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    processed_messages: Mapped[int] = mapped_column(Integer, default=0)
    total_messages: Mapped[int] = mapped_column(Integer, default=0)
    skipped_messages: Mapped[int] = mapped_column(Integer, default=0)
    failed_messages: Mapped[int] = mapped_column(Integer, default=0)
    percent: Mapped[int] = mapped_column(Integer, default=0)
    throughput: Mapped[float] = mapped_column(Float, default=0.0)
    eta_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    quota_period: Mapped[str | None] = mapped_column(String(7), nullable=True, index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    account: Mapped[Account] = relationship(back_populates="transfer_jobs", foreign_keys=[account_id])
    snapshot: Mapped[Snapshot] = relationship(foreign_keys=[snapshot_id])
    destination_account: Mapped[Account | None] = relationship(foreign_keys=[destination_account_id])


class ArchiveDeletionAudit(Base):
    __tablename__ = "archive_deletion_audit"

    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("snapshots.id", ondelete="SET NULL"), nullable=True, index=True)
    message_identifier: Mapped[str] = mapped_column(String(1000))
    action: Mapped[str] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SecurityToken(Base):
    __tablename__ = "security_tokens"
    __table_args__ = (Index("ix_security_token_lookup", "purpose", "token_hash"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    purpose: Mapped[str] = mapped_column(String(30), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PasskeyCredential(Base):
    __tablename__ = "passkey_credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    credential_id: Mapped[str] = mapped_column(String(1024), unique=True, index=True)
    public_key: Mapped[str] = mapped_column(Text)
    sign_count: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[str] = mapped_column(String(200), default="Passkey")
    transports_json: Mapped[str] = mapped_column(Text, default="[]")
    device_type: Mapped[str] = mapped_column(String(40), default="")
    backed_up: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class WebExport(Base):
    __tablename__ = "web_exports"
    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(500))
    relpath: Mapped[str] = mapped_column(String(500))
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TelegramLink(Base):
    __tablename__ = "telegram_links"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True)
    chat_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    dashboard_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notify_completed: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_failed: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_expiring: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_storage: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    encrypted: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class AdminAudit(Base):
    __tablename__ = "admin_audit"
    id: Mapped[int] = mapped_column(primary_key=True)
    admin_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    target_type: Mapped[str] = mapped_column(String(50))
    target_id: Mapped[str] = mapped_column(String(100))
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PermanentMailboxHistory(Base):
    __tablename__ = "permanent_mailbox_history"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    mailbox_identity: Mapped[str] = mapped_column(String(64), index=True)
    designated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    locked_until: Mapped[datetime] = mapped_column(DateTime, index=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class NotificationDelivery(Base):
    __tablename__ = "notification_deliveries"
    __table_args__ = (UniqueConstraint("user_id", "event_key", name="uq_notification_user_event"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    event_key: Mapped[str] = mapped_column(String(200), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
