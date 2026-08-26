from __future__ import annotations

import json
import logging
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select, text

from .config import ARCHIVES_DIR, BACKUP_RETRIES, IMAP_FETCH_BATCH
from .database import SessionLocal
from .graph_adapter import MicrosoftGraphAdapter
from .imap_adapter import StandardIMAPAdapter
from .mail_parser import parse_and_store
from .models import Account, Attachment, BackupJob, Folder, Message, Snapshot, User, utcnow
from .security import decrypt_secret, encrypt_secret
from .settings_service import get_float_setting, get_int_setting

log = logging.getLogger("emboxa.backup")


class BackupCancelled(Exception):
    pass


def next_backup_time(account: Account, from_time=None):
    base = from_time or utcnow()
    if account.schedule_mode == "daily":
        return base + timedelta(days=1)
    if account.schedule_mode == "weekly":
        return base + timedelta(days=7)
    if account.schedule_mode == "interval" and account.schedule_interval_hours:
        return base + timedelta(hours=max(1, account.schedule_interval_hours))
    return None


def snapshot_root(account_uuid: str, snapshot_uuid: str) -> Path:
    return ARCHIVES_DIR / account_uuid / "snapshots" / snapshot_uuid


def _directory_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _snapshot_comparison(previous: Snapshot, current: Snapshot) -> dict:
    old_folders = json.loads(previous.folder_counts_json or "{}")
    new_folders = json.loads(current.folder_counts_json or "{}")
    folder_changes = {
        name: int(new_folders.get(name, 0)) - int(old_folders.get(name, 0))
        for name in sorted(set(old_folders) | set(new_folders))
        if int(new_folders.get(name, 0)) != int(old_folders.get(name, 0))
    }
    removed = max(0, previous.message_count - current.message_count)
    ratio = removed / previous.message_count if previous.message_count else 0
    missing_folders = [name for name in old_folders if name not in new_folders]
    threshold = min(.9, max(.01, get_float_setting("backup_anomaly_threshold", .2)))
    return {
        "suspicious": bool(previous.message_count and ratio > threshold),
        "threshold": threshold,
        "previous_snapshot_id": previous.id,
        "previous_messages": previous.message_count,
        "new_messages": current.message_count,
        "messages_removed": removed,
        "messages_added": max(0, current.message_count - previous.message_count),
        "folders_removed": missing_folders,
        "folder_changes": folder_changes,
        "attachments_difference": current.attachment_count - previous.attachment_count,
        "size_difference": current.archive_size - previous.archive_size,
    }


def rotate_versions(db, account: Account) -> None:
    keep = max(1, int(account.retention_versions or 3))
    snapshots = db.scalars(select(Snapshot).where(
        Snapshot.account_id == account.id,
        Snapshot.status == "completed",
    ).order_by(Snapshot.completed_at.desc(), Snapshot.id.desc())).all()
    ordinary_kept = 0
    removable: list[Snapshot] = []
    for snapshot in snapshots:
        if snapshot.id == account.active_snapshot_id or snapshot.protected:
            continue
        ordinary_kept += 1
        if ordinary_kept >= keep:
            removable.append(snapshot)
    paths: list[Path] = []
    for snapshot in removable:
        db.execute(text("DELETE FROM message_fts WHERE snapshot_id=:sid"), {"sid": snapshot.id})
        paths.append(snapshot_root(account.archive_uuid, snapshot.snapshot_uuid))
        db.delete(snapshot)
    db.commit()
    for path in paths:
        shutil.rmtree(path, ignore_errors=True)


def _check_cancel(db, job: BackupJob) -> None:
    db.refresh(job, ["cancel_requested"])
    if job.cancel_requested:
        job.status = "cancelling"
        db.commit()
        raise BackupCancelled("Backup interrotto dall'utente")


