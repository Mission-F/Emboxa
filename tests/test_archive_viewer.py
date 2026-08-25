from email.message import EmailMessage
import json
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app.database import SessionLocal
from app.mail_parser import parse_and_store
from app.main import app
from app.models import Account, ArchiveDeletionAudit, Attachment, Folder, Message, Snapshot, User, utcnow
from app.backup import snapshot_root
from app.security import hash_password


PNG = b"\x89PNG\r\n\x1a\n" + b"test-png"
PDF = b"%PDF-1.4\n% test\n%%EOF"


def _login_client():
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == "viewer"))
        if not user:
            db.add(User(username="viewer", password_hash=hash_password("viewer-test-password")))
            db.commit()
    client = TestClient(app)
    assert client.post("/api/login", json={"username": "viewer", "password": "viewer-test-password"}).status_code == 200
    page = client.get("/")
    import re
    csrf = re.search(r'name="csrf-token" content="([^"]+)', page.text).group(1)
    return client, {"X-CSRF-Token": csrf}


def _seed_html_archive():
    mail = EmailMessage()
    mail["From"] = "sender@example.com"
    mail["To"] = "box@example.com"
    mail["Subject"] = "HTML archive viewer"
    mail["Message-ID"] = "<html-viewer@example.com>"
    mail.set_content("Plain fallback")
    mail.add_alternative(
        "<html><head><style>.hero{color:#123456;width:620px}</style></head>"
        "<body><table class='hero'><tr><td>Newsletter</td></tr></table>"
        "<img src='cid:logo@example.com'><img src='https://tracker.example/pixel.png'>"
        "<script>window.parent.hacked=true</script></body></html>", subtype="html"
    )
    mail.get_payload()[1].add_related(PNG, maintype="image", subtype="png", cid="<logo@example.com>", filename="logo.png")
    mail.add_attachment(PNG, maintype="image", subtype="png", filename="photo.png")
    mail.add_attachment(PDF, maintype="application", subtype="pdf", filename="invoice.pdf")
    mail.add_attachment(b"hello,archive\n", maintype="text", subtype="plain", filename="notes.txt")

    with SessionLocal() as db:
        account = Account(archive_uuid=str(uuid.uuid4()), display_name="Viewer", email="box@example.com", imap_enabled=False)
        db.add(account); db.flush()
        snapshot = Snapshot(account_id=account.id, snapshot_uuid=str(uuid.uuid4()), status="completed",
                            completed_at=utcnow(), message_count=1, attachment_count=4)
        db.add(snapshot); db.flush()
        folder = Folder(snapshot_id=snapshot.id, name="INBOX", message_count=1)
        db.add(folder); db.flush()
        root = snapshot_root(account.archive_uuid, snapshot.snapshot_uuid); root.mkdir(parents=True, exist_ok=True)
        parsed = parse_and_store(mail.as_bytes(), root)
        message = Message(snapshot_id=snapshot.id, folder_id=folder.id, imap_uid="1", message_id=parsed.message_id,
                          in_reply_to=parsed.in_reply_to, references_json=parsed.references_json,
                          thread_key=parsed.thread_key, subject=parsed.subject, sender=parsed.sender,
                          recipients_to=parsed.recipients_to, recipients_cc="", recipients_bcc="", reply_to="",
                          date_utc=parsed.date_utc, headers_json=parsed.headers_json, text_body=parsed.text_body,
                          html_body=parsed.html_body, mime_json=parsed.mime_json, flags_json="[]", has_attachments=True,
                          size=len(mail.as_bytes()), raw_sha256=parsed.raw_sha256, raw_relpath=parsed.raw_relpath)
        db.add(message); db.flush()
        attachment_ids = {}
        for part in parsed.attachments:
            item = Attachment(message_id=message.id, filename=part.filename, content_type=part.content_type,
                              size=part.size, sha256=part.sha256, relpath=part.relpath,
                              content_id=part.content_id, is_inline=part.is_inline)
            db.add(item); db.flush(); attachment_ids[part.filename] = item.id
        db.execute(text("INSERT INTO message_fts(message_id,snapshot_id,subject,sender,recipients,body) VALUES (:m,:s,:a,:b,:c,:d)"),
                   {"m": message.id, "s": snapshot.id, "a": message.subject, "b": message.sender,
                    "c": message.recipients_to, "d": message.text_body})
        snapshot.archive_size = sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
        account.active_snapshot_id = snapshot.id; account.message_count = 1; account.archive_size = snapshot.archive_size
        db.commit()
        return account.id, snapshot.id, message.id, attachment_ids


def test_html_cid_attachment_viewers_and_local_delete():
    client, csrf = _login_client()
    account_id, snapshot_id, message_id, attachments = _seed_html_archive()

    rendered = client.get(f"/api/messages/{message_id}/render")
    assert rendered.status_code == 200
    assert f"/api/attachments/{attachments['logo.png']}?inline=1" in rendered.text
    assert "tracker.example" not in rendered.text and "<script" not in rendered.text
    assert "window.parent.hacked" not in rendered.text
    assert ".hero" in rendered.text and "sandbox allow-same-origin" in rendered.headers["content-security-policy"]
    remote = client.get(f"/api/messages/{message_id}/render?remote_images=1")
    assert "tracker.example" in remote.text and "http:" in remote.headers["content-security-policy"]

    listing = client.get(f"/api/accounts/{account_id}/attachments?snapshot_id={snapshot_id}")
    assert listing.status_code == 200, listing.text
    items = listing.json()["items"]
    assert {item["filename"] for item in items} == {"photo.png", "invoice.pdf", "notes.txt"}
    assert {item["category"] for item in items} == {"images", "pdf", "documents"}
    assert client.get(f"/api/accounts/{account_id}/attachments?snapshot_id={snapshot_id}&q=invoice").json()["total"] == 1
    assert client.get(f"/api/attachments/{attachments['notes.txt']}/text-preview").text == "hello,archive\n"
    pdf = client.get(f"/api/attachments/{attachments['invoice.pdf']}?inline=1")
    assert pdf.content.startswith(b"%PDF") and pdf.headers["x-frame-options"] == "SAMEORIGIN"
    image = client.get(f"/api/attachments/{attachments['photo.png']}?inline=1")
    assert image.content.startswith(b"\x89PNG") and image.headers["content-type"].startswith("image/png")

    assert client.post(f"/api/messages/{message_id}/trash", headers=csrf).status_code == 200
    assert client.get(f"/api/accounts/{account_id}/messages?snapshot_id={snapshot_id}").json()["total"] == 0
    assert client.get(f"/api/accounts/{account_id}/messages?snapshot_id={snapshot_id}&trash=true").json()["total"] == 1
    assert client.get(f"/api/accounts/{account_id}/attachments?snapshot_id={snapshot_id}").json()["total"] == 0
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(text("message_fts")).where(text("message_id=:mid")).params(mid=message_id)) == 0
    assert client.post(f"/api/messages/{message_id}/restore", headers=csrf).status_code == 200
    assert client.get(f"/api/accounts/{account_id}/attachments?snapshot_id={snapshot_id}").json()["total"] == 3
    client.post(f"/api/messages/{message_id}/trash", headers=csrf)
    assert client.delete(f"/api/messages/{message_id}/permanent", headers=csrf).status_code == 200
    with SessionLocal() as db:
        assert db.get(Message, message_id) is None
        actions = set(db.scalars(select(ArchiveDeletionAudit.action).where(ArchiveDeletionAudit.snapshot_id == snapshot_id)).all())
        assert actions == {"trash", "restore", "permanent"}
