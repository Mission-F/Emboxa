from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

import app.pec as pec
from app.backup import snapshot_root, store_remote_message
from app.database import SessionLocal
from app.imap_adapter import RemoteFolder, RemoteMessage
from app.mail_parser import pec_classify
from app.main import app
from app.migrations import run_migrations
from app.models import Account, Folder, Message, Snapshot, User, utcnow
from app.security import encrypt_secret, hash_password
from tests.test_web import login

ORIGINAL = (b"From: me@pec.example.it\r\nTo: ente@pec.example.it\r\nSubject: Contratto\r\n"
            b"Message-ID: <orig@pec.example.it>\r\nDate: Mon, 14 Sep 2026 09:00:00 +0200\r\n\r\nIn allegato.\r\n")
RECEIPT = (b"From: posta-certificata@pec.aruba.it\r\nTo: me@pec.example.it\r\n"
           b"Subject: ACCETTAZIONE: Contratto\r\nMessage-ID: <r1@pec.aruba.it>\r\n"
           b"X-Ricevuta: accettazione\r\nX-Riferimento-Message-ID: <orig@pec.example.it>\r\n"
           b"Date: Mon, 14 Sep 2026 09:00:05 +0200\r\n\r\nIl messaggio e' stato accettato.\r\n")


def _pec_mailbox(plan="PLUS"):
    tag = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        user = User(username=f"pec-{tag}@example.com", email=f"pec-{tag}@example.com",
                    password_hash=hash_password("secure-pec-password"), verified_at=utcnow(), plan=plan)
        db.add(user); db.flush()
        account = Account(owner_id=user.id, archive_uuid=str(uuid.uuid4()), display_name="Studio",
                          email=f"me-{tag}@pec.example.it", imap_enabled=True, imap_host="imap.example.it",
                          imap_port=993, imap_username="me", encrypted_password=encrypt_secret("pw"),
                          mailbox_identity=tag, is_pec=True, smtp_host="smtp.example.it", smtp_port=465,
                          smtp_username="me", encrypted_smtp_password=encrypt_secret("pw"))
        db.add(account); db.flush()
        snapshot = Snapshot(account_id=account.id, snapshot_uuid=str(uuid.uuid4()), status="completed",
                            completed_at=utcnow(), message_count=1)
        db.add(snapshot); db.flush()
        folder = Folder(snapshot_id=snapshot.id, name="INBOX", delimiter="/", uidvalidity="7",
                        message_count=1, remote_count=1)
        db.add(folder); db.flush()
        root = snapshot_root(account.archive_uuid, snapshot.snapshot_uuid)
        root.mkdir(parents=True, exist_ok=True)
        store_remote_message(db, snapshot.id, folder.id, root, RemoteMessage(1, ORIGINAL, ["\\Seen"], None))
        account.active_snapshot_id = snapshot.id
        db.commit()
        return account.id, snapshot.id, user.email


def test_receipts_are_recognised_and_threaded_with_what_they_certify():
    kind, reference = pec_classify([("X-Ricevuta", "avvenuta-consegna"),
                                    ("X-Riferimento-Message-ID", " <orig@pec.example.it> ")])
    assert (kind, reference) == ("consegna", "<orig@pec.example.it>")
    assert pec_classify([("X-Trasporto", "posta-certificata")]) == ("certificata", None)
    assert pec_classify([("Subject", "normale")]) == (None, None)


def test_a_pec_carries_the_chosen_receipt_type():
    message = pec.build_pec("me@pec.example.it", "Studio", ["a@pec.it"], ["b@pec.it"], "Oggetto\r\nBcc: x@y.z",
                            "Testo", [("atto.pdf", "application/pdf", b"%PDF")], "breve")
    assert message["X-TipoRicevuta"] == "breve"
    assert message["Subject"] == "Oggetto Bcc: x@y.z", "a newline in a subject must not become a header"
    assert message["Message-ID"].endswith("@pec.example.it>")
    assert [part.get_filename() for part in message.iter_attachments()] == ["atto.pdf"]


class FakeSMTP:
    def __init__(self): self.sent = []
    def send_message(self, message, from_addr=None, to_addrs=None):
        self.sent.append((message, from_addr, to_addrs)); return {}
    def quit(self): pass
    def close(self): pass


class FakeImap:
    def __init__(self, fail_append=False):
        self.fail_append, self.appended, self.selected = fail_append, [], []
    def list_folders(self, root=None):
        return [RemoteFolder(["\\HasNoChildren"], "/", "INBOX"), RemoteFolder(["\\Sent"], "/", "Posta inviata")]
    def select_write_folder(self, name): self.selected.append(name)
    def has_message_id(self, message_id): return False
    def append_message(self, folder, raw, flags=None, internal_date=None):
        if self.fail_append:
            raise TimeoutError("read timed out")
        self.appended.append((folder, raw, flags))
    def logout(self): pass