def _connect(account: Account, password: str) -> StandardIMAPAdapter | MicrosoftGraphAdapter:
    if account.auth_provider == "microsoft":
        adapter = MicrosoftGraphAdapter(password)
    else:
        adapter = StandardIMAPAdapter(
            account.imap_host or "",
            int(account.imap_port or 993),
            account.security,
            account.imap_username or account.email,
            password,
        )
    adapter.connect()
    return adapter


def _connect_with_retry(account: Account, password: str) -> StandardIMAPAdapter | MicrosoftGraphAdapter:
    last_error = None
    for attempt in range(BACKUP_RETRIES):
        try:
            return _connect(account, password)
        except Exception as exc:
            last_error = exc
            if attempt + 1 < BACKUP_RETRIES:
                time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"Connessione IMAP fallita dopo {BACKUP_RETRIES} tentativi: {last_error}")


def run_backup(job_id: int) -> None:
    db = SessionLocal()
    adapter: StandardIMAPAdapter | MicrosoftGraphAdapter | None = None
    stage_path: Path | None = None
    snapshot: Snapshot | None = None
    try:
        job = db.get(BackupJob, job_id)
        if not job or job.status != "queued":
            return
        if job.cancel_requested:
            job.status = "cancelled"
            job.finished_at = utcnow()
            db.commit()
            return
        account = db.get(Account, job.account_id)
        if not account or not account.imap_enabled:
            raise RuntimeError("Account IMAP non disponibile")

        job.status = "running"
        job.started_at = utcnow()
        job.updated_at = utcnow()
        account.last_backup_status = "running"
        account.last_backup_error = None
        db.commit()
        log.info("Backup account %s iniziato", account.id)

        snapshot_uuid = str(uuid.uuid4())
        snapshot = Snapshot(account_id=account.id, snapshot_uuid=snapshot_uuid, status="staging")
        db.add(snapshot)
        db.commit()
        stage_path = snapshot_root(account.archive_uuid, f".staging-{snapshot_uuid}")
        stage_path.mkdir(parents=True, exist_ok=False)

        password = decrypt_secret(account.encrypted_password)
        adapter = _connect_with_retry(account, password)
        if account.auth_provider == "microsoft" and getattr(adapter, "refresh_token", password) != password:
            password = adapter.refresh_token
            account.encrypted_password = encrypt_secret(password)
            db.commit()
        remote_folders = adapter.list_folders(account.root_folder)
        selectable = [folder for folder in remote_folders if "\\Noselect" not in folder.flags]

        # A lightweight pre-pass obtains a meaningful total without retaining UID lists in RAM.
        total = 0
        for remote in selectable:
            _check_cancel(db, job)
            _uidvalidity, exists = adapter.select_folder(remote.name)
            total += exists
        job.total_messages = total
        db.commit()

        processed = 0
        attachment_count = 0
        folder_counts: dict[str, int] = {}
        started_monotonic = time.monotonic()
        smoothed_rate = 0.0
        for remote in selectable:
            _check_cancel(db, job)
            job.current_folder = remote.name
            db.commit()
            uidvalidity, _exists = adapter.select_folder(remote.name)
            uids = adapter.message_uids()
            folder = Folder(
                snapshot_id=snapshot.id,
                name=remote.name,
                delimiter=remote.delimiter,
                flags_json=json.dumps(remote.flags, ensure_ascii=False),
                uidvalidity=uidvalidity,
                message_count=len(uids),
            )
            db.add(folder)
            db.commit()
            folder_counts[remote.name] = len(uids)
            log.info("Backup account %s, cartella %s (%s messaggi)", account.id, remote.name, len(uids))

            for offset in range(0, len(uids), IMAP_FETCH_BATCH):
                _check_cancel(db, job)
                uid_batch = uids[offset:offset + IMAP_FETCH_BATCH]
                last_error = None
                for attempt in range(BACKUP_RETRIES):
                    try:
                        remote_messages = list(adapter.fetch_messages(uid_batch))
                        last_error = None
                        break
                    except Exception as exc:
                        last_error = exc
                        adapter.logout()
                        if attempt + 1 < BACKUP_RETRIES:
                            time.sleep(min(2 ** attempt, 8))
                            adapter = _connect_with_retry(account, password)
                            adapter.select_folder(remote.name)
                if last_error is not None:
                    raise RuntimeError(f"Fetch IMAP fallito nella cartella {remote.name}: {last_error}")

                for remote_message in remote_messages:
                    parsed = parse_and_store(remote_message.raw, stage_path)
                    flags_lower = {flag.lower() for flag in remote_message.flags}
                    internal_date = remote_message.internal_date
                    if internal_date and internal_date.tzinfo:
                        internal_date = internal_date.astimezone(timezone.utc).replace(tzinfo=None)
                    message = Message(
                        snapshot_id=snapshot.id,
                        folder_id=folder.id,
                        imap_uid=str(remote_message.uid),
                        message_id=parsed.message_id,
                        in_reply_to=parsed.in_reply_to,
                        references_json=parsed.references_json,
                        thread_key=parsed.thread_key,
                        subject=parsed.subject,
                        sender=parsed.sender,
                        recipients_to=parsed.recipients_to,
                        recipients_cc=parsed.recipients_cc,
                        recipients_bcc=parsed.recipients_bcc,
                        reply_to=parsed.reply_to,
                        date_utc=parsed.date_utc,
                        internal_date=internal_date,
                        headers_json=parsed.headers_json,
                        text_body=parsed.text_body,
                        html_body=parsed.html_body,
                        mime_json=parsed.mime_json,
                        flags_json=json.dumps(remote_message.flags, ensure_ascii=False),
                        is_read="\\seen" in flags_lower,
                        is_starred="\\flagged" in flags_lower,
                        is_answered="\\answered" in flags_lower,
                        has_attachments=bool(parsed.attachments),
                        size=len(remote_message.raw),
                        raw_sha256=parsed.raw_sha256,
                        raw_relpath=parsed.raw_relpath,
                    )
                    db.add(message)
                    db.flush()
                    for item in parsed.attachments:
                        db.add(Attachment(
                            message_id=message.id,
                            filename=item.filename,
                            content_type=item.content_type,
                            size=item.size,
                            sha256=item.sha256,
                            relpath=item.relpath,
                            content_id=item.content_id,
                            is_inline=item.is_inline,
                        ))
                    recipients = " ".join((parsed.recipients_to, parsed.recipients_cc, parsed.recipients_bcc))
                    db.execute(text(
                        "INSERT INTO message_fts(message_id,snapshot_id,subject,sender,recipients,body) "
                        "VALUES (:message_id,:snapshot_id,:subject,:sender,:recipients,:body)"
                    ), {
                        "message_id": message.id,
                        "snapshot_id": snapshot.id,
                        "subject": parsed.subject,
                        "sender": parsed.sender,
                        "recipients": recipients,
                        "body": parsed.text_body,
                    })
                    processed += 1
                    attachment_count += len(parsed.attachments)

                job.processed_messages = processed
                job.attachment_count = attachment_count
                job.percent = min(99, int(processed * 100 / total)) if total else 0
                elapsed = max(0.001, time.monotonic() - started_monotonic)
                sample_rate = processed / elapsed
                smoothed_rate = sample_rate if not smoothed_rate else (smoothed_rate * 0.75 + sample_rate * 0.25)
                job.throughput = round(smoothed_rate, 3)
                job.eta_seconds = (
                    int(max(0, total - processed) / smoothed_rate)
                    if processed >= 10 and elapsed >= 5 and total > processed and smoothed_rate > 0 else None
                )
                job.updated_at = utcnow()
                snapshot.message_count = processed
                snapshot.attachment_count = attachment_count
                snapshot.folder_counts_json = json.dumps(folder_counts, ensure_ascii=False)
                db.commit()
                owner = db.get(User, account.owner_id)
                if owner and owner.plan != "PLUS":
                    existing = db.scalar(select(func.coalesce(func.sum(Snapshot.archive_size), 0)).join(Account, Snapshot.account_id == Account.id).where(
                        Account.owner_id == owner.id, Snapshot.status.in_(["completed", "active"]))) or 0
                    if existing + _directory_size(stage_path) > owner.storage_limit_bytes:
                        raise RuntimeError("Storage limit reached; the previous valid backup was preserved")

        _check_cancel(db, job)
        archive_size = _directory_size(stage_path)
        final_path = snapshot_root(account.archive_uuid, snapshot.snapshot_uuid)
        stage_path.replace(final_path)  # atomic while both paths are on the same volume
        stage_path = final_path  # retained until the database switch commits, for rollback cleanup

        old_snapshot_id = account.active_snapshot_id
        old_snapshot = db.get(Snapshot, old_snapshot_id) if old_snapshot_id else None
        snapshot.status = "completed"
        snapshot.completed_at = utcnow()
        owner = db.get(User, account.owner_id)
        if owner and owner.plan != "PLUS" and not account.is_permanent:
            snapshot.expires_at = snapshot.created_at + timedelta(days=get_int_setting("standard_retention_days", 30))
        snapshot.archive_size = archive_size
        snapshot.attachment_count = attachment_count
        snapshot.folder_counts_json = json.dumps(folder_counts, ensure_ascii=False)
        comparison = _snapshot_comparison(old_snapshot, snapshot) if old_snapshot else None
        if comparison:
            snapshot.comparison_json = json.dumps(comparison, ensure_ascii=False)
            if comparison["suspicious"]:
                old_snapshot.protected = True
                old_snapshot.protection_reason = (
                    f"Riduzione anomala: {comparison['messages_removed']} messaggi in meno "
                    f"({comparison['previous_messages']} → {comparison['new_messages']})."
                )
        account.active_snapshot_id = snapshot.id
        account.message_count = processed
        account.archive_size = archive_size
        account.last_backup_at = utcnow()
        account.last_backup_status = "completed"
        account.last_backup_error = None
        account.next_backup_at = next_backup_time(account, account.last_backup_at)
        job.status = "completed"
        job.current_folder = None
        job.processed_messages = processed
        job.total_messages = max(total, processed)
        job.attachment_count = attachment_count
        job.throughput = round(smoothed_rate, 3)
        job.eta_seconds = 0
        job.percent = 100
        job.finished_at = utcnow()
        job.updated_at = utcnow()
        db.commit()
        stage_path = None
        try:
            rotate_versions(db, account)
        except Exception:
            db.rollback()
            log.exception("Rotazione versioni fallita per l'account %s", account.id)
        log.info("Backup account %s completato: %s messaggi", account.id, processed)
        from .telegram_service import notify_backup
        notify_backup(account.id, "completed")
    except BackupCancelled as exc:
        _fail_or_cancel(db, job_id, snapshot, stage_path, "cancelled", str(exc))
        log.info("Backup job %s interrotto", job_id)
    except Exception as exc:
        log.exception("Backup job %s fallito", job_id)
        _fail_or_cancel(db, job_id, snapshot, stage_path, "failed", str(exc))
        try:
            job = db.get(BackupJob, job_id)
            if job:
                from .telegram_service import notify_backup
                notify_backup(job.account_id, "failed")
        except Exception:
            log.warning("Notifica Telegram fallita per job %s", job_id)
    finally:
        if adapter:
            adapter.logout()
        db.close()


