from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import uuid
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

from sqlalchemy import select, text

from . import __version__
from .backup import snapshot_root
from .config import ARCHIVES_DIR, EXPORTS_DIR, IMPORT_MAX_EXPANDED_BYTES
from .database import SessionLocal
from .models import Account, Attachment, Folder, Message, Snapshot, utcnow
from .security import safe_filename, safe_resolve

FORMAT_NAME = "mailvault"
FORMAT_VERSION = 1


class ArchiveError(ValueError):
    pass


def _hash_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _dt(value):
    return value.isoformat() if value else None


def _parse_dt(value):
    return datetime.fromisoformat(value) if value else None


def _write_jsonl(path: Path, rows) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def build_export(account_id: int) -> tuple[Path, str]:
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    db = SessionLocal()
    temp_dir = Path(tempfile.mkdtemp(prefix="mailvault-export-", dir=EXPORTS_DIR))
    try:
        account = db.get(Account, account_id)
        if not account or not account.active_snapshot_id:
            raise ArchiveError("L'account non contiene un archivio esportabile")
        snapshot = db.get(Snapshot, account.active_snapshot_id)
        if not snapshot or snapshot.status not in {"completed", "active"}:
            raise ArchiveError("Snapshot attivo non disponibile")
        source_root = snapshot_root(account.archive_uuid, snapshot.snapshot_uuid)
        if not source_root.is_dir():
            raise ArchiveError("I file dello snapshot non sono disponibili")

        folders_path = temp_dir / "folders.jsonl"
        messages_path = temp_dir / "messages.jsonl"
        attachments_path = temp_dir / "attachments.jsonl"

        folders = db.scalars(select(Folder).where(Folder.snapshot_id == snapshot.id).order_by(Folder.id)).all()
        _write_jsonl(folders_path, ({
            "export_id": folder.id,
            "name": folder.name,
            "delimiter": folder.delimiter,
            "flags_json": folder.flags_json,
            "uidvalidity": folder.uidvalidity,
            "message_count": folder.message_count,
        } for folder in folders))

        messages = db.scalars(
            select(Message)
            .where(Message.snapshot_id == snapshot.id, Message.is_deleted.is_(False))
            .order_by(Message.id)
        ).yield_per(500)
        _write_jsonl(messages_path, ({
            "export_id": item.id,
            "folder_export_id": item.folder_id,
            "imap_uid": item.imap_uid,
            "message_id": item.message_id,
            "in_reply_to": item.in_reply_to,
            "references_json": item.references_json,
            "thread_key": item.thread_key,
            "subject": item.subject,
            "sender": item.sender,
            "recipients_to": item.recipients_to,
            "recipients_cc": item.recipients_cc,
            "recipients_bcc": item.recipients_bcc,
            "reply_to": item.reply_to,
            "date_utc": _dt(item.date_utc),
            "internal_date": _dt(item.internal_date),
            "headers_json": item.headers_json,
            "text_body": item.text_body,
            "html_body": item.html_body,
            "mime_json": item.mime_json,
            "flags_json": item.flags_json,
            "is_read": item.is_read,
            "is_starred": item.is_starred,
            "is_answered": item.is_answered,
            "has_attachments": item.has_attachments,
            "size": item.size,
            "raw_sha256": item.raw_sha256,
            "raw_relpath": item.raw_relpath,
        } for item in messages))

        attachments = db.scalars(
            select(Attachment)
            .join(Message)
            .where(Message.snapshot_id == snapshot.id, Message.is_deleted.is_(False))
            .order_by(Attachment.id)
        ).yield_per(1000)
        _write_jsonl(attachments_path, ({
            "message_export_id": item.message_id,
            "filename": item.filename,
            "content_type": item.content_type,
            "size": item.size,
            "sha256": item.sha256,
            "relpath": item.relpath,
            "content_id": item.content_id,
            "is_inline": item.is_inline,
        } for item in attachments))

        export_name = safe_filename(f"{account.display_name}-{utcnow():%Y%m%d-%H%M}.mailvault", "archive.mailvault")
        export_path = temp_dir / export_name
        checksum_lines: list[str] = []
        with zipfile.ZipFile(export_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as bundle:
            for metadata_path in (folders_path, messages_path, attachments_path):
                arcname = f"metadata/{metadata_path.name}"
                checksum_lines.append(f"{_hash_file(metadata_path)}  {arcname}")
                bundle.write(metadata_path, arcname)

            relpaths = set(db.scalars(
                select(Message.raw_relpath)
                .where(Message.snapshot_id == snapshot.id, Message.is_deleted.is_(False))
                .distinct()
            ).all())
            relpaths.update(db.scalars(
                select(Attachment.relpath)
                .join(Message)
                .where(Message.snapshot_id == snapshot.id, Message.is_deleted.is_(False))
                .distinct()
            ).all())
            exported_size = 0
            for relpath in sorted(path for path in relpaths if path):
                source = safe_resolve(source_root, relpath)
                if not source.is_file():
                    raise ArchiveError(f"File archivio mancante: {relpath}")
                exported_size += source.stat().st_size
                arcname = f"files/{PurePosixPath(relpath).as_posix()}"
                checksum_lines.append(f"{_hash_file(source)}  {arcname}")
                bundle.write(source, arcname)

            checksums = ("\n".join(checksum_lines) + "\n").encode()
            manifest = {
                "format": FORMAT_NAME,
                "format_version": FORMAT_VERSION,
                "created_at": utcnow().isoformat(),
                "app_version": __version__,
                "checksums_sha256": hashlib.sha256(checksums).hexdigest(),
                "account": {"display_name": account.display_name, "email": account.email},
                "snapshot": {
                    "created_at": _dt(snapshot.created_at),
                    "completed_at": _dt(snapshot.completed_at),
                    "message_count": snapshot.message_count,
                    "archive_size": exported_size,
                },
                "credentials_included": False,
            }
            bundle.writestr("checksums.sha256", checksums)
            bundle.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode())
        return export_path, export_name
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    finally:
        db.close()