def test_sending_goes_through_the_provider_and_is_filed_under_sent(monkeypatch):
    run_migrations()
    account_id, _snapshot_id, _email = _pec_mailbox()
    smtp, imap = FakeSMTP(), FakeImap()
    monkeypatch.setattr(pec, "smtp_connect", lambda *_a: smtp)
    monkeypatch.setattr(pec, "_connect_with_retry", lambda _a, _p: imap)

    result = pec.send_pec(account_id, "a@pec.it; b@pec.it", "c@pec.it", "Diffida", "Testo",
                          [("atto.pdf", "application/pdf", b"%PDF")], "sintetica")

    message, _sender, recipients = smtp.sent[0]
    assert recipients == ["a@pec.it", "b@pec.it", "c@pec.it"], "semicolons are separators too"
    assert message["X-TipoRicevuta"] == "sintetica"
    assert imap.appended[0][0] == "Posta inviata", "found by its \\Sent flag, whatever it is called"
    assert result["saved_to_sent"] is True and result["refused"] == []


def test_a_sent_pec_is_never_reported_as_failed_because_filing_the_copy_failed(monkeypatch):
    """Once the provider accepted it the PEC is sent, legally. Saying otherwise invites a resend."""
    run_migrations()
    account_id, _snapshot_id, _email = _pec_mailbox()
    monkeypatch.setattr(pec, "smtp_connect", lambda *_a: FakeSMTP())
    monkeypatch.setattr(pec, "_connect_with_retry", lambda _a, _p: FakeImap(fail_append=True))

    result = pec.send_pec(account_id, "a@pec.it", "", "Oggetto", "Testo", [], "completa")

    assert result["saved_to_sent"] is False and result["message_id"]


class SyncImap:
    """INBOX gains a receipt; Archivio was recreated on the server, so its UIDs mean something else."""

    def __init__(self):
        self.folders = {"INBOX": ("7", {1: ORIGINAL, 2: RECEIPT}), "Archivio": ("99", {1: ORIGINAL})}
        self.current, self.fetched = None, []
    def list_folders(self, root=None): return [RemoteFolder([], "/", name) for name in self.folders]
    def select_folder(self, name):
        self.current = name
        return self.folders[name][0], len(self.folders[name][1])
    def message_uids(self, expected=None): return sorted(self.folders[self.current][1])
    def fetch_messages(self, uids):
        self.fetched.extend((self.current, uid) for uid in uids)
        for uid in uids:
            yield RemoteMessage(uid, self.folders[self.current][1][uid], [], None)
    def is_alive(self): return True
    def logout(self): pass


def test_refresh_downloads_only_what_the_archive_does_not_hold(monkeypatch):
    run_migrations()
    account_id, snapshot_id, _email = _pec_mailbox()
    with SessionLocal() as db:
        db.add(Folder(snapshot_id=snapshot_id, name="Archivio", delimiter="/", uidvalidity="3"))
        db.commit()
    server = SyncImap()
    monkeypatch.setattr(pec, "_connect_with_retry", lambda _a, _p: server)

    result = pec.sync_new_messages(account_id)

    assert server.fetched == [("INBOX", 2)], "UID 1 was already archived; Archivio was not compared"
    assert result == {"added": 1, "skipped_folders": ["Archivio"]}
    with SessionLocal() as db:
        receipt = db.query(Message).filter_by(snapshot_id=snapshot_id, imap_uid="2").one()
        original = db.query(Message).filter_by(snapshot_id=snapshot_id, imap_uid="1").one()
        assert receipt.pec_kind == "accettazione"
        assert receipt.thread_key == original.thread_key, "the receipt sits in the original's conversation"
        assert db.get(Snapshot, snapshot_id).message_count == 2


def test_pec_is_plus_only_and_refuses_an_unknown_receipt_type(monkeypatch):
    run_migrations()
    _account_id, _snapshot_id, standard_email = _pec_mailbox(plan="STANDARD")
    with TestClient(app) as client:
        headers = login(client, standard_email, "secure-pec-password")
        created = client.post("/api/accounts", headers=headers, json={
            "display_name": "PEC", "email": "x@pec.example.it", "imap_host": "imap.example.it", "imap_port": 993,
            "imap_username": "x", "password": "pw", "is_pec": True, "smtp_host": "smtp.example.it", "smtp_port": 465})
        assert created.status_code == 403

    plus_account, _snapshot, plus_email = _pec_mailbox()
    monkeypatch.setattr(pec, "smtp_connect", lambda *_a: pytest.fail("must not send"))
    with TestClient(app) as client:
        headers = login(client, plus_email, "secure-pec-password")
        response = client.post(f"/api/accounts/{plus_account}/pec/send", headers=headers,
                               data={"to": "a@pec.it", "subject": "x", "receipt_type": "raccomandata"})
        assert response.status_code == 422
