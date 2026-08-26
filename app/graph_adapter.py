from __future__ import annotations

import json
from datetime import datetime
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from fastapi import HTTPException

from .config import MICROSOFT_CLIENT_ID, MICROSOFT_CLIENT_SECRET, MICROSOFT_TENANT
from .imap_adapter import RemoteFolder, RemoteMessage

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
MICROSOFT_SCOPES = "openid email profile offline_access User.Read Mail.ReadWrite"


def _json_request(url: str, payload: dict | None = None, headers: dict | None = None, method: str | None = None):
    data = None if payload is None else urlencode(payload).encode()
    request = Request(url, data=data, headers=headers or {}, method=method or ("POST" if payload is not None else "GET"))
    try:
        with urlopen(request, timeout=30) as response:
            body = response.read()
    except Exception as exc:
        raise HTTPException(502, "Microsoft Graph connection failed") from exc
    return json.loads(body.decode() or "{}")


def microsoft_authorize_url(redirect_uri: str, state: str) -> str:
    if not MICROSOFT_CLIENT_ID or not MICROSOFT_CLIENT_SECRET:
        raise HTTPException(409, "Microsoft OAuth is not configured")
    query = urlencode({
        "client_id": MICROSOFT_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": MICROSOFT_SCOPES,
        "state": state,
        "prompt": "select_account",
    })
    return f"https://login.microsoftonline.com/{MICROSOFT_TENANT}/oauth2/v2.0/authorize?{query}"


def exchange_code(code: str, redirect_uri: str) -> dict:
    return _json_request(f"https://login.microsoftonline.com/{MICROSOFT_TENANT}/oauth2/v2.0/token", {
        "client_id": MICROSOFT_CLIENT_ID,
        "client_secret": MICROSOFT_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "scope": MICROSOFT_SCOPES,
    })


def refresh_access_token(refresh_token: str) -> dict:
    return _json_request(f"https://login.microsoftonline.com/{MICROSOFT_TENANT}/oauth2/v2.0/token", {
        "client_id": MICROSOFT_CLIENT_ID,
        "client_secret": MICROSOFT_CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": MICROSOFT_SCOPES,
    })


def graph_json(access_token: str, path_or_url: str) -> dict:
    url = path_or_url if path_or_url.startswith("https://") else f"{GRAPH_ROOT}{path_or_url}"
    return _json_request(url, headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"})


def graph_bytes(access_token: str, path: str) -> bytes:
    request = Request(f"{GRAPH_ROOT}{path}", headers={"Authorization": f"Bearer {access_token}"})
    try:
        with urlopen(request, timeout=60) as response:
            return response.read()
    except Exception as exc:
        raise RuntimeError("Microsoft Graph message download failed") from exc


def microsoft_profile(access_token: str) -> dict:
    return graph_json(access_token, "/me?$select=id,displayName,mail,userPrincipalName")


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class MicrosoftGraphAdapter:
    """Read Outlook/Microsoft 365 mail through Graph while exposing the IMAP-like backup interface."""

    def __init__(self, refresh_token: str):
        self.refresh_token = refresh_token
        self.access_token = ""
        self.current_folder_id = ""
        self.folder_ids: dict[str, str] = {}
        self.messages: dict[str, dict] = {}

    def connect(self) -> None:
        token = refresh_access_token(self.refresh_token)
        self.access_token = token["access_token"]
        self.refresh_token = token.get("refresh_token") or self.refresh_token

    def _paged(self, path: str):
        url = f"{GRAPH_ROOT}{path}"
        while url:
            data = graph_json(self.access_token, url)
            for item in data.get("value", []):
                yield item
            url = data.get("@odata.nextLink")

    def list_folders(self, root: str | None = None) -> list[RemoteFolder]:
        self.folder_ids.clear()
        folders: list[RemoteFolder] = []

        def walk(parent_id: str | None, prefix: str = "") -> None:
            path = (
                f"/me/mailFolders/{quote(parent_id, safe='')}/childFolders?$top=100&includeHiddenFolders=true"
                if parent_id else "/me/mailFolders?$top=100&includeHiddenFolders=true"
            )
            for item in self._paged(path):
                name = f"{prefix}/{item['displayName']}" if prefix else item["displayName"]
                self.folder_ids[name] = item["id"]
                folders.append(RemoteFolder(flags=[], delimiter="/", name=name))
                if item.get("childFolderCount"):
                    walk(item["id"], name)

        walk(None)
        if root:
            folders = [folder for folder in folders if folder.name == root or folder.name.startswith(f"{root}/")]
        return folders

    def select_folder(self, name: str) -> tuple[str | None, int]:
        self.current_folder_id = self.folder_ids.get(name, "")
        if not self.current_folder_id:
            raise RuntimeError(f"Microsoft folder not found: {name}")
        info = graph_json(self.access_token, f"/me/mailFolders/{quote(self.current_folder_id, safe='')}?$select=id,totalItemCount")
        return self.current_folder_id, int(info.get("totalItemCount") or 0)

    def message_uids(self) -> list[str]:
        self.messages.clear()
        path = f"/me/mailFolders/{quote(self.current_folder_id, safe='')}/messages?$top=50&$select=id,receivedDateTime,isRead,flag"
        ids: list[str] = []
        for item in self._paged(path):
            ids.append(item["id"])
            self.messages[item["id"]] = item
        return ids

    def fetch_messages(self, uids: list[str]):
        for uid in uids:
            safe_uid = quote(uid, safe="")
            meta = self.messages.get(uid) or graph_json(self.access_token, f"/me/messages/{safe_uid}?$select=id,receivedDateTime,isRead,flag")
            flags = []
            if meta.get("isRead"):
                flags.append("\\Seen")
            if (meta.get("flag") or {}).get("flagStatus") == "flagged":
                flags.append("\\Flagged")
            yield RemoteMessage(
                uid=uid,
                raw=graph_bytes(self.access_token, f"/me/messages/{safe_uid}/$value"),
                flags=flags,
                internal_date=_dt(meta.get("receivedDateTime")),
            )

    def capabilities(self) -> list[str]:
        return ["MICROSOFT_GRAPH", "OAUTH2"]

    def logout(self) -> None:
        self.access_token = ""
