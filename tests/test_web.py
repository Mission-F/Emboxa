from __future__ import annotations

import re
import json
import time
import tempfile
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.backup import snapshot_root
from app.config import EXPORTS_DIR
from app.database import SessionLocal
from app.main import app
import app.main as main
from app.models import Account, AppSetting, PasskeyCredential, Snapshot, TelegramLink, User, WebExport, utcnow
from app.scheduler import scheduler
from app.security import decrypt_secret, encrypt_secret, hash_password


MAILBOX = {
    "display_name": "Mailbox", "email": "mailbox@example.com", "imap_host": "imap.example.com",
    "imap_port": 993, "security": "ssl", "imap_username": "mailbox@example.com",
    "password": "imap-app-password", "schedule_mode": "disabled", "retention_versions": 3,
}


def csrf(client: TestClient) -> dict[str, str]:
    page = client.get("/app")
    token = re.search(r'name="csrf-token" content="([^"]+)', page.text).group(1)
    return {"X-CSRF-Token": token}


def login(client: TestClient, email: str, password: str) -> dict[str, str]:
    response = client.post("/api/login", json={"username": email, "password": password})
    assert response.status_code == 200, response.text
    return csrf(client)


def fake_export_file(name: str, content: bytes) -> tuple:
    temp_dir = Path(tempfile.mkdtemp(prefix="test-export-", dir=EXPORTS_DIR))
    path = temp_dir / name
    path.write_bytes(content)
    return path, name


def wait_export(client: TestClient, job: dict) -> dict:
    for _ in range(80):
        status = client.get(job["status_url"])
        assert status.status_code == 200, status.text
        payload = status.json()
        if payload["status"] == "completed":
            return payload["export"]
        if payload["status"] == "failed":
            raise AssertionError(payload.get("error") or payload.get("detail"))
        time.sleep(0.05)
    raise AssertionError("export job did not complete")


def wait_mbox_import(client: TestClient, job: dict) -> dict:
    for _ in range(80):
        status = client.get(job["status_url"])
        assert status.status_code == 200, status.text
        payload = status.json()
        if payload["status"] == "completed":
            return payload["account"]
        if payload["status"] == "failed":
            raise AssertionError(payload.get("error") or payload.get("detail"))
        time.sleep(0.05)
    raise AssertionError("mbox import job did not complete")


def wait_archive_import(client: TestClient, job: dict) -> dict:
    for _ in range(80):
        status = client.get(job["status_url"])
        assert status.status_code == 200, status.text
        payload = status.json()
        if payload["status"] == "completed":
            return payload
        if payload["status"] == "failed":
            raise AssertionError(payload.get("error") or payload.get("detail"))
        time.sleep(0.05)
    raise AssertionError("archive import job did not complete")


