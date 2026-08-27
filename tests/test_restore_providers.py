from __future__ import annotations

import base64
import re
from urllib.parse import parse_qs

from fastapi.testclient import TestClient

import app.main as main
import app.restore_providers as restore_providers
from app.database import SessionLocal
from app.graph_adapter import GraphError
from app.main import app
from app.models import Account, Folder, Snapshot, User, utcnow
from app.restore_providers import MicrosoftGraphRestoreTarget, account_restore_support, restore_provider_id
from app.security import decrypt_secret, encrypt_secret, hash_password


class FakeGraph:
    """Minimal Graph double: folders, message import and PATCH bookkeeping."""

    def __init__(self, fail_first_with: int | None = None):
        self.folders = {"root": {}}          # parent id -> {display name: folder id}
        self.messages: list[dict] = []
        self.patched: list[dict] = []
        self.calls: list[tuple[str, str]] = []
        self._fail_first_with = fail_first_with
        self._next_id = 0

    def _new_id(self, prefix: str) -> str:
        self._next_id += 1
        return f"{prefix}-{self._next_id}"

    def __call__(self, token, method, path, body=None, content_type="application/json", timeout=120):
        self.calls.append((method, path))
        if self._fail_first_with is not None:
            status, self._fail_first_with = self._fail_first_with, None
            raise GraphError(status, "expired")
        if method == "GET" and "childFolders" in path:
            parent = re.search(r"/me/mailFolders/([^/]+)/childFolders", path).group(1)
            return {"value": [{"id": value, "displayName": name} for name, value in self.folders.get(parent, {}).items()]}
        if method == "GET" and path.startswith("/me/mailFolders?"):
            return {"value": [{"id": value, "displayName": name} for name, value in self.folders["root"].items()]}
        if method == "GET" and "internetMessageId" in path:
            wanted = parse_qs(path.split("?", 1)[1]).get("$filter", [""])[0]
            needle = re.search(r"internetMessageId eq '(.+)'", wanted).group(1)
            return {"value": [item for item in self.messages if needle in item.get("mime", "")][:1]}
        if method == "POST" and path.endswith("/childFolders"):
            parent = re.search(r"/me/mailFolders/([^/]+)/childFolders", path).group(1)
            folder_id = self._new_id("folder")
            self.folders.setdefault(parent, {})[body["displayName"]] = folder_id
            return {"id": folder_id, "displayName": body["displayName"]}
        if method == "POST" and path == "/me/mailFolders":
            folder_id = self._new_id("folder")
            self.folders["root"][body["displayName"]] = folder_id
            return {"id": folder_id, "displayName": body["displayName"]}
        if method == "POST" and path.endswith("/messages"):
            folder = re.search(r"/me/mailFolders/([^/]+)/messages", path).group(1)
            item = {"id": self._new_id("message"), "folder": folder, "isRead": False}
            if content_type == "text/plain":
                item["mime"] = base64.b64decode(body).decode("utf-8", "replace")
            else:
                item["json"] = body
            self.messages.append(item)
            return item
        if method == "PATCH":
            self.patched.append(body)
            return {"id": path.rsplit("/", 1)[-1], **body}
        raise AssertionError(f"unexpected Graph call {method} {path}")


def _target(monkeypatch, graph: FakeGraph, tokens=None) -> MicrosoftGraphRestoreTarget:
    issued = list(tokens or [{"access_token": "token-1", "refresh_token": "refresh-1"}])
    monkeypatch.setattr(restore_providers, "graph_request", graph)
    monkeypatch.setattr(restore_providers, "refresh_access_token", lambda _token: issued.pop(0) if issued else
                        {"access_token": "token-n", "refresh_token": "refresh-n"})
    target = MicrosoftGraphRestoreTarget("stored-refresh-token")
    target.connect()
    return target


def test_graph_restore_creates_nested_folders_imports_mime_and_applies_flags(monkeypatch):
    graph = FakeGraph()
    target = _target(monkeypatch, graph)

    target.prepare_folder("Archivio/2026/Fatture")
    assert set(graph.folders["root"]) == {"Archivio"}
    created = graph.folders["root"]["Archivio"]
    assert set(graph.folders[created]) == {"2026"}

    raw = b"Message-ID: <import-1@example>\r\nSubject: Fattura\r\n\r\ncorpo"
    target.deliver(raw, ["\\Seen"], None)
    assert graph.messages[-1]["mime"] == raw.decode()
    assert graph.patched == [{"isRead": True}]  # MIME import lands unread, the flag is restored

    # A second folder with the same prefix reuses the cached ids instead of recreating them.
    folder_calls = len([call for call in graph.calls if call[0] == "POST" and "Folders" in call[1]])
    target.prepare_folder("Archivio/2026/Fatture")
    assert len([call for call in graph.calls if call[0] == "POST" and "Folders" in call[1]]) == folder_calls


def test_graph_restore_detects_duplicates_and_refreshes_expired_token(monkeypatch):
    graph = FakeGraph()
    target = _target(monkeypatch, graph)
    target.prepare_folder("INBOX")
    target.deliver(b"Message-ID: <dup@example>\r\n\r\nbody", [], None)
    assert target.has_message("<dup@example>") is True
    assert target.has_message("<missing@example>") is False

    # A restore longer than the access token lifetime must refresh and retry once.
    graph._fail_first_with = 401
    target.deliver(b"Message-ID: <after-refresh@example>\r\n\r\nbody", [], None)
    assert target.refresh_token == "refresh-n"
    assert any("after-refresh@example" in item.get("mime", "") for item in graph.messages)


