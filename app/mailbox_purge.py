from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from sqlalchemy import func, select

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


def server_folders(account_id: int) -> dict:
    """The folders as the mail server has them right now, with the count it reports.

    Deliberately not the archive's list: this feeds the screen whose whole point is to act on the
    mailbox without consulting the backup, so it has to show the mailbox.
    """
    with SessionLocal() as db:
        account = db.get(Account, account_id)
        if not account or not account.imap_enabled:
            raise PurgeRefused("Account IMAP non disponibile")
        if account.auth_provider == "microsoft":
            raise PurgeRefused("Per ora è disponibile solo sulle caselle IMAP.")
        password = decrypt_secret(account.encrypted_password)

    adapter = _connect_with_retry(account, password)
    try:
        folders = []
        for remote in adapter.list_folders(account.root_folder):
            if "\\Noselect" in remote.flags:
                continue
            try:
                _uidvalidity, exists = adapter.select_folder(remote.name)
            except Exception:
                log.warning("Cartella %s non selezionabile", remote.name, exc_info=True)
                continue
            folders.append({"name": remote.name, "messages": int(exists)})
        return {"folders": folders}
    finally:
        try:
            adapter.logout()
        except Exception:
            log.warning("Logout dopo l'elenco cartelle non riuscito", exc_info=True)


def purge_folder_direct(account_id: int, folder_name: str, before: datetime | None = None,
                        progress: Callable[[int, str], None] | None = None,
                        should_cancel: Callable[[], bool] | None = None) -> dict:
    """Empty a folder on the server without consulting the archive at all.

    The archive-backed purge refuses anything it cannot prove is backed up. This is the opposite
    instruction, asked for deliberately: the mailbox belongs to whoever is asking, and emptying a
    folder is a thing every mail client does. Nothing it removes is recoverable by this program
    afterwards, which is why it lives on its own screen rather than as a checkbox on the safe one.

    `before` is resolved by the server through IMAP BEFORE, which tests the date the message
    arrived: with no archive to consult there is nowhere else a date could come from.
    """
    def report(percent: int, detail: str) -> None:
        if progress:
            progress(percent, detail)

    with SessionLocal() as db:
        account = db.get(Account, account_id)
        if not account or not account.imap_enabled:
            raise PurgeRefused("Account IMAP non disponibile")
        if account.auth_provider == "microsoft":
            raise PurgeRefused("Per ora è disponibile solo sulle caselle IMAP.")
        password = decrypt_secret(account.encrypted_password)

    adapter = _connect_with_retry(account, password)
    try:
        report(4, f"Apertura di «{folder_name}» sul server.")
        _uidvalidity, exists = adapter.select_folder(folder_name)
        adapter.select_write_folder(folder_name)
        targets = (adapter.uids_before(before.date()) if before is not None
                   else adapter.message_uids(expected=exists))

        total = len(targets)
        log.warning("Cancellazione diretta: account %s, cartella %s, %s messaggi, "
                    "nessuna verifica sull'archivio", account_id, folder_name, total)
        result = {"folder": folder_name, "before": before.isoformat() if before else None,
                  "found": total}
        if not total:
            return {**result, "deleted": 0}

        deleted = 0
        for offset in range(0, total, PURGE_BATCH):
            if should_cancel and should_cancel():
                return {**result, "deleted": deleted, "cancelled": True}
            batch = targets[offset:offset + PURGE_BATCH]
            adapter.delete_uids(batch)
            deleted += len(batch)
            report(4 + int(deleted / total * 94),
                   f"Cancellati {deleted} di {total} messaggi da «{folder_name}».")
        report(100, f"Cancellati {deleted} messaggi da «{folder_name}».")
        return {**result, "deleted": deleted}
    finally:
        try:
            adapter.logout()
        except Exception:
            log.warning("Logout dopo la cancellazione non riuscito", exc_info=True)


def _archived_uids(db, account: Account, folder_name: str) -> dict[str, datetime | None]:
    """Every archived UID of the folder, with the date the message carries.

    A dict rather than a set because a cutoff needs the date, and the date has to come from the
    archive: the server is about to be asked to delete things, not to be trusted about them.
    """
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
    rows = db.execute(select(Message.imap_uid, Message.date_utc, Message.internal_date).where(
        Message.folder_id == folder.id, Message.is_deleted.is_(False))).all()
    return {row.imap_uid: (row.date_utc or row.internal_date) for row in rows}