def _fail_or_cancel(db, job_id, snapshot, stage_path, status: str, error: str) -> None:
    try:
        db.rollback()
        job = db.get(BackupJob, job_id)
        if job:
            job.status = status
            job.error = error[:4000]
            job.finished_at = utcnow()
            account = db.get(Account, job.account_id)
            if account:
                account.last_backup_status = status
                account.last_backup_error = error[:4000]
                account.next_backup_at = next_backup_time(account)
        if snapshot and snapshot.id:
            db.execute(text("DELETE FROM message_fts WHERE snapshot_id=:sid"), {"sid": snapshot.id})
            stale = db.get(Snapshot, snapshot.id)
            if stale:
                db.delete(stale)
        db.commit()
    finally:
        if stage_path:
            shutil.rmtree(stage_path, ignore_errors=True)


class BackupManager:
    def __init__(self) -> None:
        self.executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix="emboxa-backup")
        self._lock = threading.Lock()
        self._futures = {}

    def _submit_locked(self, job_id: int) -> None:
        if job_id in self._futures:
            return
        future = self.executor.submit(run_backup, job_id)
        self._futures[job_id] = future
        future.add_done_callback(lambda _f, jid=job_id: self._complete(jid))

    def _complete(self, job_id: int) -> None:
        with self._lock:
            self._futures.pop(job_id, None)
            self._drain_locked()

    def _drain_locked(self) -> None:
        limit = max(1, min(16, get_int_setting("backup_concurrency", 1)))
        available = max(0, limit - len(self._futures))
        if not available:
            return
        with SessionLocal() as db:
            ids = list(db.scalars(select(BackupJob.id).where(
                BackupJob.status == "queued", BackupJob.cancel_requested.is_(False),
                BackupJob.id.not_in(list(self._futures) or [-1]),
            ).order_by(BackupJob.created_at, BackupJob.id).limit(available)).all())
        for queued_id in ids:
            self._submit_locked(queued_id)

    def start(self, account_id: int) -> tuple[int, bool]:
        with self._lock:
            with SessionLocal() as db:
                running = db.scalar(select(BackupJob).where(
                    BackupJob.account_id == account_id,
                    BackupJob.status.in_(["queued", "running", "cancelling"]),
                ))
                if running:
                    return running.id, False
                job = BackupJob(account_id=account_id, status="queued")
                db.add(job)
                db.commit()
                job_id = job.id
            self._drain_locked()
            return job_id, True

    def resume_pending(self) -> None:
        with self._lock:
            self._drain_locked()

    def refresh(self) -> None:
        self.resume_pending()

    def cancel(self, job_id: int) -> bool:
        with SessionLocal() as db:
            job = db.get(BackupJob, job_id)
            if not job or job.status not in {"queued", "running", "cancelling"}:
                return False
            if job.status == "queued":
                job.status = "cancelled"
                job.cancel_requested = True
                job.finished_at = utcnow()
            else:
                job.cancel_requested = True
                job.status = "cancelling"
            db.commit()
            return True

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=False)


