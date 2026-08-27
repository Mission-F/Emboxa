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
        def search(self, criteria): self.calls.append(("search", criteria)); return [1] if criteria[-1] == "<existing@example>" else []
        def append(self, folder, raw, flags=None, msg_time=None): self.calls.append(("append", folder, raw, flags, msg_time))

    adapter = StandardIMAPAdapter("imap.example.com", 993, "ssl", "user", "password")
    adapter.client = Client()
    original = b"From: sender@example.com\r\nMessage-ID: <new@example>\r\n\r\n\xfforiginal"
    assert adapter.has_message_id("<existing@example>") is True
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
        body = {"destination":{"account_id":destination_id}, "mode":"preserve", "skip_duplicates":True}
        first = client.post(f"/api/accounts/{source_id}/transfers", headers=headers, json=body)
        second = client.post(f"/api/accounts/{source_id}/transfers", headers=headers, json=body)
        third = client.post(f"/api/accounts/{source_id}/transfers", headers=headers, json=body)
        assert first.status_code == 200 and second.status_code == 200
        assert third.status_code == 409 and "limite mensile" in third.text.lower()
        assert len(submitted) == 2
        listing = client.get("/api/imap-transfers").json()
        assert listing["quota"]["used"] == 2 and listing["quota"]["remaining"] == 0

    with SessionLocal() as db:
        for job in db.query(IMAPTransferJob).filter(IMAPTransferJob.id.in_(submitted)).all():
            job.status = "cancelled"; job.cancel_requested = True; job.encrypted_password = None
        db.commit()
