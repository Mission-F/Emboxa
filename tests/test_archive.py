from email.message import EmailMessage
from pathlib import Path

from sqlalchemy import text

from app.archive import build_export, import_archive, validate_archive
from app.backup import snapshot_root
from app.database import SessionLocal
from app.mail_parser import parse_and_store
from app.main import list_messages
from app.migrations import run_migrations
from app.models import Account, Attachment, Folder, Message, Snapshot, User, utcnow
from app.security import hash_password


def test_mailvault_export_import_roundtrip():
    run_migrations()
    with SessionLocal() as db:
        user = User(username="roundtrip@example.com", email="roundtrip@example.com", password_hash=hash_password("password"), verified_at=utcnow(), plan="PLUS")
        db.add(user); db.flush()
        account = Account(owner_id=user.id, archive_uuid="11111111-1111-1111-1111-111111111111", display_name="Roundtrip", email="box@example.com", imap_enabled=False, mailbox_identity="roundtrip")
        db.add(account); db.flush()
        snapshot = Snapshot(account_id=account.id, snapshot_uuid="22222222-2222-2222-2222-222222222222", status="active", completed_at=utcnow())
        db.add(snapshot); db.flush()
        folder = Folder(snapshot_id=snapshot.id, name="INBOX", delimiter="/", message_count=1)
        db.add(folder); db.flush()
        root = snapshot_root(account.archive_uuid, snapshot.snapshot_uuid); root.mkdir(parents=True)
        mail = EmailMessage(); mail["From"]="sender@example.com"; mail["To"]="box@example.com"; mail["Subject"]="Portable"; mail["Message-ID"]="<portable@example.com>"; mail.set_content("Portable archive content"); mail.add_attachment(b"payload", maintype="application", subtype="octet-stream", filename="data.bin")
        parsed = parse_and_store(mail.as_bytes(), root)
        item = Message(snapshot_id=snapshot.id, folder_id=folder.id, imap_uid="1", message_id=parsed.message_id, in_reply_to=parsed.in_reply_to, references_json=parsed.references_json, thread_key=parsed.thread_key, subject=parsed.subject, sender=parsed.sender, recipients_to=parsed.recipients_to, recipients_cc="", recipients_bcc="", reply_to="", date_utc=parsed.date_utc, headers_json=parsed.headers_json, text_body=parsed.text_body, html_body=parsed.html_body, mime_json=parsed.mime_json, flags_json="[]", has_attachments=True, size=len(mail.as_bytes()), raw_sha256=parsed.raw_sha256, raw_relpath=parsed.raw_relpath)
        db.add(item); db.flush()
        for part in parsed.attachments: db.add(Attachment(message_id=item.id, filename=part.filename, content_type=part.content_type, size=part.size, sha256=part.sha256, relpath=part.relpath, content_id=part.content_id, is_inline=part.is_inline))
        db.execute(text("INSERT INTO message_fts(message_id,snapshot_id,subject,sender,recipients,body) VALUES (:m,:s,:a,:b,:c,:d)"),{"m":item.id,"s":snapshot.id,"a":item.subject,"b":item.sender,"c":item.recipients_to,"d":item.text_body})
        snapshot.message_count=1; snapshot.archive_size=sum(p.stat().st_size for p in root.rglob('*') if p.is_file()); account.active_snapshot_id=snapshot.id; account.message_count=1; account.archive_size=snapshot.archive_size; db.commit(); account_id=account.id; owner_id=user.id

    export_path, _name = build_export(account_id)
    manifest, checksums = validate_archive(export_path)
    assert manifest["format_version"] == 1
    assert checksums
    imported_id = import_archive(export_path, owner_id)
    with SessionLocal() as db:
        imported = db.get(Account, imported_id)
        assert imported.imap_enabled is False
        assert imported.message_count == 1
        message = db.query(Message).filter(Message.snapshot_id == imported.active_snapshot_id).one()
        assert "Portable archive content" in message.text_body
        imported_snapshot = db.get(Snapshot, imported.active_snapshot_id)
        assert (snapshot_root(imported.archive_uuid, imported_snapshot.snapshot_uuid) / message.raw_relpath).is_file()
        result = list_messages(
            imported_id, db=db, page=1, page_size=50, q="Portable", sender=None, recipient=None,
            subject=None, folder_id=None, date_from=None, date_to=None, has_attachments=None,
            is_read=None, is_starred=None, sort="date_desc",
        )
        assert result["total"] == 1
        assert result["items"][0]["subject"] == "Portable"
