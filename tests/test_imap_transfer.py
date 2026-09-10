from __future__ import annotations

import re

from fastapi.testclient import TestClient

import app.main as main
from app.database import SessionLocal
from app.main import app
from app.imap_adapter import StandardIMAPAdapter
from app.models import Account, Folder, IMAPTransferJob, Snapshot, User, utcnow
from app.security import encrypt_secret, hash_password


def test_imap_append_preserves_original_rfc822_bytes():
    class Client:
        def __init__(self): self.calls = []
        def search(self, criteria): self.calls.append(("search", criteria)); return [1] if '"<existing@example>"' in criteria else []
        def append(self, folder, raw, flags=None, msg_time=None): self.calls.append(("append", folder, raw, flags, msg_time))

    adapter = StandardIMAPAdapter("imap.example.com", 993, "ssl", "user", "password")
    adapter.client = Client()
    original = b"From: sender@example.com\r\nMessage-ID: <new@example>\r\n\r\n\xfforiginal"
    assert adapter.has_message_id("<existing@example>") is True
    # Quoted, because a Message-ID is not a legal IMAP atom and Yahoo answers an unquoted one with
    # "[CLIENTBUG] UID SEARCH Command arguments invalid" — which used to end the whole restore.
    assert adapter.client.calls[0] == ("search", 'HEADER Message-ID "<existing@example>"')
    assert adapter.has_message_id("<odd@*>") is False
    assert adapter.client.calls[-1] == ("search", 'HEADER Message-ID "<odd@*>"')
    adapter.append_message("Archive/Inbox", original, ["\\Seen", "custom"], None)
    append = adapter.client.calls[-1]
    assert append[2] == original
    assert append[3] == ["\\Seen"]


def _csrf(client: TestClient) -> dict[str, str]:
    token = re.search(r'name="csrf-token" content="([^"]+)', client.get("/app").text).group(1)
    return {"X-CSRF-Token": token}


