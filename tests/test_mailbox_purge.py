from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

import app.mailbox_purge as purge
from app.database import SessionLocal
from app.main import app
from app.migrations import run_migrations
from app.models import Account, Folder, Message, Snapshot, User, utcnow
from app.security import hash_password
from tests.test_web import login  # the suite's existing session helper


def _mailbox(plan="PLUS", archived=3, remote=3):
    """An account with an archived folder, ready to be emptied on the server."""
    tag = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        user = User(username=f"purge-{tag}@example.com", email=f"purge-{tag}@example.com",
                    password_hash=hash_password("secure-purge-password"), verified_at=utcnow(),
                    plan=plan)
        db.add(user); db.flush()
        account = Account(owner_id=user.id, archive_uuid=str(uuid.uuid4()), display_name="Casella",
                          email=f"box-{tag}@example.com", imap_enabled=True,
                          encrypted_password="x", mailbox_identity=tag, imap_host="imap.example.com")
        db.add(account); db.flush()
        snapshot = Snapshot(account_id=account.id, snapshot_uuid=str(uuid.uuid4()),
                            status="completed", completed_at=utcnow(), message_count=archived)
        db.add(snapshot); db.flush()
        folder = Folder(snapshot_id=snapshot.id, name="Inbox", delimiter="/",
                        message_count=archived, remote_count=remote)
        db.add(folder); db.flush()
        for index in range(archived):
            db.add(Message(snapshot_id=snapshot.id, folder_id=folder.id, imap_uid=str(1000 + index),
                           thread_key=f"t{index}", subject=f"Messaggio {index}", sender="a@b.c",
                           recipients_to="", recipients_cc="", recipients_bcc="", reply_to="",
                           headers_json="{}", text_body="corpo", flags_json="[]",
                           raw_sha256=f"{index:064d}", raw_relpath=f"raw/{index}"))
        account.active_snapshot_id = snapshot.id
        db.commit()
        return account.id, user.email


class FakeServer:
    """A mailbox holding what the archive knows plus one message that arrived afterwards."""

    def __init__(self, uids):
        self.uids = list(uids)
        self.deleted: list[int] = []
        self.batches: list[int] = []
        self.selected_write: list[str] = []

    def select_write_folder(self, name):
        self.selected_write.append(name)

    def select_folder(self, name):
        return "1", len(self.uids)

    def message_uids(self, expected=None):
        return list(self.uids)

    def delete_uids(self, uids):
        self.batches.append(len(uids))
        self.deleted.extend(uids)
        self.uids = [uid for uid in self.uids if uid not in set(uids)]
        return len(uids)

    def logout(self):
        pass


def test_only_messages_the_archive_holds_are_deleted(monkeypatch):
    """The guarantee the checkboxes cannot give.

    UID 9999 reached the mailbox after the backup, so no copy of it exists anywhere. Emptying the
    folder must leave it where it is: the point is to reclaim space, not to destroy the one copy
    of a message nobody has read.
    """
    run_migrations()
    account_id, _email = _mailbox()
    server = FakeServer([1000, 1001, 1002, 9999])
    monkeypatch.setattr(purge, "_connect_with_retry", lambda _a, _p: server)
    monkeypatch.setattr(purge, "decrypt_secret", lambda _v: "pw")

    result = purge.purge_folder(account_id, "Inbox")

    assert sorted(server.deleted) == [1000, 1001, 1002]
    assert result == {"deleted": 3, "untouched": 1, "folder": "Inbox"}
    assert server.uids == [9999], "the message that arrived later is still on the server"
    assert server.selected_write == ["Inbox", "Inbox"], "opened read-write, never read-only"


def test_deletion_goes_in_batches(monkeypatch):
    """A single STORE of 38 000 UIDs is how a connection times out half way through."""
    run_migrations()
    account_id, _email = _mailbox(archived=450, remote=450)
    server = FakeServer(list(range(1000, 1450)))
    monkeypatch.setattr(purge, "_connect_with_retry", lambda _a, _p: server)
    monkeypatch.setattr(purge, "decrypt_secret", lambda _v: "pw")

    purge.purge_folder(account_id, "Inbox")

    assert server.batches == [200, 200, 50]


def test_an_incomplete_folder_is_refused(monkeypatch):
    """The archive holds 3 of the 5 the server declared: emptying it would lose two for good."""
    run_migrations()
    account_id, _email = _mailbox(archived=3, remote=5)
    monkeypatch.setattr(purge, "decrypt_secret", lambda _v: "pw")
    monkeypatch.setattr(purge, "_connect_with_retry",
                        lambda _a, _p: pytest.fail("must not reach the server"))

    with pytest.raises(purge.PurgeRefused, match="per intero"):
        purge.purge_folder(account_id, "Inbox")


def test_cancelling_stops_between_batches(monkeypatch):
    run_migrations()
    account_id, _email = _mailbox(archived=450, remote=450)
    server = FakeServer(list(range(1000, 1450)))
    monkeypatch.setattr(purge, "_connect_with_retry", lambda _a, _p: server)
    monkeypatch.setattr(purge, "decrypt_secret", lambda _v: "pw")
    calls = []

    result = purge.purge_folder(account_id, "Inbox",
                                should_cancel=lambda: bool(calls.append(1)) or len(calls) > 2)

    assert result["cancelled"] is True
    assert 0 < result["deleted"] < 450, "it stopped part way, without a half-finished batch"
    assert sum(server.batches) == result["deleted"]


def test_the_api_refuses_without_every_confirmation():
    """Confirmations checked only in the browser are confirmations that can be skipped."""
    run_migrations()
    account_id, email = _mailbox()
    with TestClient(app) as client:
        headers = login(client, email, "secure-purge-password")
        body = {"folder": "Inbox", "confirm_folder": "Inbox",
                "verified_backup": True, "understood_irreversible": True}

        assert client.post(f"/api/accounts/{account_id}/purge", headers=headers,
                           json={**body, "verified_backup": False}).status_code == 400
        assert client.post(f"/api/accounts/{account_id}/purge", headers=headers,
                           json={**body, "understood_irreversible": False}).status_code == 400
        assert client.post(f"/api/accounts/{account_id}/purge", headers=headers,
                           json={**body, "confirm_folder": "inbox"}).status_code == 400
        assert client.post(f"/api/accounts/{account_id}/purge", headers=headers,
                           json={**body, "folder": "Inesistente",
                                 "confirm_folder": "Inesistente"}).status_code == 404


def test_the_api_is_closed_to_the_standard_plan():
    run_migrations()
    account_id, email = _mailbox(plan="STANDARD")
    with TestClient(app) as client:
        headers = login(client, email, "secure-purge-password")
        assert client.get(f"/api/accounts/{account_id}/purge-preview").status_code == 403
        assert client.post(f"/api/accounts/{account_id}/purge", headers=headers, json={
            "folder": "Inbox", "confirm_folder": "Inbox",
            "verified_backup": True, "understood_irreversible": True}).status_code == 403
