from __future__ import annotations

import threading
import uuid
from datetime import datetime

from sqlalchemy import select

import app.backup as backup
from app.database import SessionLocal
from app.imap_adapter import BODY_MISSING_FLAG, RemoteFolder, RemoteMessage
from app.migrations import run_migrations
from app.models import Account, BackupJob, Message, Snapshot, User, utcnow
from app.security import encrypt_secret, hash_password


class FakeMailbox:
    """A whole IMAP server in memory, shared by every session the backup opens against it.

    UID 3 in Inbox is refused the way Yahoo refuses a broken message — always, on every session —
    but its headers are still served, so the archive should end up with every message and one
    of them marked as missing its body.
    """

    def __init__(self):
        self.folders = {
            "Inbox": {uid: f"From: a@b.c\r\nSubject: inbox {uid}\r\n\r\nbody {uid}\r\n".encode() for uid in range(1, 46)},
            "Sent": {uid: f"From: me@b.c\r\nSubject: sent {uid}\r\n\r\nbody {uid}\r\n".encode() for uid in range(1, 8)},
        }
        self.refused = {("Inbox", 3)}
        self.sessions = 0
        self.peak = 0
        self.active = 0
        self.lock = threading.Lock()

    def session(self):
        with self.lock:
            self.sessions += 1
        return FakeSession(self)


class FakeSession:
    def __init__(self, mailbox: FakeMailbox):
        self.mailbox = mailbox
        self.folder = None
        self.refresh_token = None

    def connect(self):
        pass

    def list_folders(self, _root=None):
        return [RemoteFolder(flags=[], delimiter="/", name=name) for name in self.mailbox.folders]

    def select_folder(self, name):
        self.folder = name
        return "77", len(self.mailbox.folders[name])

    def message_uids(self, expected=None):
        return sorted(self.mailbox.folders[self.folder])

    def fetch_messages(self, uids):
        with self.mailbox.lock:
            self.mailbox.active += 1
            self.mailbox.peak = max(self.mailbox.peak, self.mailbox.active)
        try:
            threading.Event().wait(0.01)  # a little network latency, so overlap is observable
            if any((self.folder, uid) in self.mailbox.refused for uid in uids):
                raise RuntimeError("[UNAVAILABLE] UID FETCH Server error - Please try again later")
            for uid in uids:
                yield RemoteMessage(uid=uid, raw=self.mailbox.folders[self.folder][uid],
                                    flags=["\\Seen"], internal_date=datetime(2015, 1, 5))
        finally:
            with self.mailbox.lock:
                self.mailbox.active -= 1

    def fetch_headers_only(self, uid):
        raw = self.mailbox.folders[self.folder][uid].split(b"\r\n\r\n")[0] + b"\r\nX-Emboxa-Body-Unavailable: yes\r\n\r\n(corpo non fornito)\r\n"
        return RemoteMessage(uid=uid, raw=raw, flags=["\\Seen", BODY_MISSING_FLAG],
                             internal_date=datetime(2015, 1, 5), body_missing=True)

    def message_summary(self, uid):
        return f"inbox {uid}"

    def is_alive(self):
        return True

    def logout(self):
        pass


def _queued_job():
    with SessionLocal() as db:
        tag = uuid.uuid4().hex[:8]
        owner = User(username=f"e2e-{tag}@example.com", email=f"e2e-{tag}@example.com",
                     password_hash=hash_password("password"), verified_at=utcnow(), plan="PLUS")
        db.add(owner); db.flush()
        account = Account(owner_id=owner.id, archive_uuid=str(uuid.uuid4()), display_name="E2E",
                          email=f"box-{tag}@example.com", imap_enabled=True,
                          encrypted_password=encrypt_secret("pw"), retention_versions=3,
                          mailbox_identity=uuid.uuid4().hex)
        db.add(account); db.flush()
        job = BackupJob(account_id=account.id, status="queued")
        db.add(job); db.commit()
        return job.id, account.id


def test_a_whole_backup_over_several_sessions(monkeypatch):
    run_migrations()
    mailbox = FakeMailbox()
    monkeypatch.setattr(backup, "_connect", lambda _account, _password: mailbox.session())
    monkeypatch.setattr(backup, "BACKUP_CONNECTIONS", 3)
    monkeypatch.setattr(backup, "IMAP_FETCH_BATCH", 4)
    monkeypatch.setattr(backup.time, "sleep", lambda _s: None)
    job_id, account_id = _queued_job()

    backup.run_backup(job_id)

    with SessionLocal() as db:
        job = db.get(BackupJob, job_id)
        account = db.get(Account, account_id)
        assert job.status == "completed", job.error
        assert job.processed_messages == 45 + 7, "every message, the refused one included"
        assert "1 salvati senza il corpo" in job.error
        assert "non scaricati" not in job.error
        snapshot = db.get(Snapshot, account.active_snapshot_id)
        assert snapshot.status == "completed" and snapshot.message_count == 52
        stub = db.scalar(select(Message).where(Message.snapshot_id == snapshot.id, Message.imap_uid == "3"))
        assert stub is not None and stub.subject == "inbox 3"
        assert BODY_MISSING_FLAG in stub.flags_json
        others = db.scalars(select(Message).where(Message.snapshot_id == snapshot.id)).all()
        assert len({(m.folder_id, m.imap_uid) for m in others}) == 52, "no duplicates from the second pass"
        assert backup.snapshot_root(account.archive_uuid, snapshot.snapshot_uuid).is_dir()

    assert mailbox.sessions == 3, "one session per connection, the first reused from the pre-pass"
    assert mailbox.peak >= 2, "batches really were in flight at the same time"


def test_cancelling_mid_way_leaves_nothing_behind(monkeypatch):
    run_migrations()
    mailbox = FakeMailbox()
    monkeypatch.setattr(backup, "_connect", lambda _account, _password: mailbox.session())
    monkeypatch.setattr(backup, "BACKUP_CONNECTIONS", 2)
    monkeypatch.setattr(backup, "IMAP_FETCH_BATCH", 4)
    monkeypatch.setattr(backup.time, "sleep", lambda _s: None)
    job_id, account_id = _queued_job()

    real_check, calls = backup._check_cancel, []

    def cancelling_check(db, job):
        calls.append(1)
        if len(calls) == 6:  # a few batches in: the user pressed "Interrompi"
            job.cancel_requested = True
            db.commit()
        return real_check(db, job)

    monkeypatch.setattr(backup, "_check_cancel", cancelling_check)

    backup.run_backup(job_id)

    with SessionLocal() as db:
        job = db.get(BackupJob, job_id)
        account = db.get(Account, account_id)
        assert job.status == "cancelled"
        assert account.active_snapshot_id is None
        assert db.scalar(select(Snapshot).where(Snapshot.account_id == account_id)) is None
        staging = backup.snapshot_root(account.archive_uuid, "").parent
        assert not any(p.name.startswith(".staging-") for p in staging.iterdir()) if staging.exists() else True