def test_passkey_registration_login_and_delete(monkeypatch):
    class FakeOptions:
        def __init__(self, challenge: bytes, allow_credentials=None):
            self.challenge = challenge
            self.allow_credentials = allow_credentials or []

    class FakeRegistration:
        credential_id = b"credential-1"
        credential_public_key = b"public-key-1"
        sign_count = 1
        credential_device_type = "multi_device"
        credential_backed_up = True

    class FakeAuthentication:
        new_sign_count = 2

    monkeypatch.setattr(main, "generate_registration_options", lambda **kwargs: FakeOptions(b"registration-challenge"))
    monkeypatch.setattr(main, "generate_authentication_options", lambda **kwargs: FakeOptions(b"authentication-challenge", kwargs.get("allow_credentials")))
    monkeypatch.setattr(main, "verify_registration_response", lambda **kwargs: FakeRegistration())
    monkeypatch.setattr(main, "verify_authentication_response", lambda **kwargs: FakeAuthentication())

    def fake_options_to_json(options):
        payload = {"challenge": main._b64url(options.challenge), "timeout": 60000}
        if options.allow_credentials:
            payload["allowCredentials"] = [{"type": "public-key", "id": main._b64url(item.id)} for item in options.allow_credentials]
        else:
            payload["rp"] = {"id": "testserver", "name": "Emboxa Web"}
            payload["user"] = {"id": main._b64url(b"1"), "name": "passkey@example.com", "displayName": "passkey@example.com"}
            payload["pubKeyCredParams"] = [{"type": "public-key", "alg": -7}]
        return json.dumps(payload)

    monkeypatch.setattr(main, "options_to_json", fake_options_to_json)

    with TestClient(app) as client:
        with SessionLocal() as db:
            user = User(username="passkey@example.com", email="passkey@example.com",
                        password_hash=hash_password("secure-passkey-password"), verified_at=utcnow())
            db.add(user)
            db.commit()
        headers = login(client, "passkey@example.com", "secure-passkey-password")
        options = client.post("/api/passkeys/register/options", headers=headers)
        assert options.status_code == 200, options.text
        assert options.json()["challenge"] == main._b64url(b"registration-challenge")
        credential_json = {
            "id": main._b64url(b"credential-1"),
            "rawId": main._b64url(b"credential-1"),
            "type": "public-key",
            "response": {
                "clientDataJSON": main._b64url(b"client-data"),
                "attestationObject": main._b64url(b"attestation"),
                "transports": ["internal"],
            },
            "clientExtensionResults": {},
        }
        verified = client.post("/api/passkeys/register/verify", json={"credential": credential_json, "name": "Mac Touch ID"}, headers=headers)
        assert verified.status_code == 200, verified.text
        listed = client.get("/api/passkeys")
        assert listed.status_code == 200
        assert listed.json()[0]["name"] == "Mac Touch ID"

        assert client.post("/api/logout", headers=headers).status_code == 200
        auth_options = client.post("/api/passkeys/authentication/options", json={"email": "passkey@example.com"})
        assert auth_options.status_code == 200, auth_options.text
        assert auth_options.json()["allowCredentials"][0]["id"] == main._b64url(b"credential-1")
        authenticated = client.post("/api/passkeys/authentication/verify", json={"credential": {
            "id": main._b64url(b"credential-1"),
            "rawId": main._b64url(b"credential-1"),
            "type": "public-key",
            "response": {
                "clientDataJSON": main._b64url(b"client-data"),
                "authenticatorData": main._b64url(b"authenticator-data"),
                "signature": main._b64url(b"signature"),
                "userHandle": main._b64url(b"1"),
            },
            "clientExtensionResults": {},
        }})
        assert authenticated.status_code == 200, authenticated.text
        assert client.get("/app").status_code == 200
        headers = csrf(client)
        passkey_id = client.get("/api/passkeys").json()[0]["id"]
        assert client.delete(f"/api/passkeys/{passkey_id}", headers=headers).status_code == 200
        with SessionLocal() as db:
            assert db.scalar(select(PasskeyCredential)) is None


def test_microsoft_oauth_connect_refresh_token_and_disconnect(monkeypatch):
    captured = {}
    monkeypatch.setattr(main, "microsoft_authorize_url", lambda redirect, state: captured.update({"redirect": redirect, "state": state}) or "https://login.microsoftonline.com/common/oauth2/v2.0/authorize")
    monkeypatch.setattr(main, "exchange_code", lambda code, redirect: {"access_token": "access-token", "refresh_token": "refresh-token"})
    monkeypatch.setattr(main, "microsoft_profile", lambda access_token: {"id": "ms-user-1", "displayName": "Outlook Box", "mail": "outlook@example.com"})

    with TestClient(app) as client:
        with SessionLocal() as db:
            user = User(username="oauth-user@example.com", email="oauth-user@example.com",
                        password_hash=hash_password("secure-oauth-password"), verified_at=utcnow())
            db.add(user)
            db.commit()
        headers = login(client, "oauth-user@example.com", "secure-oauth-password")
        start = client.get("/api/auth/microsoft/start", follow_redirects=False)
        assert start.status_code == 303 and captured["state"]
        callback = client.get(f"/api/auth/microsoft/callback?code=abc&state={captured['state']}", follow_redirects=False)
        assert callback.status_code == 303 and callback.headers["location"] == "/app?microsoft=connected"

        with SessionLocal() as db:
            account = db.scalar(select(Account).where(Account.email == "outlook@example.com"))
            assert account.auth_provider == "microsoft"
            assert account.imap_host == "graph.microsoft.com" and account.security == "oauth2"
            assert account.encrypted_password != "refresh-token"
            assert decrypt_secret(account.encrypted_password) == "refresh-token"
            account_id = account.id

        response = client.get("/api/accounts")
        assert response.status_code == 200 and response.json()[0]["auth_provider"] == "microsoft"
        assert client.delete(f"/api/accounts/{account_id}/microsoft", headers=headers).status_code == 200
        with SessionLocal() as db:
            account = db.get(Account, account_id)
            assert account.encrypted_password is None and account.imap_enabled is False