def purge_preview(account_id: int, before: datetime | None = None) -> dict:
    """What emptying each folder would actually delete, without deleting anything.

    With `before`, the counts answer the question actually being asked: how many go, how many stay,
    and how many have no date at all — which is a number worth seeing before choosing a cutoff
    rather than discovering afterwards.
    """
    with SessionLocal() as db:
        account = db.get(Account, account_id)
        if not account:
            raise PurgeRefused("Account non trovato")
        snapshot = db.get(Snapshot, account.active_snapshot_id) if account.active_snapshot_id else None
        if not snapshot:
            raise PurgeRefused("Questo account non ha un archivio: non c'è niente che copra la casella.")
        folders = db.scalars(select(Folder).where(Folder.snapshot_id == snapshot.id)
                             .order_by(Folder.name.collate("NOCASE"))).all()
        items = []
        for folder in folders:
            oldest, newest, undated = db.execute(select(
                func.min(func.coalesce(Message.date_utc, Message.internal_date)),
                func.max(func.coalesce(Message.date_utc, Message.internal_date)),
                func.count().filter(Message.date_utc.is_(None), Message.internal_date.is_(None)),
            ).where(Message.folder_id == folder.id, Message.is_deleted.is_(False))).one()
            deletable = folder.message_count
            if before is not None:
                deletable = db.scalar(select(func.count()).where(
                    Message.folder_id == folder.id, Message.is_deleted.is_(False),
                    func.coalesce(Message.date_utc, Message.internal_date) < before)) or 0
            items.append({
                "name": folder.name,
                "archived": folder.message_count,
                "remote": folder.remote_count,
                # Only a folder archived in full may be emptied. The count the server gave at
                # backup time is the yardstick; anything short and the answer is no.
                "complete": folder.remote_count is None or folder.message_count >= folder.remote_count,
                "deletable": deletable,
                "kept": folder.message_count - deletable,
                "undated": int(undated or 0),
                "oldest": oldest,
                "newest": newest,
            })
        return {"snapshot_at": snapshot.completed_at, "before": before, "folders": items}


def purge_folder(account_id: int, folder_name: str, before: datetime | None = None,
                 progress: Callable[[int, str], None] | None = None,
                 should_cancel: Callable[[], bool] | None = None) -> dict:
    """Delete from the mail server the messages of `folder_name` that the archive already holds.

    The set is an intersection, not a folder wipe: only UIDs that are both on the server now and
    in the active snapshot are deleted. Mail that arrived after the backup is not in the archive,
    so it is left where it is and reported — the alternative is destroying the one copy of a
    message nobody has read yet.

    `before` keeps everything from that moment on. A message the archive holds no date for is
    always kept: not knowing when something is dated is not a reason to decide it is old.
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

        def in_range(uid) -> bool:
            if str(uid) not in archived:
                return False              # not in the archive: no copy exists, leave it alone
            if before is None:
                return True
            when = archived[str(uid)]
            return when is not None and when < before

        deletable = [uid for uid in server_uids if in_range(uid)]
        untouched = len(server_uids) - len(deletable)
        log.info("Purge account %s cartella %s: %s da cancellare, %s non archiviati e lasciati",
                 account_id, folder_name, len(deletable), untouched)
        if not deletable:
            return {"deleted": 0, "untouched": untouched, "folder": folder_name,
                    "before": before.isoformat() if before else None}

        deleted = 0
        total = len(deletable)
        for offset in range(0, total, PURGE_BATCH):
            if should_cancel and should_cancel():
                return {"deleted": deleted, "untouched": untouched, "folder": folder_name,
                        "before": before.isoformat() if before else None, "cancelled": True}
            batch = deletable[offset:offset + PURGE_BATCH]
            adapter.delete_uids(batch)
            deleted += len(batch)
            report(5 + int(deleted / total * 93),
                   f"Cancellati {deleted} di {total} messaggi da «{folder_name}».")
        report(100, f"Cancellati {deleted} messaggi da «{folder_name}».")
        return {"deleted": deleted, "untouched": untouched, "folder": folder_name,
                "before": before.isoformat() if before else None}
    finally:
        try:
            adapter.logout()
        except Exception:
            log.warning("Logout dopo lo svuotamento non riuscito", exc_info=True)