def test_imap_transfer_quota_test_and_tenant_safety(monkeypatch):
    with SessionLocal() as db:
        owner = User(username="transfer@example.com", email="transfer@example.com",
                     password_hash=hash_password("secure-transfer-password"), verified_at=utcnow())
        foreign_user = User(username="transfer-other@example.com", email="transfer-other@example.com",
                            password_hash=hash_password("secure-transfer-password"), verified_at=utcnow())
        db.add_all([owner, foreign_user]); db.flush()
        source = Account(owner_id=owner.id, archive_uuid="00000000-0000-0000-0000-000000000201",
                         display_name="Source", email="source@example.com", imap_enabled=True,
                         imap_host="imap.example.com", imap_port=993, security="ssl",
                         imap_username="source@example.com", encrypted_password=encrypt_secret("source-password"),
                         mailbox_identity="2" * 64)
        destination = Account(owner_id=owner.id, archive_uuid="00000000-0000-0000-0000-000000000202",
                              display_name="Destination", email="destination@example.com", imap_enabled=True,
                              imap_host="imap.example.com", imap_port=993, security="ssl",
                              imap_username="destination@example.com", encrypted_password=encrypt_secret("destination-password"),
                              mailbox_identity="3" * 64)
        foreign = Account(owner_id=foreign_user.id, archive_uuid="00000000-0000-0000-0000-000000000203",
                          display_name="Foreign", email="foreign-transfer@example.com", imap_enabled=True,
                          imap_host="imap.example.com", imap_port=993, security="ssl",
                          imap_username="foreign@example.com", encrypted_password=encrypt_secret("foreign-password"),
                          mailbox_identity="4" * 64)
        db.add_all([source, destination, foreign]); db.flush()
        snapshot = Snapshot(account_id=source.id, snapshot_uuid="00000000-0000-0000-0000-000000000204",
                            status="completed", completed_at=utcnow(), message_count=12, archive_size=4096)
        db.add(snapshot); db.flush()
        db.add(Folder(snapshot_id=snapshot.id, name="INBOX", message_count=12, flags_json="[]"))
        source.active_snapshot_id = snapshot.id
        db.commit()
        source_id, destination_id, foreign_id = source.id, destination.id, foreign.id

    submitted: list[int] = []
    monkeypatch.setattr(main, "test_imap_connection", lambda *_args: {"ok": True, "folders": 4, "capabilities": ["IMAP4REV1"]})
    monkeypatch.setattr(main.transfer_manager, "submit", submitted.append)
    with TestClient(app) as client:
        assert client.post("/api/login", json={"username":"transfer@example.com", "password":"secure-transfer-password"}).status_code == 200
        headers = _csrf(client)
        test = client.post("/api/imap-transfer/test", headers=headers, json={"destination":{"account_id":destination_id}})
        assert test.status_code == 200 and test.json()["quota_consumed"] is False
        assert client.post("/api/imap-transfer/test", headers=headers, json={"destination":{"account_id":foreign_id}}).status_code == 404
        # Folder names arrive from a form, so they are checked against the snapshot before a job
        # exists — and before the quota is spent on a restore that could not have run.
        unknown = client.post(f"/api/accounts/{source_id}/transfers", headers=headers, json={
            "destination":{"account_id":destination_id}, "mode":"preserve", "folders":["Inesistente"]})
        assert unknown.status_code == 422 and "Inesistente" in unknown.text
        backwards = client.post(f"/api/accounts/{source_id}/transfers", headers=headers, json={
            "destination":{"account_id":destination_id}, "mode":"preserve",
            "date_from":"2025-06-01T00:00:00", "date_to":"2025-01-01T00:00:00"})
        assert backwards.status_code == 422

        body = {"destination":{"account_id":destination_id}, "mode":"preserve", "skip_duplicates":True,
                "folders":["INBOX"], "date_from":"2020-01-01T00:00:00"}
        first = client.post(f"/api/accounts/{source_id}/transfers", headers=headers, json=body)
        second = client.post(f"/api/accounts/{source_id}/transfers", headers=headers, json=body)
        third = client.post(f"/api/accounts/{source_id}/transfers", headers=headers, json=body)
        assert first.status_code == 200 and second.status_code == 200
        assert third.status_code == 409 and "limite mensile" in third.text.lower()
        assert len(submitted) == 2
        listing = client.get("/api/imap-transfers").json()
        assert listing["quota"]["used"] == 2 and listing["quota"]["remaining"] == 0
        queued = listing["items"][0]
        assert queued["folders"] == ["INBOX"], "the chosen folder rides on the job"
        assert queued["date_from"].startswith("2020-01-01")

    with SessionLocal() as db:
        for job in db.query(IMAPTransferJob).filter(IMAPTransferJob.id.in_(submitted)).all():
            job.status = "cancelled"; job.cancel_requested = True; job.encrypted_password = None
        db.commit()


def test_a_transfer_can_be_limited_to_a_date_range():
    """"From this date to today": the archive holds everything, but a restore often should not.

    The window is applied to the archived date, so a message the archive has no date for cannot be
    shown to fall inside it and stays out — the preview reports how many those are beforehand.
    """
    from datetime import datetime

    from app.imap_transfer import _within_window
    from app.models import IMAPTransferJob

    unbounded = IMAPTransferJob(date_from=None, date_to=None)
    assert _within_window(unbounded) == [], "no window means the whole archive"

    assert len(_within_window(IMAPTransferJob(date_from=datetime(2024, 1, 1), date_to=None))) == 1
    assert len(_within_window(IMAPTransferJob(date_from=datetime(2024, 1, 1),
                                              date_to=datetime(2025, 1, 1)))) == 2


def test_a_restore_can_be_limited_to_chosen_folders():
    """"I only need Fatture back" used to mean restoring 44 000 messages to get 300."""
    import json as _json

    from app.models import IMAPTransferJob

    everything = IMAPTransferJob(folders_json="[]")
    assert set(_json.loads(everything.folders_json or "[]")) == set(), "empty means the whole archive"

    chosen = IMAPTransferJob(folders_json=_json.dumps(["Fatture", "Viaggi"]))
    assert set(_json.loads(chosen.folders_json)) == {"Fatture", "Viaggi"}


