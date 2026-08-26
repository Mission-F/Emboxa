from __future__ import annotations

import hashlib
import json
import logging
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable

from sqlalchemy import text

from .backup import snapshot_root
from .database import SessionLocal
from .mail_parser import parse_and_store
from .models import Account, Attachment, Folder, Message, Snapshot, utcnow

log = logging.getLogger("emboxa.mbox_import")


class MboxImportError(ValueError):
    pass


@dataclass(frozen=True)
class MboxSource:
    path: Path
    original_name: str
    folder_name: str


def folder_name_from_upload(filename: str) -> str:
    clean = filename.replace("\\", "/").strip("/")
    path = PurePosixPath(clean)
    parts = [part for part in path.parts if part and part != "."]
    if not parts:
        return "MBOX import"
    if parts[-1].lower() == "mbox" and len(parts) >= 2:
        name = parts[-2]
    else:
        name = parts[-1]
    if name.lower().endswith(".mbox"):
        name = name[:-5]
    return (name.strip() or "MBOX import")[:1000]


def _iter_mbox_messages(path: Path, progress: Callable[[int], None] | None = None):
    buffer = bytearray()
    seen_boundary = False
    with path.open("rb") as handle:
        while True:
            line = handle.readline()
            if not line:
                break
            if progress:
                progress(len(line))
            if line.startswith(b"From "):
                if buffer:
                    yield bytes(buffer)
                    buffer.clear()
                seen_boundary = True
                continue
            buffer.extend(line)
        if buffer:
            yield bytes(buffer)
        elif not seen_boundary and path.stat().st_size:
            handle.seek(0)
            data = handle.read()
            if data.strip():
                yield data


def _directory_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _unique_folder_name(base: str, used: set[str]) -> str:
    name = base[:1000] or "MBOX import"
    if name not in used:
        used.add(name)
        return name
    index = 2
    while True:
        suffix = f" ({index})"
        candidate = f"{name[:1000 - len(suffix)]}{suffix}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        index += 1