def test_storage_usage_is_recomputed_after_archive_delete():
    with SessionLocal() as db:
        user = User(username="storage-live@example.com", email="storage-live@example.com",
                    password_hash=hash_password("secure-storage-password"), verified_at=utcnow())
        db.add(user); db.flush()
        account = Account(owner_id=user.id, archive_uuid="00000000-0000-0000-0000-000000000188",
                          display_name="Storage Live", email="storage-live@example.com",
                          imap_enabled=False, mailbox_identity="storage-live", archive_size=999999)
        db.add(account); db.flush()
        snapshot = Snapshot(account_id=account.id, snapshot_uuid="00000000-0000-0000-0000-000000000189",
                            status="completed", completed_at=utcnow(), archive_size=999999)
        db.add(snapshot); db.flush()
        account.active_snapshot_id = snapshot.id
        root = snapshot_root(account.archive_uuid, snapshot.snapshot_uuid)
        root.mkdir(parents=True, exist_ok=True)
        (root / "payload.bin").write_bytes(b"live-storage")
        db.commit()
        account_id = account.id

    with TestClient(app) as client:
        headers = login(client, "storage-live@example.com", "secure-storage-password")
        usage = client.get("/api/web/usage")
        assert usage.status_code == 200
        assert usage.json()["storage_used"] == len(b"live-storage")

        deleted = client.delete(f"/api/accounts/{account_id}/archive", headers=headers)
        assert deleted.status_code == 200, deleted.text
        usage = client.get("/api/web/usage")
        assert usage.status_code == 200
        assert usage.json()["storage_used"] == 0


def test_mbox_import_from_link_downloads_server_side(monkeypatch):
    class FakeResponse:
        status = 200
        headers = {"content-length": str(len(b"From a@example.com Sat Jan  1 00:00:00 2022\nSubject: Linked\n\nBody\n"))}

        def __init__(self):
            self._chunks = [b"From a@example.com Sat Jan  1 00:00:00 2022\n", b"Subject: Linked\n\nBody\n"]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size):
            return self._chunks.pop(0) if self._chunks else b""

    downloaded = {}
    monkeypatch.setattr(main, "urlopen", lambda request, timeout=60: downloaded.setdefault("url", request.full_url) and FakeResponse())

    def fake_import(sources, user_id, display_name, email, progress=None):
        assert sources[0].path.read_text().startswith("From a@example.com")
        assert sources[0].folder_name == "linked"
        if progress:
            progress(95, 1, "linked")
        with SessionLocal() as db:
            account = Account(owner_id=user_id, archive_uuid="00000000-0000-0000-0000-000000000288",
                              display_name=display_name, email=email,
                              imap_enabled=False, auth_provider="mbox", mailbox_identity="linked")
            db.add(account); db.commit()
            return account.id

    monkeypatch.setattr(main, "import_mbox_sources", fake_import)
    with SessionLocal() as db:
        user = User(username="mbox-link@example.com", email="mbox-link@example.com",
                    password_hash=hash_password("secure-mbox-link-password"), verified_at=utcnow(),
                    plan="PLUS", storage_limit_bytes=0)
        db.add(user); db.commit()

    with TestClient(app) as client:
        headers = login(client, "mbox-link@example.com", "secure-mbox-link-password")
        response = client.post("/api/import/mbox/link", json={
            "url": "https://example.com/linked.mbox",
            "display_name": "Linked MBOX",
            "email": "linked@local.invalid",
        }, headers=headers)
        assert response.status_code == 200, response.text
        account = wait_mbox_import(client, response.json())
        assert account["display_name"] == "Linked MBOX"
        assert downloaded["url"] == "https://example.com/linked.mbox"


