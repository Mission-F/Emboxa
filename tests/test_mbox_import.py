from email.message import EmailMessage

from sqlalchemy import text

from app.backup import snapshot_root
from app.database import SessionLocal
from app.mbox_import import MboxSource, folder_name_from_upload, import_mbox_sources
from app.migrations import run_migrations
from app.models import Account, Message, User, utcnow
from app.security import hash_password


def _message(subject: str, body: str) -> bytes:
    mail = EmailMessage()
    mail["From"] = "sender@example.com"
    mail["To"] = "box@example.com"
    mail["Subject"] = subject
    mail["Message-ID"] = f"<{subject.lower().replace(' ', '-')}-mbox@example.com>"
    mail.set_content(body)
    return mail.as_bytes()


def test_folder_name_from_apple_mail_mbox_path():
    assert folder_name_from_upload("luiss mail/Posta in arrivo.mbox/mbox") == "Posta in arrivo"
    assert folder_name_from_upload("Archive.mbox") == "Archive"


def test_import_mbox_sources_creates_offline_account(tmp_path):
    run_migrations()
    mbox = tmp_path / "mbox"
    mbox.write_bytes(
        b"From sender@example.com Wed Aug 26 10:00:00 2026\n"
        + _message("Primo messaggio", "Corpo uno")
        + b"\nFrom sender@example.com Wed Aug 26 10:01:00 2026\n"
        + _message("Secondo messaggio", "Corpo due")
    )
    with SessionLocal() as db:
        user = User(
            username="mbox-plus@example.com",
            email="mbox-plus@example.com",
            password_hash=hash_password("password"),
            verified_at=utcnow(),
            plan="PLUS",
        )
        db.add(user)
        db.commit()
        user_id = user.id

    account_id = import_mbox_sources(
        [MboxSource(mbox, "luiss mail/Posta in arrivo.mbox/mbox", "Posta in arrivo")],
        user_id,
        display_name="Luiss Mail",
        email="luiss@local.invalid",
    )

    with SessionLocal() as db:
        account = db.get(Account, account_id)
        assert account.auth_provider == "mbox"
        assert account.imap_enabled is False
        assert account.message_count == 2
        messages = db.query(Message).filter(Message.snapshot_id == account.active_snapshot_id).order_by(Message.id).all()
        assert [message.subject for message in messages] == ["Primo messaggio", "Secondo messaggio"]
        assert "Corpo due" in messages[1].text_body
        assert db.execute(text("SELECT count(*) FROM message_fts WHERE snapshot_id=:sid"), {"sid": account.active_snapshot_id}).scalar_one() == 2
        root = snapshot_root(account.archive_uuid, account.snapshots[0].snapshot_uuid)
        assert (root / messages[0].raw_relpath).is_file()
