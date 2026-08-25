from __future__ import annotations

import re

from fastapi.testclient import TestClient
from sqlalchemy import select

import app.main as main
from app.database import SessionLocal
from app.main import app
from app.models import AdminAudit, AppSetting, User, utcnow
from app.security import decrypt_secret, hash_password


def _login(client: TestClient, email: str, password: str) -> dict[str, str]:
    response = client.post("/api/login", json={"username": email, "password": password})
    assert response.status_code == 200, response.text
    page = client.get("/app")
    token = re.search(r'name="csrf-token" content="([^"]+)', page.text).group(1)
    return {"X-CSRF-Token": token}


def test_admin_settings_authorization_persistence_secrets_and_auth_ui(monkeypatch):
    with SessionLocal() as db:
        admin = User(username="settings-admin@example.com", email="settings-admin@example.com",
                     password_hash=hash_password("settings-admin-password"), verified_at=utcnow(),
                     role="admin", plan="PLUS", storage_limit_bytes=0)
        normal = User(username="settings-user@example.com", email="settings-user@example.com",
                      password_hash=hash_password("settings-user-password"), verified_at=utcnow())
        db.add_all([admin, normal]); db.commit()

    with TestClient(app) as normal_client:
        _login(normal_client, "settings-user@example.com", "settings-user-password")
        assert normal_client.get("/admin").status_code == 404
        assert normal_client.get("/api/admin/settings").status_code == 404
        assert "Administration" not in normal_client.get("/app").text

    class DummySMTP:
        def __enter__(self): return self
        def __exit__(self, *_args): return False

    monkeypatch.setattr(main, "_smtp_client", lambda: DummySMTP())
    def telegram_request(_token, method, _payload):
        if method == "getMe":
            return {"username": "emboxa_test_bot"}
        if method == "getWebhookInfo":
            return {"url": "https://emboxa.eu/api/telegram/webhook"}
        return True
    monkeypatch.setattr(main, "_telegram_request", telegram_request)
    monkeypatch.setattr(main, "_telegram_call", lambda method, payload: {"username": "emboxa_test_bot"})

    with TestClient(app) as admin_client:
        headers = _login(admin_client, "settings-admin@example.com", "settings-admin-password")
        assert "Administration" in admin_client.get("/app").text
        settings = admin_client.get("/api/admin/settings").json()
        for read_only in ("smtp_password_set", "smtp_password_masked", "telegram_connected",
                          "telegram_bot_token_set", "telegram_bot_token_masked", "telegram_links",
                          "telegram_webhook_status", "telegram_webhook_error"):
            settings.pop(read_only, None)
        settings.update({
            "smtp_enabled": True, "smtp_host": "smtp.example.com", "smtp_from_email": "mail@example.com",
            "smtp_password": "smtp-secret-value", "telegram_enabled": True,
            "telegram_bot_token": "telegram-secret-value", "telegram_bot_username": "emboxa_test_bot",
            "registration_enabled": False, "standard_mailbox_limit": 6,
            "google_analytics_id": "G-TEST123", "analytics_enabled": True,
        })
        saved = admin_client.put("/api/admin/settings", json=settings, headers=headers)
        assert saved.status_code == 200, saved.text
        assert admin_client.post("/api/admin/smtp/test", json={"email": None}, headers=headers).json()["message"] == "Connection successful"
        telegram_test = admin_client.post("/api/admin/telegram/test", headers=headers).json()
        assert telegram_test["message"] == "Bot connected" and telegram_test["webhook_status"] == "connected"
        result = admin_client.get("/api/admin/settings").json()
        assert result["smtp_password_masked"] == "••••••••••••" and "smtp_password" not in result
        assert result["telegram_bot_token_masked"] == "••••••••••••" and "telegram_bot_token" not in result
        assert result["telegram_connected"] and result["telegram_webhook_status"] == "connected"
        assert admin_client.post("/api/register", json={"email": "disabled@example.com", "password": "disabled-registration"}).status_code == 403

    with TestClient(app) as restarted:
        _login(restarted, "settings-admin@example.com", "settings-admin-password")
        assert restarted.get("/api/admin/settings").json()["standard_mailbox_limit"] == 6

    with SessionLocal() as db:
        smtp = db.get(AppSetting, "smtp_password")
        telegram = db.get(AppSetting, "telegram_bot_token")
        assert smtp.encrypted and smtp.value != "smtp-secret-value" and decrypt_secret(smtp.value) == "smtp-secret-value"
        assert telegram.encrypted and telegram.value != "telegram-secret-value" and decrypt_secret(telegram.value) == "telegram-secret-value"
        assert db.scalar(select(AdminAudit).where(AdminAudit.action == "settings_update"))
        for row in db.scalars(select(AppSetting)).all(): db.delete(row)
        for user in db.scalars(select(User).where(User.email.in_(["settings-admin@example.com", "settings-user@example.com"]))).all(): db.delete(user)
        db.commit()

    login = TestClient(app).get("/login").text
    register = TestClient(app).get("/register").text
    verify = TestClient(app).get("/verify").text
    reset = TestClient(app).get("/reset-password").text
    assert 'autocomplete="email"' in login and 'autocomplete="current-password"' in login
    assert 'name="confirm_password"' in register and register.count('autocomplete="new-password"') == 2
    assert 'autocomplete="one-time-code"' in verify and 'id="resend-code"' in verify
    assert 'name="confirm_password"' in reset and 'autocomplete="new-password"' in reset


def test_valid_telegram_token_remains_connected_when_webhook_is_unreachable(monkeypatch):
    with SessionLocal() as db:
        admin = User(username="telegram-admin@example.com", email="telegram-admin@example.com",
                     password_hash=hash_password("telegram-admin-password"), verified_at=utcnow(),
                     role="admin", plan="PLUS", storage_limit_bytes=0)
        db.add(admin); db.commit()

    def telegram_request(_token, method, _payload):
        if method == "getMe":
            return {"username": "emboxa_valid_bot"}
        raise main.HTTPException(502, "Telegram connection failed")

    monkeypatch.setattr(main, "_telegram_request", telegram_request)
    with TestClient(app) as client:
        headers = _login(client, "telegram-admin@example.com", "telegram-admin-password")
        saved = client.post("/api/admin/telegram/connect",
                            json={"token": "valid-token-with-unreachable-webhook"}, headers=headers)
        assert saved.status_code == 200, saved.text
        assert saved.json()["connected"] is True
        assert saved.json()["webhook_status"] == "failed"
        result = client.get("/api/admin/settings").json()
        assert result["telegram_connected"] is True
        assert result["telegram_webhook_status"] == "failed"

    with SessionLocal() as db:
        for row in db.scalars(select(AppSetting)).all(): db.delete(row)
        admin = db.scalar(select(User).where(User.email == "telegram-admin@example.com"))
        if admin: db.delete(admin)
        db.commit()