def test_mailvault_upload_link_local_import_and_local_export(monkeypatch, tmp_path):
    with SessionLocal() as db:
        user = User(username="archive-ops@example.com", email="archive-ops@example.com",
                    password_hash=hash_password("secure-archive-ops-password"), verified_at=utcnow(),
                    plan="PLUS", storage_limit_bytes=0)
        db.add(user); db.flush()
        account = Account(owner_id=user.id, archive_uuid="00000000-0000-0000-0000-000000000388",
                          display_name="Exportable", email="exportable@example.com",
                          imap_enabled=False, mailbox_identity="exportable", archive_size=64)
        db.add(account); db.commit(); user_id = user.id; account_id = account.id

    imported_paths: list[str] = []

    def fake_import_archive(path, owner_id):
        assert owner_id == user_id
        imported_paths.append(Path(path).name)
        with SessionLocal() as db:
            imported = Account(owner_id=owner_id, archive_uuid=f"00000000-0000-0000-0000-00000000039{len(imported_paths)}",
                               display_name=f"Imported {len(imported_paths)}", email="imported@example.com",
                               imap_enabled=False, mailbox_identity=f"imported-{len(imported_paths)}")
            db.add(imported); db.commit()
            return imported.id

    monkeypatch.setattr(main, "import_archive", fake_import_archive)

    class FakeResponse:
        status = 200
        headers = {"content-length": "15"}

        def __init__(self):
            self._chunks = [b"mailvault-bytes"]

        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def read(self, _size): return self._chunks.pop(0) if self._chunks else b""

    monkeypatch.setattr(main, "urlopen", lambda request, timeout=60: FakeResponse())
    local_imports = tmp_path / "local-imports"; local_imports.mkdir()
    (local_imports / "drop.mailvault").write_bytes(b"drop")
    local_exports = tmp_path / "local-exports"
    monkeypatch.setattr(main, "LOCAL_IMPORTS_DIR", local_imports)
    monkeypatch.setattr(main, "LOCAL_EXPORTS_DIR", local_exports)
    monkeypatch.setattr(main, "build_export", lambda _account_id, progress=None: fake_export_file("ops.mailvault", b"ops-export"))

    with TestClient(app) as client:
        headers = login(client, "archive-ops@example.com", "secure-archive-ops-password")

        upload = client.post("/api/import", files={"file": ("upload.mailvault", b"upload", "application/octet-stream")}, headers=headers)
        assert upload.status_code == 200, upload.text
        assert wait_archive_import(client, upload.json())["account_id"]

        link = client.post("/api/import/link", json={"url": "https://example.com/archive.mailvault"}, headers=headers)
        assert link.status_code == 200, link.text
        assert wait_archive_import(client, link.json())["account_id"]

        local = client.post("/api/import/local", json={"mode": "mailvault"}, headers=headers)
        assert local.status_code == 200, local.text
        assert wait_archive_import(client, local.json())["account_ids"]

        export = client.post(f"/api/accounts/{account_id}/export/local", headers=headers)
        assert export.status_code == 200, export.text
        result = wait_export(client, export.json())
        assert result["filename"] == "ops.mailvault"
        assert (local_exports / "ops.mailvault").read_bytes() == b"ops-export"