def test_graph_restore_rebuilds_message_when_mime_import_is_refused(monkeypatch):
    graph = FakeGraph()
    target = _target(monkeypatch, graph)
    target.prepare_folder("INBOX")

    original = graph.__call__

    def refuse_mime(token, method, path, body=None, content_type="application/json", timeout=120):
        if content_type == "text/plain":
            raise GraphError(400, "MIME import unavailable")
        return original(token, method, path, body, content_type, timeout)

    monkeypatch.setattr(restore_providers, "graph_request", refuse_mime)
    raw = (b"Message-ID: <big@example>\r\nSubject: Report\r\nFrom: Anna <anna@example.com>\r\n"
           b"To: bruno@example.com\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<p>Ciao</p>")
    target.deliver(raw, ["\\Seen"], None)

    rebuilt = graph.messages[-1]["json"]
    assert rebuilt["subject"] == "Report"
    assert rebuilt["body"]["contentType"] == "HTML" and "Ciao" in rebuilt["body"]["content"]
    assert rebuilt["from"]["emailAddress"]["address"] == "anna@example.com"
    assert rebuilt["toRecipients"][0]["emailAddress"]["address"] == "bruno@example.com"
    assert rebuilt["internetMessageId"] == "<big@example>"
    assert rebuilt["isRead"] is True


def test_provider_picker_matches_the_mailbox_kind():
    microsoft = Account(auth_provider="microsoft", imap_host="graph.microsoft.com", imap_enabled=True,
                        encrypted_password="secret")
    gmail = Account(auth_provider="imap", imap_host="imap.gmail.com", imap_enabled=True, encrypted_password="secret")
    generic = Account(auth_provider="imap", imap_host="imap.example.com", imap_enabled=True, encrypted_password="secret")
    offline = Account(auth_provider="mbox", imap_enabled=False)

    assert restore_provider_id(microsoft) == "microsoft_graph"
    assert restore_provider_id(gmail) == "gmail"
    assert restore_provider_id(generic) == "imap"
    assert account_restore_support(microsoft)["supported"] is True
    assert account_restore_support(offline)["supported"] is False


def _csrf(client: TestClient) -> dict[str, str]:
    token = re.search(r'name="csrf-token" content="([^"]+)', client.get("/app").text).group(1)
    return {"X-CSRF-Token": token}


def test_microsoft_mailbox_is_accepted_as_restore_destination(monkeypatch):
    probed: list[str] = []

    class Probe:
        def __init__(self, credentials): probed.append(credentials["provider"])
        def probe(self): return {"ok": True, "folders": 7, "capabilities": ["MICROSOFT_GRAPH"]}
        def rotated_secret(self): return "rotated-refresh-token"

    monkeypatch.setattr(main, "build_restore_target", Probe)
    with TestClient(app) as client, SessionLocal() as db:
        owner = User(username="restore-ms@example.com", email="restore-ms@example.com",
                     password_hash=hash_password("secure-restore-password"), verified_at=utcnow())
        db.add(owner); db.flush()
        source = Account(owner_id=owner.id, archive_uuid="00000000-0000-0000-0000-000000000301",
                         display_name="Origine", email="origine@example.com", imap_enabled=True,
                         imap_host="imap.example.com", imap_port=993, security="ssl",
                         imap_username="origine@example.com", encrypted_password=encrypt_secret("pw"),
                         mailbox_identity="5" * 64)
        outlook = Account(owner_id=owner.id, archive_uuid="00000000-0000-0000-0000-000000000302",
                          display_name="Outlook", email="outlook-restore@example.com", auth_provider="microsoft",
                          imap_host="graph.microsoft.com", imap_port=443, security="oauth2", imap_enabled=True,
                          imap_username="outlook-restore@example.com",
                          encrypted_password=encrypt_secret("refresh-token"), mailbox_identity="6" * 64)
        offline = Account(owner_id=owner.id, archive_uuid="00000000-0000-0000-0000-000000000303",
                          display_name="MBOX", email="mbox-restore@example.com", auth_provider="mbox",
                          imap_enabled=False, mailbox_identity="7" * 64)
        db.add_all([source, outlook, offline]); db.flush()
        snapshot = Snapshot(account_id=source.id, snapshot_uuid="00000000-0000-0000-0000-000000000304",
                            status="completed", completed_at=utcnow(), message_count=3, archive_size=1024)
        db.add(snapshot); db.flush()
        db.add(Folder(snapshot_id=snapshot.id, name="INBOX", message_count=3, flags_json="[]"))
        source.active_snapshot_id = snapshot.id
        db.commit()
        source_id, outlook_id, offline_id = source.id, outlook.id, offline.id

        assert client.post("/api/login", json={"username": "restore-ms@example.com",
                                               "password": "secure-restore-password"}).status_code == 200
        headers = _csrf(client)
        result = client.post("/api/imap-transfer/test", headers=headers, json={"destination": {"account_id": outlook_id}})
        assert result.status_code == 200 and result.json()["folders"] == 7
        assert probed == ["microsoft_graph"]

        offline_result = client.post("/api/imap-transfer/test", headers=headers, json={"destination": {"account_id": offline_id}})
        assert offline_result.status_code == 409 and "MBOX" in offline_result.text

        destinations = client.get(f"/api/accounts/{source_id}/transfer-preview").json()["destinations"]
        by_id = {item["id"]: item for item in destinations}
        assert by_id[outlook_id]["provider"] == "microsoft_graph"
        assert offline_id not in by_id

        # The check refreshes the OAuth token, so the rotated one has to be stored.
        db.expire_all()
        assert decrypt_secret(db.get(Account, outlook_id).encrypted_password) == "rotated-refresh-token"
