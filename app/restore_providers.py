"""Restore providers.

Every destination mailbox is written through one of these targets:

    Restore Providers
    ├── Microsoft Graph   (OAuth mailboxes: no IMAP APPEND)
    ├── Gmail             (transported over IMAP with an app password)
    └── Generic IMAP      (any other server)

The picker is server-side: callers only pass the destination account or the
temporary credentials, and :func:`build_restore_target` returns the right
implementation. The UI never has to know which technology is used.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone
from email import message_from_bytes, policy
from email.utils import parseaddr
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .graph_adapter import GraphError, graph_request, refresh_access_token
from .imap_adapter import StandardIMAPAdapter, test_imap_connection

log = logging.getLogger("emboxa.restore")

# Graph accepts a MIME import up to 4 MB of base64 payload; bigger messages are
# rebuilt as a structured message plus upload sessions for the attachments.
MIME_IMPORT_LIMIT = 3_900_000
INLINE_ATTACHMENT_LIMIT = 3 * 1024 * 1024
UPLOAD_CHUNK = 320 * 1024 * 10

GMAIL_HOSTS = {"imap.gmail.com", "imap.googlemail.com"}


class RestoreTarget:
    """Common surface used by the restore worker, whatever the backend is."""

    provider = "imap"
    provider_label = "Generic IMAP"

    def connect(self) -> None:
        raise NotImplementedError

    def probe(self) -> dict:
        """Validate the destination without writing anything."""
        raise NotImplementedError

    def prepare_folder(self, path: str) -> None:
        """Create the destination folder when missing and make it current."""
        raise NotImplementedError

    def has_message(self, message_id: str) -> bool:
        raise NotImplementedError

    def deliver(self, raw: bytes, flags: list[str], internal_date: datetime | None) -> None:
        raise NotImplementedError

    def rotated_secret(self) -> str | None:
        """New secret to persist when the provider rotated it (OAuth refresh)."""
        return None

    def logout(self) -> None:
        pass


class IMAPRestoreTarget(RestoreTarget):
    """Generic IMAP and Gmail destinations: original RFC822 bytes via APPEND."""

    def __init__(self, host: str, port: int, security: str, username: str, password: str, provider: str = "imap"):
        self.adapter = StandardIMAPAdapter(host, port, security, username, password)
        self.provider = provider
        self.provider_label = "Gmail" if provider == "gmail" else "Generic IMAP"
        self._host, self._port, self._security = host, port, security
        self._username, self._password = username, password
        self._folder = ""

    def connect(self) -> None:
        self.adapter.connect()

    def probe(self) -> dict:
        return test_imap_connection(self._host, self._port, self._security, self._username, self._password)

    def prepare_folder(self, path: str) -> None:
        self.adapter.ensure_folder(path)
        self.adapter.select_write_folder(path)
        self._folder = path

    def has_message(self, message_id: str) -> bool:
        return self.adapter.has_message_id(message_id)

    def deliver(self, raw: bytes, flags: list[str], internal_date: datetime | None) -> None:
        self.adapter.append_message(self._folder, raw, flags, internal_date)

    def logout(self) -> None:
        self.adapter.logout()


def _graph_datetime(value: datetime | None) -> str | None:
    if not value:
        return None
    moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _recipients(message, header: str) -> list[dict]:
    people = []
    for item in (message.get_all(header) or []):
        for chunk in str(item).split(","):
            name, address = parseaddr(chunk)
            if address:
                people.append({"emailAddress": {"address": address[:320], "name": (name or address)[:200]}})
    return people[:200]


class MicrosoftGraphRestoreTarget(RestoreTarget):
    """Restore into Outlook/Microsoft 365 through Graph instead of IMAP APPEND.

    Folders are created on demand, messages are imported with their original MIME
    when they fit, and larger ones are rebuilt with HTML body plus attachments.
    """

    provider = "microsoft_graph"
    provider_label = "Microsoft Graph"

    def __init__(self, refresh_token: str):
        self.refresh_token = refresh_token
        self._initial_token = refresh_token
        self.access_token = ""
        self.folder_ids: dict[str, str] = {}
        self.current_folder_id = ""

    # -- transport -------------------------------------------------------
    def connect(self) -> None:
        try:
            token = refresh_access_token(self.refresh_token)
        except Exception as exc:  # HTTPException/urllib errors would leak transport details
            log.info("Microsoft token refresh failed: %s", exc)
            raise RuntimeError("Autorizzazione Microsoft non valida o scaduta: ricollega la casella") from exc
        if not token.get("access_token"):
            raise RuntimeError("Autorizzazione Microsoft non valida o scaduta: ricollega la casella")
        self.access_token = token["access_token"]
        self.refresh_token = token.get("refresh_token") or self.refresh_token

    def rotated_secret(self) -> str | None:
        return self.refresh_token if self.refresh_token != self._initial_token else None

    def _call(self, method: str, path: str, body=None, content_type: str = "application/json", retry: bool = True):
        try:
            return graph_request(self.access_token, method, path, body, content_type)
        except GraphError as exc:
            # Access tokens live one hour: a long restore has to refresh mid-flight.
            if retry and exc.status in (401, 429, 503):
                self.connect()
                return self._call(method, path, body, content_type, retry=False)
            raise

    def probe(self) -> dict:
        self.connect()
        data = self._call("GET", "/me/mailFolders?$top=100&$select=id,displayName")
        return {"ok": True, "folders": len(data.get("value", [])), "capabilities": ["MICROSOFT_GRAPH", "OAUTH2"]}

    # -- folders ---------------------------------------------------------
    def _children(self, parent_id: str | None) -> dict[str, str]:
        path = (
            f"/me/mailFolders/{quote(parent_id, safe='')}/childFolders?$top=200&$select=id,displayName"
            if parent_id else "/me/mailFolders?$top=200&$select=id,displayName"
        )
        found: dict[str, str] = {}
        while path:
            data = self._call("GET", path)
            for item in data.get("value", []):
                found[item["displayName"].casefold()] = item["id"]
            path = data.get("@odata.nextLink") or ""
        return found

    def _create_folder(self, parent_id: str | None, name: str) -> str:
        path = f"/me/mailFolders/{quote(parent_id, safe='')}/childFolders" if parent_id else "/me/mailFolders"
        return self._call("POST", path, {"displayName": name})["id"]

    def prepare_folder(self, path: str) -> None:
        cached = self.folder_ids.get(path)
        if cached:
            self.current_folder_id = cached
            return
        parent_id: str | None = None
        walked = ""
        for segment in [part for part in str(path).replace("\\", "/").split("/") if part.strip()]:
            name = segment.strip()[:250]
            walked = f"{walked}/{name}" if walked else name
            known = self.folder_ids.get(walked)
            if not known:
                known = self._children(parent_id).get(name.casefold()) or self._create_folder(parent_id, name)
                self.folder_ids[walked] = known
            parent_id = known
        if not parent_id:
            raise RuntimeError("Cartella di destinazione non valida")
        self.current_folder_id = parent_id
        self.folder_ids[path] = parent_id

    # -- messages --------------------------------------------------------
    def has_message(self, message_id: str) -> bool:
        if not message_id:
            return False
        escaped = message_id.replace("'", "''")
        query = (
            f"/me/mailFolders/{quote(self.current_folder_id, safe='')}/messages?"
            + urlencode({"$filter": f"internetMessageId eq '{escaped}'", "$select": "id", "$top": "1"})
        )
        try:
            return bool(self._call("GET", query).get("value"))
        except GraphError as exc:
            log.debug("Graph duplicate lookup failed (%s); importing anyway", exc)
            return False

    def deliver(self, raw: bytes, flags: list[str], internal_date: datetime | None) -> None:
        encoded = base64.b64encode(raw)
        created = None
        if len(encoded) <= MIME_IMPORT_LIMIT:
            try:
                created = self._call(
                    "POST",
                    f"/me/mailFolders/{quote(self.current_folder_id, safe='')}/messages",
                    encoded,
                    content_type="text/plain",
                )
            except GraphError as exc:
                if exc.status not in (400, 413, 0):
                    raise
                log.info("MIME import refused (%s); rebuilding message for Graph", exc)
        if created is None:
            created = self._deliver_structured(raw, flags, internal_date)
        self._apply_flags(created, flags)

    def _apply_flags(self, created: dict, flags: list[str]) -> None:
        lowered = {flag.lower() for flag in (flags or [])}
        wanted_read = "\\seen" in lowered
        wanted_flagged = "\\flagged" in lowered
        changes: dict = {}
        if bool(created.get("isRead")) != wanted_read:
            changes["isRead"] = wanted_read
        if wanted_flagged and (created.get("flag") or {}).get("flagStatus") != "flagged":
            changes["flag"] = {"flagStatus": "flagged"}
        if not changes or not created.get("id"):
            return
        try:
            self._call("PATCH", f"/me/messages/{quote(created['id'], safe='')}", changes)
        except GraphError as exc:
            log.debug("Graph flag update skipped: %s", exc)

    def _deliver_structured(self, raw: bytes, flags: list[str], internal_date: datetime | None) -> dict:
        """Rebuild subject, HTML body, metadata and attachments when MIME import is not possible."""
        parsed = message_from_bytes(raw, policy=policy.default)
        html = parsed.get_body(preferencelist=("html",))
        text = parsed.get_body(preferencelist=("plain",))
        chosen = html or text
        content = ""
        if chosen is not None:
            try:
                content = chosen.get_content()
            except Exception:
                content = ""
        sender_name, sender_address = parseaddr(str(parsed.get("From", "")))
        received = _graph_datetime(internal_date)
        payload: dict = {
            "subject": str(parsed.get("Subject", ""))[:255],
            "body": {"contentType": "HTML" if chosen is html and html is not None else "Text", "content": content},
            "toRecipients": _recipients(parsed, "To"),
            "ccRecipients": _recipients(parsed, "Cc"),
            "isRead": "\\seen" in {flag.lower() for flag in (flags or [])},
        }
        if sender_address:
            payload["from"] = {"emailAddress": {"address": sender_address[:320], "name": (sender_name or sender_address)[:200]}}
        if received:
            payload["receivedDateTime"] = received
            payload["sentDateTime"] = received
        message_id = str(parsed.get("Message-ID", "")).strip()
        if message_id:
            payload["internetMessageId"] = message_id[:1000]

        small, large = [], []
        for part in parsed.iter_attachments():
            try:
                content_bytes = part.get_payload(decode=True) or b""
            except Exception:
                continue
            name = part.get_filename() or "attachment"
            item = (name[:250], part.get_content_type(), content_bytes, bool(part.get("Content-ID")), part.get("Content-ID"))
            (small if len(content_bytes) <= INLINE_ATTACHMENT_LIMIT else large).append(item)
        if small:
            payload["attachments"] = [{
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": name,
                "contentType": content_type,
                "contentBytes": base64.b64encode(data).decode(),
                "isInline": inline,
                **({"contentId": str(content_id).strip("<>")} if content_id else {}),
            } for name, content_type, data, inline, content_id in small]

        path = f"/me/mailFolders/{quote(self.current_folder_id, safe='')}/messages"
        try:
            created = self._call("POST", path, payload)
        except GraphError as exc:
            if exc.status != 400:
                raise
            # Some tenants refuse writable envelope fields: retry with the safe subset.
            for key in ("from", "receivedDateTime", "sentDateTime", "internetMessageId"):
                payload.pop(key, None)
            created = self._call("POST", path, payload)
        for name, content_type, data, _inline, _content_id in large:
            self._upload_attachment(created["id"], name, content_type, data)
        return created

    def _upload_attachment(self, message_id: str, name: str, content_type: str, data: bytes) -> None:
        session = self._call(
            "POST",
            f"/me/messages/{quote(message_id, safe='')}/attachments/createUploadSession",
            {"AttachmentItem": {"attachmentType": "file", "name": name, "size": len(data), "contentType": content_type}},
        )
        upload_url = session.get("uploadUrl")
        if not upload_url:
            raise RuntimeError(f"Microsoft Graph ha rifiutato l'upload dell'allegato {name}")
        total = len(data)
        for start in range(0, total, UPLOAD_CHUNK):
            chunk = data[start:start + UPLOAD_CHUNK]
            end = start + len(chunk) - 1
            request = Request(upload_url, data=chunk, method="PUT", headers={
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {start}-{end}/{total}",
            })
            with urlopen(request, timeout=180) as response:
                response.read()

    def logout(self) -> None:
        self.access_token = ""


def restore_provider_id(account) -> str:
    """Provider identifier used by the UI and by :func:`build_restore_target`."""
    if getattr(account, "auth_provider", "imap") == "microsoft":
        return "microsoft_graph"
    if (getattr(account, "imap_host", "") or "").lower() in GMAIL_HOSTS:
        return "gmail"
    return "imap"


# Names shown in the UI: the user picks a mailbox, not a technology.
PROVIDER_LABELS = {"microsoft_graph": "Microsoft", "gmail": "Gmail", "imap": "IMAP"}


def account_restore_support(account) -> dict:
    """Whether a saved mailbox can receive a restore, and with which provider."""
    provider = restore_provider_id(account)
    if getattr(account, "auth_provider", "imap") == "mbox":
        return {"provider": provider, "label": PROVIDER_LABELS[provider], "supported": False,
                "reason": "Le caselle MBOX offline non hanno un server su cui ripristinare"}
    if not getattr(account, "imap_enabled", False) or not getattr(account, "encrypted_password", None):
        return {"provider": provider, "label": PROVIDER_LABELS[provider], "supported": False,
                "reason": "Ricollega questa casella prima di usarla come destinazione di ripristino"}
    return {"provider": provider, "label": PROVIDER_LABELS[provider], "supported": True, "reason": ""}


def build_restore_target(credentials: dict) -> RestoreTarget:
    """Pick the restore implementation from resolved destination credentials."""
    provider = credentials.get("provider") or "imap"
    if provider == "microsoft_graph":
        return MicrosoftGraphRestoreTarget(credentials["password"])
    return IMAPRestoreTarget(
        credentials.get("host") or "",
        int(credentials.get("port") or 993),
        credentials.get("security") or "ssl",
        credentials.get("username") or "",
        credentials.get("password") or "",
        provider=provider,
    )