def test_web_multitenant_plans_retention_cleanup_telegram_and_public(monkeypatch):
    sent: list[tuple[str, str, str, str | None]] = []
    monkeypatch.setattr(main, "_send_email", lambda to, subject, body, html=None: sent.append((to, subject, body, html)))
    with TestClient(app) as client:
        registered = client.post("/api/register", json={"email": "user@example.com", "password": "secure-user-password"})
        assert registered.status_code == 200, registered.text
        assert sent[-1][3] and "EMBOXA" in sent[-1][3] and "Verify your email" in sent[-1][3]
        code_parts = re.search(r"\b(\d{3})\s+(\d{3})\b", sent[-1][2]).groups()
        code = "".join(code_parts)
        verified = client.post("/api/verify", json={"email": "user@example.com", "code": code})
        assert verified.status_code == 200
        headers = csrf(client)

        account_ids = []
        for index in range(2):
            payload = {**MAILBOX, "display_name": f"Mailbox {index}", "email": f"mail{index}@example.com",
                       "imap_username": f"mail{index}@example.com"}
            response = client.post("/api/accounts", json=payload, headers=headers)
            assert response.status_code == 200, response.text
            account_ids.append(response.json()["id"])
        third = client.post("/api/accounts", json={**MAILBOX, "email": "third@example.com"}, headers=headers)
        assert third.status_code == 409 and "Mailbox limit" in third.text

        assert client.post(f"/api/accounts/{account_ids[0]}/permanent", headers=headers).status_code == 200
        locked = client.post(f"/api/accounts/{account_ids[1]}/permanent", headers=headers)
        assert locked.status_code == 409 and "locked until" in locked.text
        assert client.delete(f"/api/accounts/{account_ids[0]}", headers=headers).status_code == 200
        assert client.post(f"/api/accounts/{account_ids[1]}/permanent", headers=headers).status_code == 409

        with SessionLocal() as db:
            user = db.scalar(select(User).where(User.email == "user@example.com"))
            user.storage_limit_bytes = 128
            account = db.get(Account, account_ids[1]); account.archive_size = 128
            snapshot = Snapshot(account_id=account.id, snapshot_uuid="00000000-0000-0000-0000-000000000088",
                                status="completed", completed_at=utcnow(), archive_size=128)
            db.add(snapshot); db.flush(); account.active_snapshot_id = snapshot.id
            root = snapshot_root(account.archive_uuid, snapshot.snapshot_uuid)
            root.mkdir(parents=True, exist_ok=True)
            (root / "payload.bin").write_bytes(b"x" * 128)
            admin = User(username="admin@example.com", email="admin@example.com", password_hash=hash_password("secure-admin-password"),
                         verified_at=utcnow(), role="admin", plan="PLUS", storage_limit_bytes=0)
            other = User(username="other@example.com", email="other@example.com", password_hash=hash_password("secure-other-password"),
                         verified_at=utcnow())
            db.add_all([admin, other]); db.flush()
            foreign = Account(owner_id=other.id, archive_uuid="00000000-0000-0000-0000-000000000099", display_name="Foreign",
                              email="foreign@example.com", imap_enabled=False, mailbox_identity="f" * 64)
            db.add(foreign); db.commit(); user_id, foreign_id = user.id, foreign.id
        quota = client.post(f"/api/accounts/{account_ids[1]}/backup", headers=headers)
        assert quota.status_code == 409 and "Storage limit reached" in quota.text
        assert client.get(f"/api/accounts/{foreign_id}/versions").status_code == 404
        assert client.get(f"/api/accounts/{foreign_id}/export").status_code == 404

        with TestClient(app) as admin_client:
            admin_headers = login(admin_client, "admin@example.com", "secure-admin-password")
            assert admin_client.get("/admin").status_code == 200
            assert admin_client.patch(f"/api/admin/users/{user_id}", json={"plan": "PLUS"}, headers=admin_headers).status_code == 200
            downgrade = admin_client.patch(f"/api/admin/users/{user_id}", json={"plan": "STANDARD"}, headers=admin_headers)
            assert downgrade.status_code == 409

        for index in (6, 7):
            payload = {**MAILBOX, "display_name": f"Mailbox {index}", "email": f"mail{index}@example.com",
                       "imap_username": f"mail{index}@example.com"}
            assert client.post("/api/accounts", json=payload, headers=headers).status_code == 200

        with SessionLocal() as db:
            user = db.get(User, user_id)
            link = TelegramLink(user_id=user.id, chat_id="123456")
            db.add(link)
            db.add(AppSetting(key="telegram_webhook_secret", value=encrypt_secret("webhook-secret"), encrypted=True))
            db.commit()
        telegram_calls = []
        monkeypatch.setattr(main, "_telegram_call", lambda method, payload: telegram_calls.append((method, payload)) or {"message_id": 7})
        started = []
        monkeypatch.setattr(main.backup_manager, "start", lambda account_id: started.append(account_id) or (91, True))
        webhook_headers = {"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"}
        foreign_callback = {"callback_query": {"id": "c1", "data": f"backup:{foreign_id}",
                            "message": {"message_id": 7, "chat": {"id": 123456}}}}
        assert client.post("/api/telegram/webhook", json=foreign_callback, headers=webhook_headers).status_code == 200
        assert not started
        own_callback = {"callback_query": {"id": "c2", "data": f"backup:{account_ids[1]}",
                        "message": {"message_id": 7, "chat": {"id": 123456}}}}
        assert client.post("/api/telegram/webhook", json=own_callback, headers=webhook_headers).status_code == 200
        assert started == [account_ids[1]]

        EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
        expired_path = EXPORTS_DIR / "expired.mailvault"; expired_path.write_bytes(b"expired")
        with SessionLocal() as db:
            db.add(WebExport(public_id="00000000-0000-0000-0000-000000000001", owner_id=user_id,
                             account_id=account_ids[1], filename="expired.mailvault", relpath="expired.mailvault",
                             size=7, expires_at=utcnow() - timedelta(hours=1)))
            db.commit()
        scheduler.cleanup()
        assert not expired_path.exists()

        sent.clear()
        assert client.post("/api/password-reset/request", json={"email": "user@example.com"}).status_code == 200
        raw_token = re.search(r"token=([A-Za-z0-9_%-]+)", sent[-1][2]).group(1)
        assert client.post("/api/password-reset/confirm", json={"token": raw_token, "password": "new-secure-password"}).status_code == 200

        root = client.get("/", follow_redirects=False)
        assert root.status_code == 303 and root.headers["location"] == "/app"
        public = client.get("/it/", follow_redirects=False)
        assert public.status_code == 308 and public.headers["location"] == "https://emboxa.eu/it/"
        assert client.get("/robots.txt").text == "User-agent: *\nDisallow: /\n"
        sitemap_redirect = client.get("/sitemap.xml", follow_redirects=False)
        assert sitemap_redirect.status_code == 308 and sitemap_redirect.headers["location"] == "https://emboxa.eu/sitemap.xml"
        assert client.get("/en/imap-email-backup", follow_redirects=False).status_code == 308
        assert "noindex" in client.get("/app").headers["x-robots-tag"]

        with TestClient(app) as guest:
            root = guest.get("/", follow_redirects=False)
            assert root.status_code == 303 and root.headers["location"] == "/login"