def import_mbox_sources(
    sources: Iterable[MboxSource],
    owner_id: int,
    display_name: str = "Archivio MBOX importato",
    email: str = "mbox-import@local.invalid",
    progress: Callable[[int, int, str], None] | None = None,
) -> int:
    items = [source for source in sources if source.path.is_file()]
    if not items:
        raise MboxImportError("Nessun file MBOX valido trovato")

    account_uuid = str(uuid.uuid4())
    snapshot_uuid = str(uuid.uuid4())
    stage = snapshot_root(account_uuid, f".staging-{snapshot_uuid}")
    stage.mkdir(parents=True, exist_ok=False)
    total_bytes = sum(item.path.stat().st_size for item in items)
    processed_bytes = 0
    db = SessionLocal()
    account: Account | None = None
    snapshot: Snapshot | None = None
    try:
        account = Account(
            owner_id=owner_id,
            archive_uuid=account_uuid,
            display_name=display_name.strip()[:200] or "Archivio MBOX importato",
            email=email.strip()[:320] or "mbox-import@local.invalid",
            imap_enabled=False,
            security="plain",
            auth_provider="mbox",
            schedule_mode="disabled",
            last_backup_status="imported",
            mailbox_identity=hashlib.sha256(f"{owner_id}:{account_uuid}:mbox".encode()).hexdigest(),
        )
        db.add(account)
        db.flush()
        snapshot = Snapshot(
            account_id=account.id,
            snapshot_uuid=snapshot_uuid,
            status="staging",
            created_at=utcnow(),
            completed_at=utcnow(),
            message_count=0,
            archive_size=0,
        )
        db.add(snapshot)
        db.flush()

        used_folders: set[str] = set()
        folder_counts: dict[str, int] = {}
        total_messages = 0
        total_attachments = 0

        def advance(amount: int, folder_label: str) -> None:
            nonlocal processed_bytes
            processed_bytes += amount
            if progress:
                percent = 15 + int(min(80, (processed_bytes / max(1, total_bytes)) * 80))
                progress(min(95, percent), total_messages, folder_label)

        for source in items:
            folder_label = _unique_folder_name(source.folder_name, used_folders)
            folder = Folder(
                snapshot_id=snapshot.id,
                name=folder_label,
                delimiter="/",
                flags_json="[]",
                uidvalidity="mbox-import",
                message_count=0,
            )
            db.add(folder)
            db.flush()

            folder_messages = 0
            for raw in _iter_mbox_messages(source.path, lambda amount, label=folder_label: advance(amount, label)):
                if not raw.strip():
                    continue
                try:
                    parsed = parse_and_store(raw, stage)
                except Exception as exc:
                    log.warning("Skipping malformed MBOX message from %s: %s", source.original_name, exc)
                    continue
                folder_messages += 1
                total_messages += 1
                message = Message(
                    snapshot_id=snapshot.id,
                    folder_id=folder.id,
                    imap_uid=str(folder_messages),
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
                    internal_date=parsed.date_utc,
                    headers_json=parsed.headers_json,
                    text_body=parsed.text_body,
                    html_body=parsed.html_body,
                    mime_json=parsed.mime_json,
                    flags_json="[]",
                    is_read=False,
                    is_starred=False,
                    is_answered=False,
                    has_attachments=bool(parsed.attachments),
                    size=len(raw),
                    raw_sha256=parsed.raw_sha256,
                    raw_relpath=parsed.raw_relpath,
                )
                db.add(message)
                db.flush()
                for attachment in parsed.attachments:
                    db.add(Attachment(
                        message_id=message.id,
                        filename=attachment.filename,
                        content_type=attachment.content_type,
                        size=attachment.size,
                        sha256=attachment.sha256,
                        relpath=attachment.relpath,
                        content_id=attachment.content_id,
                        is_inline=attachment.is_inline,
                    ))
                total_attachments += len(parsed.attachments)
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
                if total_messages % 500 == 0:
                    folder.message_count = folder_messages
                    snapshot.message_count = total_messages
                    snapshot.attachment_count = total_attachments
                    snapshot.folder_counts_json = json.dumps({**folder_counts, folder_label: folder_messages}, ensure_ascii=False)
                    db.commit()

            folder.message_count = folder_messages
            folder_counts[folder_label] = folder_messages
            snapshot.folder_counts_json = json.dumps(folder_counts, ensure_ascii=False)
            db.commit()

        archive_size = _directory_size(stage)
        final = snapshot_root(account_uuid, snapshot_uuid)
        stage.replace(final)
        snapshot.status = "completed"
        snapshot.completed_at = utcnow()
        snapshot.message_count = total_messages
        snapshot.attachment_count = total_attachments
        snapshot.archive_size = archive_size
        snapshot.folder_counts_json = json.dumps(folder_counts, ensure_ascii=False)
        account.active_snapshot_id = snapshot.id
        account.message_count = total_messages
        account.archive_size = archive_size
        account.last_backup_at = snapshot.completed_at
        account.last_backup_status = "imported"
        account.last_backup_error = None
        db.commit()
        if progress:
            progress(100, total_messages, "Import completato")
        return account.id
    except Exception:
        db.rollback()
        if account and account.id:
            try:
                db.execute(text("DELETE FROM message_fts WHERE snapshot_id IN (SELECT id FROM snapshots WHERE account_id=:aid)"), {"aid": account.id})
                stale = db.get(Account, account.id)
                if stale:
                    stale.active_snapshot_id = None
                    db.flush()
                    db.delete(stale)
                db.commit()
            except Exception:
                db.rollback()
        shutil.rmtree(stage, ignore_errors=True)
        shutil.rmtree(snapshot_root(account_uuid, snapshot_uuid), ignore_errors=True)
        raise
    finally:
        db.close()
