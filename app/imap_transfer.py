from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select

from .backup import snapshot_root
from .database import SessionLocal
from .imap_adapter import StandardIMAPAdapter
from .models import Account, Folder, IMAPTransferJob, Message, utcnow
from .security import decrypt_secret, safe_resolve
from .settings_service import get_int_setting

log = logging.getLogger("emboxa.imap_transfer")


class TransferCancelled(Exception):
    pass


def _clean_folder(name: str) -> str:
    value = " ".join(str(name).replace("\x00", "").split()).strip()
    if not value or len(value) > 500:
        raise RuntimeError("Invalid destination folder")
    return value


def _destination(job: IMAPTransferJob, db) -> tuple[StandardIMAPAdapter, str]:
    if job.destination_account_id:
        account = db.get(Account, job.destination_account_id)
        if not account or account.owner_id != job.owner_id or not account.imap_enabled:
            raise RuntimeError("Destination mailbox is no longer available")
        password = decrypt_secret(account.encrypted_password)
        return StandardIMAPAdapter(
            account.imap_host or "", int(account.imap_port or 993), account.security,
            account.imap_username or account.email, password,
        ), account.display_name
    if not all((job.destination_host, job.destination_port, job.destination_security, job.destination_username,
                job.encrypted_password)):
        raise RuntimeError("Temporary destination credentials are unavailable")
    return StandardIMAPAdapter(
        job.destination_host, int(job.destination_port), job.destination_security,
        job.destination_username, decrypt_secret(job.encrypted_password),
    ), job.destination_label


def _cancel_if_requested(db, job: IMAPTransferJob) -> None:
    db.refresh(job, ["cancel_requested"])
    if job.cancel_requested:
        job.status = "cancelling"
        db.commit()
        raise TransferCancelled()


def run_transfer(job_id: int) -> None:
    db = SessionLocal()
    adapter: StandardIMAPAdapter | None = None
    job: IMAPTransferJob | None = None
    try:
        job = db.get(IMAPTransferJob, job_id)
        if not job or job.status != "queued":
            return
        if job.cancel_requested:
            raise TransferCancelled()
        account = db.get(Account, job.account_id)
        snapshot = job.snapshot
        if not account or account.owner_id != job.owner_id or snapshot.account_id != account.id:
            raise RuntimeError("Source archive is unavailable")
        if snapshot.status not in {"completed", "active"}:
            raise RuntimeError("Source snapshot is not ready")

        adapter, destination_label = _destination(job, db)
        adapter.connect()
        job.status = "running"
        job.started_at = job.started_at or utcnow()
        job.error = None
        job.current_folder = None
        job.processed_messages = 0
        job.skipped_messages = 0
        job.failed_messages = 0
        job.percent = 0
        job.throughput = 0
        job.eta_seconds = None
        job.total_messages = db.query(Message).filter(
            Message.snapshot_id == snapshot.id, Message.is_deleted.is_(False)
        ).count()
        db.commit()

        mappings = json.loads(job.mappings_json or "{}")
        folders = db.scalars(select(Folder).where(Folder.snapshot_id == snapshot.id).order_by(Folder.id)).all()
        started = time.monotonic()
        appended = 0
        for folder in folders:
            _cancel_if_requested(db, job)
            target = _clean_folder(job.single_folder or mappings.get(folder.name) or folder.name)
            adapter.ensure_folder(target)
            adapter.select_write_folder(target)
            job.current_folder = folder.name
            db.commit()
            messages = db.scalars(select(Message).where(
                Message.snapshot_id == snapshot.id,
                Message.folder_id == folder.id,
                Message.is_deleted.is_(False),
            ).order_by(Message.id)).all()
            for message in messages:
                _cancel_if_requested(db, job)
                if job.skip_duplicates and message.message_id and adapter.has_message_id(message.message_id):
                    job.skipped_messages += 1
                else:
                    raw_path = safe_resolve(
                        snapshot_root(account.archive_uuid, snapshot.snapshot_uuid), message.raw_relpath
                    )
                    if not raw_path.is_file():
                        job.failed_messages += 1
                    else:
                        flags = json.loads(message.flags_json or "[]")
                        adapter.append_message(target, raw_path.read_bytes(), flags, message.internal_date or message.date_utc)
                appended += 1
                job.processed_messages = appended
                job.percent = min(100, round(appended * 100 / max(1, job.total_messages)))
                elapsed = max(.001, time.monotonic() - started)
                job.throughput = appended / elapsed
                remaining = max(0, job.total_messages - appended)
                job.eta_seconds = round(remaining / job.throughput) if job.throughput else None
                if appended % 10 == 0:
                    db.commit()

        job.status = "completed"
        job.percent = 100
        job.eta_seconds = 0
        job.finished_at = utcnow()
        job.encrypted_password = None
        db.commit()
        log.info("IMAP transfer %s completed to %s", job.id, destination_label)
        from .telegram_service import notify_transfer
        notify_transfer(job.owner_id, destination_label, "completed", job.processed_messages, job.skipped_messages)
    except TransferCancelled:
        if job:
            job.status = "cancelled"
            job.finished_at = utcnow()
            job.encrypted_password = None
            if not job.started_at:
                job.quota_period = None
            db.commit()
    except Exception as exc:
        log.exception("IMAP transfer %s failed", job_id)
        if job:
            job.status = "failed"
            job.error = str(exc)[:2000]
            job.finished_at = utcnow()
            job.encrypted_password = None
            db.commit()
            from .telegram_service import notify_transfer
            notify_transfer(job.owner_id, job.destination_label, "failed", job.processed_messages, job.skipped_messages)
    finally:
        if adapter:
            adapter.logout()
        db.close()


class IMAPTransferManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pool: ThreadPoolExecutor | None = None
        self._submitted: set[int] = set()

    def _executor(self) -> ThreadPoolExecutor:
        if self._pool is None:
            workers = max(1, min(8, get_int_setting("imap_transfer_concurrency", 2)))
            self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="imap-transfer")
        return self._pool

    def submit(self, job_id: int) -> None:
        with self._lock:
            if job_id in self._submitted:
                return
            self._submitted.add(job_id)
            future = self._executor().submit(run_transfer, job_id)
            future.add_done_callback(lambda _future: self._done(job_id))

    def _done(self, job_id: int) -> None:
        with self._lock:
            self._submitted.discard(job_id)
        self.dispatch_queued()

    def dispatch_queued(self) -> None:
        with SessionLocal() as db:
            queued = db.scalars(select(IMAPTransferJob.id).where(
                IMAPTransferJob.status == "queued"
            ).order_by(IMAPTransferJob.created_at, IMAPTransferJob.id)).all()
        for job_id in queued:
            self.submit(job_id)

    def refresh(self) -> None:
        """Apply a new concurrency value when no transfer is currently executing."""
        old_pool = None
        with self._lock:
            if not self._submitted and self._pool is not None:
                old_pool, self._pool = self._pool, None
        if old_pool:
            old_pool.shutdown(wait=False, cancel_futures=False)

    def shutdown(self) -> None:
        if self._pool:
            self._pool.shutdown(wait=False, cancel_futures=False)


def recover_interrupted_transfers() -> None:
    with SessionLocal() as db:
        running = db.scalars(select(IMAPTransferJob).where(
            IMAPTransferJob.status.in_(["running", "cancelling"])
        )).all()
        for job in running:
            if job.status == "cancelling" or job.cancel_requested:
                job.status = "cancelled"
                job.finished_at = utcnow()
                job.encrypted_password = None
            else:
                job.status = "queued"
        db.commit()
    transfer_manager.dispatch_queued()


transfer_manager = IMAPTransferManager()