def test_standard_export_can_be_kept_forever_when_admin_sets_zero_ttl(monkeypatch):
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main, "build_export", lambda _account_id: fake_export_file("standard.mailvault", b"standard-export"))
    with SessionLocal() as db:
        user = User(username="standard-export@example.com", email="standard-export@example.com",
                    password_hash=hash_password("standard-export-password"), verified_at=utcnow(),
                    plan="STANDARD", storage_limit_bytes=1024 * 1024)
        db.add(user); db.flush()
        account = Account(owner_id=user.id, archive_uuid="00000000-0000-0000-0000-000000000501",
                          display_name="Standard Export", email="standard-export@example.com",
                          imap_enabled=False, mailbox_identity="standard-export", archive_size=64)
        db.add(account)
        db.merge(AppSetting(key="export_ttl_hours", value="0"))
        db.commit(); account_id = account.id

    with TestClient(app) as client:
        headers = login(client, "standard-export@example.com", "standard-export-password")
        response = client.post(f"/api/accounts/{account_id}/export", headers=headers)
        assert response.status_code == 200, response.text
        payload = wait_export(client, response.json())
        assert payload["persistent"] is True and payload["expires_at"] is None
        download = client.get(payload["download_url"])
        assert download.status_code == 200 and download.content == b"standard-export"

    with SessionLocal() as db:
        item = db.scalar(select(WebExport).where(WebExport.owner_id == user.id))
        export_path = EXPORTS_DIR / item.relpath
        assert item.expires_at is None and export_path.exists()
        scheduler.cleanup()
        assert export_path.exists()
        db.delete(user)
        setting = db.get(AppSetting, "export_ttl_hours")
        if setting: db.delete(setting)
        db.commit()


def test_plus_exports_ignore_size_storage_and_duration_limits(monkeypatch):
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main, "build_export", lambda _account_id: fake_export_file("plus.mailvault", b"plus-export"))
    with SessionLocal() as db:
        user = User(username="plus-export@example.com", email="plus-export@example.com",
                    password_hash=hash_password("plus-export-password"), verified_at=utcnow(),
                    plan="PLUS", storage_limit_bytes=1)
        db.add(user); db.flush()
        account = Account(owner_id=user.id, archive_uuid="00000000-0000-0000-0000-000000000502",
                          display_name="Plus Export", email="plus-export@example.com",
                          imap_enabled=False, mailbox_identity="plus-export", archive_size=10 * 1024**3)
        db.add(account)
        db.merge(AppSetting(key="export_ttl_hours", value="1"))
        db.merge(AppSetting(key="export_max_bytes", value="1"))
        db.commit(); account_id = account.id

    with TestClient(app) as client:
        headers = login(client, "plus-export@example.com", "plus-export-password")
        response = client.post(f"/api/accounts/{account_id}/export", headers=headers)
        assert response.status_code == 200, response.text
        payload = wait_export(client, response.json())
        assert payload["persistent"] is True and payload["expires_at"] is None and payload["size"] > 1
        assert client.get(payload["download_url"]).content == b"plus-export"

    with SessionLocal() as db:
        item = db.scalar(select(WebExport).where(WebExport.owner_id == user.id))
        assert item.expires_at is None
        db.delete(user)
        for key in ("export_ttl_hours", "export_max_bytes"):
            setting = db.get(AppSetting, key)
            if setting: db.delete(setting)
        db.commit()
