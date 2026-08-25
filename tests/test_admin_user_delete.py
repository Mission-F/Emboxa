from __future__ import annotations

import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import ARCHIVES_DIR, EXPORTS_DIR
from app.database import SessionLocal
from app.main import app
from app.models import Account, AdminAudit, SecurityToken, Snapshot, TelegramLink, User, WebExport, utcnow
from app.security import hash_password


def test_admin_permanently_deletes_user_and_owned_files():
    archive_uuid = "00000000-0000-0000-0000-000000000401"
    with TestClient(app) as client:
        with SessionLocal() as db:
            admin = User(username="delete-admin@example.com", email="delete-admin@example.com", password_hash=hash_password("secure-admin-password"), verified_at=utcnow(), role="admin", plan="PLUS")
            target = User(username="delete-target@example.com", email="delete-target@example.com", password_hash=hash_password("secure-target-password"), verified_at=utcnow())
            db.add_all([admin, target]); db.flush()
            account = Account(owner_id=target.id, archive_uuid=archive_uuid, display_name="Delete me", email="delete-target@example.com", imap_enabled=False, mailbox_identity="d" * 64)
            db.add(account); db.flush()
            snapshot = Snapshot(account_id=account.id, snapshot_uuid="00000000-0000-0000-0000-000000000402", status="completed", completed_at=utcnow())
            db.add(snapshot); db.flush(); account.active_snapshot_id = snapshot.id
            db.add(TelegramLink(user_id=target.id, chat_id="444401"))
            db.add(SecurityToken(user_id=target.id, purpose="reset", token_hash="4" * 64, expires_at=utcnow()))
            export_path = EXPORTS_DIR / "user-delete-test.mailvault"; export_path.parent.mkdir(parents=True, exist_ok=True); export_path.write_bytes(b"archive")
            db.add(WebExport(public_id="00000000-0000-0000-0000-000000000403", owner_id=target.id, account_id=account.id, filename=export_path.name, relpath=export_path.name, size=7, expires_at=utcnow()))
            db.commit(); target_id, admin_id = target.id, admin.id
        archive_path = ARCHIVES_DIR / archive_uuid; archive_path.mkdir(parents=True, exist_ok=True); (archive_path / "original.eml").write_bytes(b"mail")

        assert client.post("/api/login", json={"username":"delete-admin@example.com", "password":"secure-admin-password"}).status_code == 200
        token = re.search(r'name="csrf-token" content="([^"]+)', client.get("/app").text).group(1)
        headers = {"X-CSRF-Token": token}
        assert client.request("DELETE", f"/api/admin/users/{target_id}", headers=headers, json={"confirmation":"wrong"}).status_code == 422
        assert client.request("DELETE", f"/api/admin/users/{admin_id}", headers=headers, json={"confirmation":"DELETE"}).status_code == 409
        response = client.request("DELETE", f"/api/admin/users/{target_id}", headers=headers, json={"confirmation":"DELETE"})
        assert response.status_code == 200, response.text
        assert not archive_path.exists() and not export_path.exists()
        with SessionLocal() as db:
            assert db.get(User, target_id) is None
            assert db.scalar(select(AdminAudit).where(AdminAudit.action == "user_delete", AdminAudit.target_id == str(target_id)))
            db.delete(db.get(User, admin_id)); db.commit()