class FlakyTarget:
    """A destination that misbehaves the way the failing restore did."""

    def __init__(self, fail_on=(), search_raises=()):
        self.fail_on = set(fail_on)          # message ids whose APPEND blows up once
        self.search_raises = set(search_raises)
        self.delivered = []
        self.reconnects = 0
        self.attempted = []

    def has_message(self, message_id):
        if message_id in self.search_raises:
            # What Yahoo answers to a Message-ID it will not accept inside a SEARCH.
            raise RuntimeError("[CLIENTBUG] UID SEARCH Command arguments invalid")
        return False

    def deliver(self, raw, flags, internal_date):
        self.attempted.append(raw)
        if raw in self.fail_on:
            self.fail_on.discard(raw)
            raise TimeoutError("The read operation timed out")
        self.delivered.append(raw)

    def reconnect(self):
        self.reconnects += 1


def _message(tmp_path, index, body=b"ciao", message_id=None):
    from app.models import Message
    (tmp_path / f"{index}.eml").write_bytes(body)
    item = Message(id=index, folder_id=1, imap_uid=str(index), thread_key="t",
                   flags_json="[]", raw_relpath=f"{index}.eml", raw_sha256="x" * 64,
                   message_id=message_id)
    return item


def test_a_timeout_on_one_message_does_not_end_the_restore(tmp_path, monkeypatch):
    """The run that prompted this died at 1 728 of 7 266 and kept nothing."""
    import app.imap_transfer as transfer
    from app.models import Account, IMAPTransferJob, Snapshot

    monkeypatch.setattr(transfer, "snapshot_root", lambda *_a: tmp_path)
    monkeypatch.setattr(transfer.time, "sleep", lambda _s: None)
    account = Account(archive_uuid="u"); snapshot = Snapshot(snapshot_uuid="s")
    job = IMAPTransferJob(skip_duplicates=False)
    target = FlakyTarget(fail_on=[b"secondo"])

    first = transfer._deliver_one(target, job, account, snapshot, _message(tmp_path, 1, b"primo"))
    second = transfer._deliver_one(target, job, account, snapshot, _message(tmp_path, 2, b"secondo"))

    assert first is True and second is True, "the retry delivered it after reconnecting"
    assert target.delivered == [b"primo", b"secondo"]
    assert target.reconnects == 1


def test_a_message_id_the_server_rejects_does_not_end_the_restore(tmp_path, monkeypatch):
    """`[CLIENTBUG] UID SEARCH Command arguments invalid` is about one header, not the job."""
    import app.imap_transfer as transfer
    from app.models import Account, IMAPTransferJob, Snapshot

    monkeypatch.setattr(transfer, "snapshot_root", lambda *_a: tmp_path)
    monkeypatch.setattr(transfer.time, "sleep", lambda _s: None)
    account = Account(archive_uuid="u"); snapshot = Snapshot(snapshot_uuid="s")
    job = IMAPTransferJob(skip_duplicates=True)
    bad_id = "<ADR50000266814120@*>"
    target = FlakyTarget(search_raises=[bad_id])

    ok = transfer._deliver_one(target, job, account, snapshot,
                               _message(tmp_path, 1, b"corpo", message_id=bad_id))

    assert ok is True and target.delivered == [b"corpo"], "delivered rather than abandoned"


def test_a_message_missing_from_the_archive_is_counted_not_fatal(tmp_path, monkeypatch):
    import app.imap_transfer as transfer
    from app.models import Account, IMAPTransferJob, Message, Snapshot

    monkeypatch.setattr(transfer, "snapshot_root", lambda *_a: tmp_path)
    account = Account(archive_uuid="u"); snapshot = Snapshot(snapshot_uuid="s")
    absent = Message(id=9, folder_id=1, imap_uid="9", thread_key="t", flags_json="[]",
                     raw_relpath="non-esiste.eml", raw_sha256="x" * 64)

    assert transfer._deliver_one(FlakyTarget(), IMAPTransferJob(), account, snapshot, absent) is False