def recover_interrupted_jobs() -> None:
    with SessionLocal() as db:
        jobs = db.scalars(select(BackupJob).where(BackupJob.status.in_(["queued", "running", "cancelling"]))).all()
        for job in jobs:
            if job.status == "cancelling" or job.cancel_requested:
                job.status = "cancelled"
                job.error = "Cancellazione completata durante il riavvio."
                job.finished_at = utcnow()
            else:
                job.status = "queued"
                job.error = "Ripreso in sicurezza dopo il riavvio."
                job.started_at = None
                job.finished_at = None
                job.current_folder = None
                job.processed_messages = 0
                job.total_messages = 0
                job.attachment_count = 0
                job.percent = 0
                job.throughput = 0
                job.eta_seconds = None
                job.cancel_requested = False
        staging = db.scalars(select(Snapshot).where(Snapshot.status == "staging")).all()
        for snapshot in staging:
            account = db.get(Account, snapshot.account_id)
            db.execute(text("DELETE FROM message_fts WHERE snapshot_id=:sid"), {"sid": snapshot.id})
            if account:
                shutil.rmtree(snapshot_root(account.archive_uuid, f".staging-{snapshot.snapshot_uuid}"), ignore_errors=True)
            db.delete(snapshot)
        db.commit()
    backup_manager.resume_pending()


backup_manager = BackupManager()
