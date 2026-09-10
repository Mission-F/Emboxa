from __future__ import annotations

import logging
from typing import Callable

from sqlalchemy import select

from .backup import _connect_with_retry
from .database import SessionLocal
from .models import Account, Folder, Message, Snapshot
from .security import decrypt_secret

log = logging.getLogger("emboxa.purge")

# Small enough that a stall costs one batch, large enough that 38 000 messages is not 38 000 round
# trips. Progress and the cancel check happen between batches, never inside one.
PURGE_BATCH = 200


class PurgeRefused(RuntimeError):
    """The archive does not cover what is about to be deleted."""


def _archived_uids(db, account: Account, folder_name: str) -> set[str]:
    snapshot = db.get(Snapshot, account.active_snapshot_id) if account.active_snapshot_id else None
    if not snapshot or snapshot.status not in {"completed", "active"}:
        raise PurgeRefused("Questo account non ha un archivio completato: non c'è nulla che copra "
                           "i messaggi che stai per cancellare.")
    folder = db.scalar(select(Folder).where(Folder.snapshot_id == snapshot.id,
                                            Folder.name == folder_name))
    if not folder:
        raise PurgeRefused(f"La cartella «{folder_name}» non è presente nell'archivio.")
    if folder.remote_count is not None and folder.message_count < folder.remote_count:
        raise PurgeRefused(
            f"L'archivio contiene {folder.message_count} messaggi dei {folder.remote_count} che il "
            f"server dichiarava per «{folder_name}». Rifiuto di cancellare una cartella che non è "
            "stata archiviata per intero: rifai il backup e riprova.")
    return set(db.scalars(select(Message.imap_uid).where(
        Message.folder_id == folder.id, Message.is_deleted.is_(False))).all())


def purge_preview(account_id: int) -> dict:
    """What emptying each folder would actually delete, without deleting anything."""
    with SessionLocal() as db:
        account = db.get(Account, account_id)
        if not account:
            raise PurgeRefused("Account non trovato")
        snapshot = db.get(Snapshot, account.active_snapshot_id) if account.active_snapshot_id else None
        if not snapshot:
            raise PurgeRefused("Questo account non ha un archivio: non c'è niente che copra la casella.")
        folders = db.scalars(select(Folder).where(Folder.snapshot_id == snapshot.id)
                             .order_by(Folder.name.collate("NOCASE"))).all()
        return {
            "snapshot_at": snapshot.completed_at,
            "folders": [{
                "name": folder.name,
                "archived": folder.message_count,
                "remote": folder.remote_count,
                # Only a folder archived in full may be emptied. The count the server gave at
                # backup time is the yardstick; anything short and the answer is no.
                "complete": folder.remote_count is None or folder.message_count >= folder.remote_count,
            } for folder in folders],
        }


def purge_folder(account_id: int, folder_name: str,
                 progress: Callable[[int, str], None] | None = None,
                 should_cancel: Callable[[], bool] | None = None) -> dict:
    """Delete from the mail server the messages of `folder_name` that the archive already holds.

    The set is an intersection, not a folder wipe: only UIDs that are both on the server now and
    in the active snapshot are deleted. Mail that arrived after the backup is not in the archive,
    so it is left where it is and reported — the alternative is destroying the one copy of a
    message nobody has read yet.
    """
    def report(percent: int, detail: str) -> None:
        if progress:
            progress(percent, detail)

    report(2, "Verifica della copertura dell'archivio.")
    with SessionLocal() as db:
        account = db.get(Account, account_id)
        if not account or not account.imap_enabled:
            raise PurgeRefused("Account IMAP non disponibile")
        if account.auth_provider == "microsoft":
            raise PurgeRefused("Per ora lo svuotamento è disponibile solo sulle caselle IMAP.")
        archived = _archived_uids(db, account, folder_name)
        password = decrypt_secret(account.encrypted_password)

    if not archived:
        raise PurgeRefused(f"L'archivio non contiene messaggi per «{folder_name}».")

    adapter = _connect_with_retry(account, password)
    try:
        report(5, f"Apertura di «{folder_name}» sul server.")
        adapter.select_write_folder(folder_name)
        _uidvalidity, exists = adapter.select_folder(folder_name)
        adapter.select_write_folder(folder_name)
        server_uids = adapter.message_uids(expected=exists)

        deletable = [uid for uid in server_uids if str(uid) in archived]
        untouched = len(server_uids) - len(deletable)
        log.info("Purge account %s cartella %s: %s da cancellare, %s non archiviati e lasciati",
                 account_id, folder_name, len(deletable), untouched)
        if not deletable:
            return {"deleted": 0, "untouched": untouched, "folder": folder_name}

        deleted = 0
        total = len(deletable)
        for offset in range(0, total, PURGE_BATCH):
            if should_cancel and should_cancel():
                return {"deleted": deleted, "untouched": untouched, "folder": folder_name,
                        "cancelled": True}
            batch = deletable[offset:offset + PURGE_BATCH]
            adapter.delete_uids(batch)
            deleted += len(batch)
            report(5 + int(deleted / total * 93),
                   f"Cancellati {deleted} di {total} messaggi da «{folder_name}».")
        report(100, f"Cancellati {deleted} messaggi da «{folder_name}».")
        return {"deleted": deleted, "untouched": untouched, "folder": folder_name}
    finally:
        try:
            adapter.logout()
        except Exception:
            log.warning("Logout dopo lo svuotamento non riuscito", exc_info=True)
