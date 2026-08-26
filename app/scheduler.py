from __future__ import annotations

import logging
import shutil
import threading
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select, text

from .backup import backup_manager, snapshot_root
from .config import ARCHIVES_DIR, EXPORTS_DIR
from .database import SessionLocal
from .models import (Account, BackupJob, NotificationDelivery, PermanentMailboxHistory, SecurityToken,
                     Snapshot, User, WebExport, utcnow)
from .telegram_service import notify_user
from .settings_service import get_bool_setting, save_setting
from .storage import user_storage_used

log = logging.getLogger("mailvault.scheduler")


class IntegratedScheduler:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="mailvault-scheduler", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        log.info("Scheduler integrato avviato")
        while not self._stop.wait(30):
            try:
                now = utcnow()
                with SessionLocal() as db:
                    due = db.scalars(select(Account).where(
                        Account.imap_enabled.is_(True),
                        Account.schedule_mode != "disabled",
                        Account.next_backup_at.is_not(None),
                        Account.next_backup_at <= now,
                    )).all()
                    ids = [account.id for account in due]
                if get_bool_setting("backup_queue_enabled", True):
                    for account_id in ids:
                        backup_manager.start(account_id)
                if get_bool_setting("cleanup_enabled", True):
                    self.cleanup()
                self.notifications()
            except Exception:
                log.exception("Errore scheduler")

    def cleanup(self) -> None:
        now = utcnow(); paths: list[Path] = []
        with SessionLocal() as db:
            for item in db.scalars(select(WebExport).where(WebExport.expires_at.is_not(None), WebExport.expires_at <= now)).all():
                paths.append(EXPORTS_DIR / item.relpath); db.delete(item)
            for token in db.scalars(select(SecurityToken).where(SecurityToken.expires_at <= now)).all():
                db.delete(token)
            expired = db.scalars(select(Snapshot).where(Snapshot.expires_at.is_not(None), Snapshot.expires_at <= now,
                                                        Snapshot.status.in_(["completed", "active"]))).all()
            for snapshot in expired:
                account = snapshot.account; snapshot.status = "expired"
                db.execute(text("DELETE FROM message_fts WHERE snapshot_id=:sid"), {"sid": snapshot.id})
                paths.append(snapshot_root(account.archive_uuid, snapshot.snapshot_uuid))
                if account.active_snapshot_id == snapshot.id:
                    replacement = db.scalar(select(Snapshot).where(Snapshot.account_id == account.id,
                        Snapshot.id != snapshot.id, Snapshot.status.in_(["completed", "active"]),
                        (Snapshot.expires_at.is_(None) | (Snapshot.expires_at > now))).order_by(Snapshot.completed_at.desc()))
                    account.active_snapshot_id = replacement.id if replacement else None
                    account.message_count = replacement.message_count if replacement else 0
                    account.archive_size = replacement.archive_size if replacement else 0
                db.delete(snapshot)
            for job in db.scalars(select(BackupJob).where(BackupJob.status.in_(["running", "cancelling"]),
                                                           BackupJob.updated_at < now - timedelta(hours=12))).all():
                job.status = "failed"; job.error = "Recovered stale backup job"; job.finished_at = now
            db.commit()
        with SessionLocal() as db:
            save_setting(db, "last_cleanup_at", utcnow().isoformat(timespec="seconds"))
            db.commit()
        for path in paths:
            if path.is_file():
                path.unlink(missing_ok=True)
            elif path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
        for staging in ARCHIVES_DIR.glob("*/.staging-*"):
            try:
                if staging.stat().st_mtime < (now - timedelta(days=1)).timestamp():
                    shutil.rmtree(staging, ignore_errors=True)
            except OSError:
                continue

    def notifications(self) -> None:
        now = utcnow()
        with SessionLocal() as db:
            soon = db.scalars(select(Snapshot).join(Account, Snapshot.account_id == Account.id).where(
                Snapshot.status.in_(["completed", "active"]), Snapshot.expires_at.is_not(None),
                Snapshot.expires_at > now, Snapshot.expires_at <= now + timedelta(days=7))).all()
            for snapshot in soon:
                days = max(1, (snapshot.expires_at - now).days + 1); key = f"expiring:{snapshot.id}:7"
                if not db.scalar(select(NotificationDelivery).where(NotificationDelivery.user_id == snapshot.account.owner_id,
                                                                    NotificationDelivery.event_key == key)):
                    if notify_user(snapshot.account.owner_id, f"{snapshot.account.display_name} expires in {days} days.", "notify_expiring"):
                        db.add(NotificationDelivery(user_id=snapshot.account.owner_id, event_key=key))
            for user in db.scalars(select(User).where(User.plan == "STANDARD", User.storage_limit_bytes > 0)).all():
                used = user_storage_used(db, user.id)
                threshold = 95 if used >= user.storage_limit_bytes * .95 else (80 if used >= user.storage_limit_bytes * .8 else 0)
                key = f"storage:{threshold}"
                if threshold and not db.scalar(select(NotificationDelivery).where(NotificationDelivery.user_id == user.id,
                                                                                   NotificationDelivery.event_key == key)):
                    if notify_user(user.id, f"Storage is {threshold}% full.", "notify_storage"):
                        db.add(NotificationDelivery(user_id=user.id, event_key=key))
            locks = db.scalars(select(PermanentMailboxHistory).where(PermanentMailboxHistory.locked_until <= now)).all()
            for lock in locks:
                key = f"permanent-lock:{lock.id}"
                if not db.scalar(select(NotificationDelivery).where(NotificationDelivery.user_id == lock.user_id,
                                                                    NotificationDelivery.event_key == key)):
                    if notify_user(lock.user_id, "Permanent mailbox lock expired.", "notify_expiring"):
                        db.add(NotificationDelivery(user_id=lock.user_id, event_key=key))
            db.commit()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())


scheduler = IntegratedScheduler()
