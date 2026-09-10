from __future__ import annotations

import uuid

from sqlalchemy import event, func, select, text

from app.archive import clear_account_archive, delete_account
from app.backup import snapshot_root
from app.config import ARCHIVES_DIR
from app.database import SessionLocal, engine
from app.migrations import run_migrations
from app.models import Account, Attachment, Folder, Message, Snapshot, User, utcnow
from app.security import hash_password


def _archive(messages: int) -> tuple[int, str, int]:
    """An account with a snapshot, a folder, and `messages` messages with one attachment each."""
    tag = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        user = User(username=f"del-{tag}@example.com", email=f"del-{tag}@example.com",
                    password_hash=hash_password("password"), verified_at=utcnow(), plan="PLUS")
        db.add(user); db.flush()
        account = Account(owner_id=user.id, archive_uuid=str(uuid.uuid4()), display_name="Delete",
                          email=f"box-{tag}@example.com", imap_enabled=False, mailbox_identity=tag)
        db.add(account); db.flush()
        snapshot = Snapshot(account_id=account.id, snapshot_uuid=str(uuid.uuid4()),
                            status="completed", completed_at=utcnow(), message_count=messages)
        db.add(snapshot); db.flush()
        folder = Folder(snapshot_id=snapshot.id, name="INBOX", delimiter="/", message_count=messages)
        db.add(folder); db.flush()
        for index in range(messages):
            item = Message(snapshot_id=snapshot.id, folder_id=folder.id, imap_uid=str(index),
                           thread_key=f"t{index}", subject=f"Subject {index}", sender="a@b.c",
                           recipients_to="box@example.com", recipients_cc="", recipients_bcc="",
                           reply_to="", headers_json="{}", text_body="body", flags_json="[]",
                           has_attachments=True, size=10, raw_sha256=f"{index:064d}",
                           raw_relpath=f"raw/{index}")
            db.add(item); db.flush()
            db.add(Attachment(message_id=item.id, filename="a.bin", content_type="application/octet-stream",
                              size=1, sha256=f"{index:064d}", relpath=f"attachments/{index}"))
            db.execute(text("INSERT INTO message_fts(message_id,snapshot_id,subject,sender,recipients,body) "
                            "VALUES (:m,:s,:a,:b,:c,:d)"),
                       {"m": item.id, "s": snapshot.id, "a": item.subject, "b": "a@b.c", "c": "", "d": "body"})
        account.active_snapshot_id = snapshot.id
        account.message_count = messages
        root = snapshot_root(account.archive_uuid, snapshot.snapshot_uuid)
        (root / "raw").mkdir(parents=True); (root / "attachments").mkdir(parents=True)
        for index in range(messages):
            (root / "raw" / str(index)).write_bytes(b"raw")
            (root / "attachments" / str(index)).write_bytes(b"att")
        db.commit()
        return account.id, account.archive_uuid, snapshot.id


def _leftovers(db, account_id: int, snapshot_id: int) -> tuple[int, int, int, int, int]:
    """What this account still has in the database. Scoped: other tests share the database."""
    return (
        db.scalar(select(func.count(Snapshot.id)).where(Snapshot.account_id == account_id)),
        db.scalar(select(func.count(Message.id)).where(Message.snapshot_id == snapshot_id)),
        db.scalar(select(func.count(Folder.id)).where(Folder.snapshot_id == snapshot_id)),
        db.scalar(select(func.count(Attachment.id)).join(Message).where(Message.snapshot_id == snapshot_id)),
        db.scalar(text("SELECT count(*) FROM message_fts WHERE snapshot_id=:sid").bindparams(sid=snapshot_id)),
    )


class _Counter:
    """Counts the SQL statements a block issues, so a per-row cascade cannot creep back in."""

    def __init__(self):
        self.statements: list[str] = []

    def __enter__(self):
        event.listen(engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *_exc):
        event.remove(engine, "before_cursor_execute", self._record)

    def _record(self, _conn, _cursor, statement, _params, _context, _many):
        self.statements.append(statement)


def test_clearing_an_archive_does_not_grow_with_the_number_of_messages():
    """The regression that made the button look dead on a 45 000-message mailbox.

    cascade="all, delete-orphan" without passive_deletes made SQLAlchemy load every Message, then
    query each one's attachments, then delete them one by one. The database can cascade the whole
    thing itself, so the work must stay flat.
    """
    run_migrations()
    small_id, _uuid, _sid = _archive(messages=3)
    big_id, _uuid, _big_sid = _archive(messages=60)

    with _Counter() as small:
        clear_account_archive(small_id)
    with _Counter() as big:
        clear_account_archive(big_id)

    assert len(big.statements) == len(small.statements), (
        f"twenty times the messages should not mean more statements: "
        f"{len(small.statements)} vs {len(big.statements)}")
    assert not any(statement.startswith("DELETE FROM messages ") for statement in big.statements), \
        "messages go with their snapshot, by foreign key"


def test_clearing_an_archive_removes_everything():
    run_migrations()
    account_id, archive_uuid, snapshot_id = _archive(messages=5)

    clear_account_archive(account_id)

    with SessionLocal() as db:
        account = db.get(Account, account_id)
        assert account is not None, "the account stays; only its archive goes"
        assert account.active_snapshot_id is None
        assert account.message_count == 0 and account.archive_size == 0
        assert account.last_backup_status == "cleared"
        assert _leftovers(db, account_id, snapshot_id) == (0, 0, 0, 0, 0)
    assert not (ARCHIVES_DIR / archive_uuid / "snapshots").exists()


def test_deleting_an_account_removes_it_and_its_files():
    run_migrations()
    account_id, archive_uuid, snapshot_id = _archive(messages=5)

    delete_account(account_id)

    with SessionLocal() as db:
        assert db.get(Account, account_id) is None
        assert _leftovers(db, account_id, snapshot_id) == (0, 0, 0, 0, 0)
    assert not (ARCHIVES_DIR / archive_uuid).exists()


def test_progress_reaches_the_end():
    run_migrations()
    account_id, _uuid, _sid = _archive(messages=4)
    seen: list[tuple[int, str]] = []

    clear_account_archive(account_id, progress=lambda percent, detail: seen.append((percent, detail)))

    assert seen[-1][0] == 100
    assert [percent for percent, _ in seen] == sorted(percent for percent, _ in seen), \
        "the bar must never go backwards"
