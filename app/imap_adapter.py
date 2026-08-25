from __future__ import annotations

import ssl
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator

from imapclient import IMAPClient

from .config import IMAP_TIMEOUT_SECONDS


def _value(mapping: dict, name: str, default=None):
    return mapping.get(name) if name in mapping else mapping.get(name.encode(), default)


def _flag_text(flag) -> str:
    text = flag.decode("utf-8", "replace") if isinstance(flag, bytes) else str(flag)
    return text.encode("utf-8", "replace").decode("utf-8")


@dataclass(slots=True)
class RemoteFolder:
    flags: list[str]
    delimiter: str | None
    name: str


@dataclass(slots=True)
class RemoteMessage:
    uid: int
    raw: bytes
    flags: list[str]
    internal_date: datetime | None


class StandardIMAPAdapter:
    """Provider-neutral IMAP adapter; no Gmail/Outlook APIs are required."""

    def __init__(self, host: str, port: int, security: str, username: str, password: str):
        self.host = host
        self.port = port
        self.security = security
        self.username = username
        self.password = password
        self.client: IMAPClient | None = None

    def connect(self) -> None:
        use_ssl = self.security == "ssl"
        ssl_context = ssl.create_default_context()
        self.client = IMAPClient(
            self.host,
            port=self.port,
            ssl=use_ssl,
            ssl_context=ssl_context if use_ssl else None,
            timeout=IMAP_TIMEOUT_SECONDS,
        )
        if self.security == "starttls":
            self.client.starttls(ssl_context=ssl_context)
        self.client.login(self.username, self.password)

    def capabilities(self) -> list[str]:
        assert self.client
        return sorted(_flag_text(item) for item in self.client.capabilities())

    def list_folders(self, root: str | None = None) -> list[RemoteFolder]:
        assert self.client
        rows = self.client.list_folders(directory=root or "", pattern="*")
        return [
            RemoteFolder(
                flags=[_flag_text(flag) for flag in flags],
                delimiter=_flag_text(delimiter) if delimiter else None,
                name=_flag_text(name),
            )
            for flags, delimiter, name in rows
        ]

    def select_folder(self, name: str) -> tuple[str | None, int]:
        assert self.client
        info = self.client.select_folder(name, readonly=True)
        uidvalidity = _value(info, "UIDVALIDITY")
        messages = int(_value(info, "EXISTS", 0) or 0)
        return str(uidvalidity) if uidvalidity is not None else None, messages

    def message_uids(self) -> list[int]:
        assert self.client
        return list(self.client.search(["ALL"]))

    def fetch_messages(self, uids: list[int]) -> Iterator[RemoteMessage]:
        assert self.client
        if not uids:
            return
        response = self.client.fetch(uids, ["RFC822", "FLAGS", "INTERNALDATE"])
        for uid in uids:
            item = response.get(uid, {})
            raw = _value(item, "RFC822")
            if raw is None:
                continue
            flags = [_flag_text(flag) for flag in (_value(item, "FLAGS", ()) or ())]
            yield RemoteMessage(
                uid=uid,
                raw=bytes(raw),
                flags=flags,
                internal_date=_value(item, "INTERNALDATE"),
            )

    def logout(self) -> None:
        if self.client is not None:
            try:
                self.client.logout()
            except Exception:
                pass
            finally:
                self.client = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_args):
        self.logout()


def test_imap_connection(host: str, port: int, security: str, username: str, password: str) -> dict:
    with StandardIMAPAdapter(host, port, security, username, password) as adapter:
        folders = adapter.list_folders()
        return {"ok": True, "folders": len(folders), "capabilities": adapter.capabilities()}
