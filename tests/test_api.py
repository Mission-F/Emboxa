import re

from fastapi.testclient import TestClient

from app.main import app


def test_setup_session_csrf_and_account_crud():
    with TestClient(app) as client:
        assert client.get("/api/health").json() == {"status": "ok"}
        response = client.post("/api/setup", json={"username": "admin", "password": "a-strong-local-password"})
        assert response.status_code == 200
        page = client.get("/")
        assert page.status_code == 200
        csrf = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
        payload = {"display_name":"Mailbox","email":"mailbox@example.com","imap_host":"imap.example.com","imap_port":993,"security":"ssl","imap_username":"mailbox@example.com","password":"app-password","root_folder":None,"schedule_mode":"disabled","schedule_interval_hours":None}
        assert client.post("/api/accounts", json=payload).status_code == 403
        created = client.post("/api/accounts", json=payload, headers={"X-CSRF-Token":csrf})
        assert created.status_code == 200, created.text
        accounts = client.get("/api/accounts")
        assert accounts.status_code == 200
        assert any(item["email"] == "mailbox@example.com" for item in accounts.json())

    with TestClient(app) as anonymous:
        assert anonymous.get("/api/accounts").status_code == 401