def _safe_zip_name(name: str) -> str:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "" in path.parts or "\\" in name:
        raise ArchiveError(f"Percorso non sicuro nell'archivio: {name}")
    return path.as_posix()


def validate_archive(path: Path) -> tuple[dict, dict[str, str]]:
    try:
        bundle = zipfile.ZipFile(path, "r")
    except (zipfile.BadZipFile, OSError) as exc:
        raise ArchiveError("Il file non è un archivio .mailvault valido") from exc
    with bundle:
        infos = bundle.infolist()
        if len(infos) > 2_000_000:
            raise ArchiveError("L'archivio contiene troppi file")
        total = 0
        names = set()
        for info in infos:
            name = _safe_zip_name(info.filename)
            if name in names:
                raise ArchiveError(f"Voce duplicata nell'archivio: {name}")
            names.add(name)
            total += info.file_size
            if total > IMPORT_MAX_EXPANDED_BYTES:
                raise ArchiveError("L'archivio supera il limite massimo una volta estratto")
            if info.flag_bits & 0x1:
                raise ArchiveError("Gli archivi cifrati non sono supportati")
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise ArchiveError("L'archivio contiene collegamenti simbolici non consentiti")
        required = {"manifest.json", "checksums.sha256", "metadata/folders.jsonl", "metadata/messages.jsonl", "metadata/attachments.jsonl"}
        if not required.issubset(names):
            raise ArchiveError("Manifest o metadati mancanti")
        if bundle.getinfo("manifest.json").file_size > 1024 * 1024:
            raise ArchiveError("Manifest troppo grande")
        try:
            manifest = json.loads(bundle.read("manifest.json"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ArchiveError("Manifest non leggibile") from exc
        if manifest.get("format") != FORMAT_NAME:
            raise ArchiveError("Formato archivio non riconosciuto")
        if manifest.get("format_version") != FORMAT_VERSION:
            raise ArchiveError(f"Versione formato non supportata: {manifest.get('format_version')}")
        checksum_bytes = bundle.read("checksums.sha256")
        if hashlib.sha256(checksum_bytes).hexdigest() != manifest.get("checksums_sha256"):
            raise ArchiveError("Indice di integrità danneggiato")
        checksums: dict[str, str] = {}
        for raw_line in checksum_bytes.decode("utf-8").splitlines():
            digest, separator, name = raw_line.partition("  ")
            name = _safe_zip_name(name)
            if not separator or not name or not all(c in "0123456789abcdef" for c in digest) or len(digest) != 64:
                raise ArchiveError("Riga checksum non valida")
            if name in checksums:
                raise ArchiveError("Voce checksum duplicata")
            checksums[name] = digest
        expected_names = names - {"manifest.json", "checksums.sha256"}
        if set(checksums) != expected_names:
            raise ArchiveError("L'indice di integrità non corrisponde ai file presenti")
        for name, expected in checksums.items():
            digest = hashlib.sha256()
            with bundle.open(name) as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
            if digest.hexdigest() != expected:
                raise ArchiveError(f"Checksum non valido: {name}")
        return manifest, checksums


def _jsonl(bundle: zipfile.ZipFile, name: str):
    with bundle.open(name) as raw:
        for line_number, line in enumerate(raw, 1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ArchiveError(f"Metadati non validi in {name}, riga {line_number}") from exc


def import_archive(path: Path, owner_id: int) -> int:
    manifest, _checksums = validate_archive(path)
    account_uuid = str(uuid.uuid4())
    snapshot_uuid = str(uuid.uuid4())
    stage = snapshot_root(account_uuid, f".staging-{snapshot_uuid}")
    stage.mkdir(parents=True, exist_ok=False)
    db = SessionLocal()
    account: Account | None = None
    try:
        with zipfile.ZipFile(path, "r") as bundle:
            for info in bundle.infolist():
                if not info.filename.startswith("files/") or info.is_dir():
                    continue
                relpath = info.filename[len("files/"):]
                destination = safe_resolve(stage, relpath)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(info) as source, destination.open("wb") as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)

            account_meta = manifest.get("account") or {}
            account = Account(
                owner_id=owner_id,
                archive_uuid=account_uuid,
                display_name=str(account_meta.get("display_name") or "Archivio importato")[:200],
                email=str(account_meta.get("email") or "offline@local.invalid")[:320],
                imap_enabled=False,
                security="ssl",
                schedule_mode="disabled",
                last_backup_status="imported",
                mailbox_identity=hashlib.sha256(f"{owner_id}:{str(account_meta.get('email') or '').lower()}:import".encode()).hexdigest(),
            )
            db.add(account)
            db.flush()
            snapshot_meta = manifest.get("snapshot") or {}
            snapshot = Snapshot(
                account_id=account.id,
                snapshot_uuid=snapshot_uuid,
                status="staging",
                created_at=_parse_dt(snapshot_meta.get("created_at")) or utcnow(),
                completed_at=_parse_dt(snapshot_meta.get("completed_at")) or utcnow(),
                message_count=0,
                archive_size=0,
            )
            db.add(snapshot)
            db.flush()

            folder_map: dict[int, int] = {}
            for row in _jsonl(bundle, "metadata/folders.jsonl"):
                folder = Folder(
                    snapshot_id=snapshot.id,
                    name=str(row.get("name") or "")[:1000],
                    delimiter=(str(row["delimiter"])[:10] if row.get("delimiter") is not None else None),
                    flags_json=str(row.get("flags_json") or "[]"),
                    uidvalidity=(str(row["uidvalidity"])[:100] if row.get("uidvalidity") is not None else None),
                    message_count=int(row.get("message_count") or 0),
                )
                db.add(folder)
                db.flush()
                folder_map[int(row["export_id"])] = folder.id

            message_map: dict[int, int] = {}
            count = 0
            for row in _jsonl(bundle, "metadata/messages.jsonl"):
                folder_id = folder_map.get(int(row["folder_export_id"]))
                if not folder_id:
                    raise ArchiveError("Un messaggio fa riferimento a una cartella inesistente")
                relpath = str(row.get("raw_relpath") or "")
                if not safe_resolve(stage, relpath).is_file():
                    raise ArchiveError(f"EML mancante: {relpath}")
                item = Message(
                    snapshot_id=snapshot.id,
                    folder_id=folder_id,
                    imap_uid=str(row.get("imap_uid") or "")[:100],
                    message_id=row.get("message_id"),
                    in_reply_to=row.get("in_reply_to"),
                    references_json=str(row.get("references_json") or "[]"),
                    thread_key=str(row.get("thread_key") or "")[:1000],
                    subject=str(row.get("subject") or ""),
                    sender=str(row.get("sender") or ""),
                    recipients_to=str(row.get("recipients_to") or ""),
                    recipients_cc=str(row.get("recipients_cc") or ""),
                    recipients_bcc=str(row.get("recipients_bcc") or ""),
                    reply_to=str(row.get("reply_to") or ""),
                    date_utc=_parse_dt(row.get("date_utc")),
                    internal_date=_parse_dt(row.get("internal_date")),
                    headers_json=str(row.get("headers_json") or "[]"),
                    text_body=str(row.get("text_body") or ""),
                    html_body=str(row.get("html_body") or ""),
                    mime_json=str(row.get("mime_json") or "{}"),
                    flags_json=str(row.get("flags_json") or "[]"),
                    is_read=bool(row.get("is_read")),
                    is_starred=bool(row.get("is_starred")),
                    is_answered=bool(row.get("is_answered")),
                    has_attachments=bool(row.get("has_attachments")),
                    size=int(row.get("size") or 0),
                    raw_sha256=str(row.get("raw_sha256") or "")[:64],
                    raw_relpath=relpath[:500],
                )
                db.add(item)
                db.flush()
                message_map[int(row["export_id"])] = item.id
                recipients = " ".join((item.recipients_to, item.recipients_cc, item.recipients_bcc))
                db.execute(text(
                    "INSERT INTO message_fts(message_id,snapshot_id,subject,sender,recipients,body) "
                    "VALUES (:mid,:sid,:subject,:sender,:recipients,:body)"
                ), {"mid": item.id, "sid": snapshot.id, "subject": item.subject, "sender": item.sender,
                    "recipients": recipients, "body": item.text_body})
                count += 1
                if count % 500 == 0:
                    db.flush()

            for row in _jsonl(bundle, "metadata/attachments.jsonl"):
                message_id = message_map.get(int(row["message_export_id"]))
                if not message_id:
                    raise ArchiveError("Un allegato fa riferimento a un messaggio inesistente")
                relpath = str(row.get("relpath") or "")
                if not safe_resolve(stage, relpath).is_file():
                    raise ArchiveError(f"Allegato mancante: {relpath}")
                db.add(Attachment(
                    message_id=message_id,
                    filename=safe_filename(row.get("filename")),
                    content_type=str(row.get("content_type") or "application/octet-stream")[:255],
                    size=int(row.get("size") or 0),
                    sha256=str(row.get("sha256") or "")[:64],
                    relpath=relpath[:500],
                    content_id=(str(row["content_id"])[:1000] if row.get("content_id") else None),
                    is_inline=bool(row.get("is_inline")),
                ))

            archive_size = sum(item.stat().st_size for item in stage.rglob("*") if item.is_file())
            snapshot.message_count = count
            snapshot.archive_size = archive_size
            db.commit()

            final = snapshot_root(account_uuid, snapshot_uuid)
            stage.replace(final)
            snapshot.status = "completed"
            account.active_snapshot_id = snapshot.id
            account.message_count = count
            account.archive_size = archive_size
            account.last_backup_at = snapshot.completed_at
            db.commit()
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


def clear_account_archive(account_id: int) -> None:
    with SessionLocal() as db:
        account = db.get(Account, account_id)
        if not account:
            return
        snapshots = db.scalars(select(Snapshot).where(Snapshot.account_id == account.id)).all()
        snapshot_ids = [snapshot.id for snapshot in snapshots]
        account.active_snapshot_id = None
        account.message_count = 0
        account.archive_size = 0
        account.last_backup_status = "cleared"
        db.flush()
        for snapshot in snapshots:
            db.execute(text("DELETE FROM message_fts WHERE snapshot_id=:sid"), {"sid": snapshot.id})
            db.delete(snapshot)
        db.commit()
        if snapshot_ids:
            shutil.rmtree(ARCHIVES_DIR / account.archive_uuid / "snapshots", ignore_errors=True)


def delete_account(account_id: int) -> None:
    with SessionLocal() as db:
        account = db.get(Account, account_id)
        if not account:
            return
        archive_uuid = account.archive_uuid
        snapshot_ids = [row[0] for row in db.execute(select(Snapshot.id).where(Snapshot.account_id == account.id))]
        for sid in snapshot_ids:
            db.execute(text("DELETE FROM message_fts WHERE snapshot_id=:sid"), {"sid": sid})
        account.active_snapshot_id = None
        db.flush()
        db.delete(account)
        db.commit()
        shutil.rmtree(ARCHIVES_DIR / archive_uuid, ignore_errors=True)
