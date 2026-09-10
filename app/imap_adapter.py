from __future__ import annotations

import ssl
from dataclasses import dataclass
from datetime import datetime
from email.header import decode_header, make_header
from email.utils import format_datetime
from typing import Iterator

from imapclient import IMAPClient

from .config import IMAP_TIMEOUT_SECONDS


def _value(mapping: dict, name: str, default=None):
    return mapping.get(name) if name in mapping else mapping.get(name.encode(), default)


BODY_MISSING_HEADER = "X-Emboxa-Body-Unavailable"
BODY_MISSING_FLAG = "$EmboxaBodyUnavailable"

BODY_MISSING_NOTE = (
    "Il server di posta ha rifiutato di consegnare il corpo di questo messaggio "
    "(\"[UNAVAILABLE] UID FETCH Server error\"), anche dopo più tentativi a distanza di tempo. "
    "Le intestazioni qui sopra sono tutto ciò che il server ha fornito: mittente, oggetto, data e "
    "destinatari sono quelli originali. Il contenuto non esiste più lato server, oppure è "
    "danneggiato: nessun client di posta può scaricarlo."
)


def _address_header(addresses) -> str:
    parts = []
    for address in addresses or ():
        mailbox, host = getattr(address, "mailbox", None), getattr(address, "host", None)
        if not mailbox or not host:
            continue
        email = f"{_header_text(mailbox)}@{_header_text(host)}"
        name = _header_text(getattr(address, "name", None))
        parts.append(f'"{name}" <{email}>' if name else email)
    return ", ".join(parts)


def _headers_from_envelope(envelope) -> bytes:
    """Rebuild the essential headers of a message from its ENVELOPE, for when BODY[HEADER] fails."""
    lines = []
    for name, addresses in (("From", "from_"), ("Sender", "sender"), ("Reply-To", "reply_to"),
                            ("To", "to"), ("Cc", "cc"), ("Bcc", "bcc")):
        value = _address_header(getattr(envelope, addresses, None))
        if value:
            lines.append(f"{name}: {value}")
    date = getattr(envelope, "date", None)
    if date:
        lines.append(f"Date: {format_datetime(date)}")
    subject = _header_text(getattr(envelope, "subject", None))
    if subject:
        lines.append(f"Subject: {subject}")
    for name, attr in (("Message-ID", "message_id"), ("In-Reply-To", "in_reply_to")):
        value = _header_text(getattr(envelope, attr, None))
        if value:
            lines.append(f"{name}: {value}")
    return ("\r\n".join(lines) + "\r\n").encode("utf-8", "replace")


def _stub_message(headers: bytes) -> bytes:
    """An RFC 822 message carrying the original headers and, in place of the body, an explanation."""
    headers = headers.rstrip(b"\r\n") + b"\r\n"
    extra = (
        f"{BODY_MISSING_HEADER}: yes\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "Content-Transfer-Encoding: 8bit\r\n"
    ).encode()
    return headers + extra + b"\r\n" + BODY_MISSING_NOTE.encode("utf-8") + b"\r\n"


def _header_text(value) -> str:
    """Decode a header that may arrive as bytes, MIME-encoded words, or malformed legacy bytes."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    try:
        value = str(make_header(decode_header(str(value))))
    except Exception:
        value = str(value)
    return value.encode("utf-8", "replace").decode("utf-8").strip()


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
    uid: int | str
    raw: bytes
    flags: list[str]
    internal_date: datetime | None
    body_missing: bool = False


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

    def ensure_folder(self, name: str) -> None:
        """Create a destination folder only when it does not already exist."""
        assert self.client
        if not self.client.folder_exists(name):
            self.client.create_folder(name)

    def select_write_folder(self, name: str) -> None:
        assert self.client
        self.client.select_folder(name, readonly=False)

    def uids_before(self, cutoff) -> list[int]:
        """UIDs the server considers older than `cutoff`.

        IMAP BEFORE tests INTERNALDATE — when the message reached the mailbox — not the Date
        header the sender wrote. For deciding what is old that is the sounder of the two, and it
        is the only one available without downloading every message first.
        """
        assert self.client
        return [int(uid) for uid in self.client.search(["BEFORE", cutoff])]

    def delete_uids(self, uids: list[int]) -> int:
        """Flag the given messages deleted and expunge them. Returns how many were asked for.

        UID EXPUNGE where the server offers UIDPLUS, which removes exactly these messages and
        nothing else. A plain EXPUNGE removes everything in the folder currently carrying the
        \\Deleted flag — including messages another client flagged and has not yet expunged — so
        it is a fallback, not the default.
        """
        assert self.client
        if not uids:
            return 0
        self.client.delete_messages(uids)
        if b"UIDPLUS" in (self.client.capabilities() or ()) or "UIDPLUS" in self.capabilities():
            self.client.expunge(uids)
        else:
            self.client.expunge()
        return len(uids)

    def has_message_id(self, message_id: str) -> bool:
        """Check the selected folder for a duplicate without downloading messages."""
        assert self.client
        if not message_id:
            return False
        return bool(self.client.search(["HEADER", "Message-ID", message_id]))

    def append_message(
        self, folder: str, raw: bytes, flags: list[str] | None = None, internal_date: datetime | None = None
    ) -> None:
        """Append the original RFC822 bytes; MIME is never reconstructed."""
        assert self.client
        safe_flags = [flag for flag in (flags or []) if flag.lower() in {
            "\\seen", "\\answered", "\\flagged", "\\draft"
        }]
        self.client.append(folder, raw, flags=safe_flags, msg_time=internal_date)

    def message_uids(self, expected: int | None = None) -> list[int]:
        """Every UID in the selected folder, even when the server truncates SEARCH.

        Yahoo answers `SEARCH ALL` with at most 10 000 UIDs and reports no error, so a mailbox
        with 38 000 messages silently looks like a mailbox with 10 000. EXISTS from SELECT is
        authoritative, so when SEARCH comes back short we re-enumerate by sequence number, which
        no provider caps.
        """
        assert self.client
        uids = list(self.client.search(["ALL"]))
        if expected is not None and len(uids) < expected:
            recovered = self._uids_by_sequence(expected)
            if len(recovered) > len(uids):
                return recovered
        return uids

    def _uids_by_sequence(self, expected: int, chunk: int = 2000) -> list[int]:
        """Map sequence numbers 1..EXISTS to UIDs in chunks, bypassing any SEARCH limit."""
        assert self.client
        collected: list[int] = []
        previous = self.client.use_uid
        self.client.use_uid = False
        try:
            for start in range(1, expected + 1, chunk):
                end = min(start + chunk - 1, expected)
                response = self.client.fetch(f"{start}:{end}", ["UID"])
                for item in response.values():
                    uid = _value(item, "UID")
                    if uid is not None:
                        collected.append(int(uid))
        finally:
            self.client.use_uid = previous
        return sorted(set(collected))

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

    def is_alive(self) -> bool:
        """True when the connection can still be used for another command.

        A refused FETCH does not mean the link is gone. Yahoo answers
        ``[UNAVAILABLE] UID FETCH Server error`` on messages it cannot read and then keeps talking
        normally, so tearing the session down and logging in again — about twenty-five seconds
        against a 38 000-message folder — repaired nothing. NOOP asks the connection itself instead
        of guessing from the error text, which no two providers word the same way.
        """
        if self.client is None:
            return False
        try:
            self.client.noop()
            return True
        except Exception:
            return False

    def message_summary(self, uid: int) -> str:
        """Describe a message whose body the server refuses, or "" if even that fails.

        ENVELOPE is a different fetch item from RFC822, so it often survives when the body does
        not. It is the only way to tell the owner *which* emails a provider is withholding, which
        is what they need before emptying the mailbox those emails still live in.
        """
        if self.client is None:
            return ""
        try:
            response = self.client.fetch([uid], ["ENVELOPE"])
        except Exception:
            return ""
        envelope = _value(response.get(uid, {}), "ENVELOPE")
        if envelope is None:
            return ""
        subject = _header_text(getattr(envelope, "subject", None)) or "(senza oggetto)"
        sender = ""
        senders = getattr(envelope, "from_", None) or ()
        if senders:
            mailbox, host = getattr(senders[0], "mailbox", None), getattr(senders[0], "host", None)
            if mailbox and host:
                sender = f"{_header_text(mailbox)}@{_header_text(host)}"
        date = getattr(envelope, "date", None)
        parts = [f'"{subject}"']
        if sender:
            parts.append(f"da {sender}")
        if date:
            parts.append(f"del {date:%d/%m/%Y}")
        return " ".join(parts)

    def fetch_headers_only(self, uid: int) -> RemoteMessage | None:
        """Keep what the server *will* give of a message whose body it refuses, or None if nothing.

        The mailbox is the owner's; a message the provider has broken is still theirs, and its
        sender, subject, date and recipients are worth keeping even when the text is gone. Yahoo
        refuses RFC822 on such messages but still answers BODY[HEADER], and failing that ENVELOPE,
        so the archive gets a message with the original headers and an explanatory body, marked
        with both a header and a flag so it can never be mistaken for the real thing.
        """
        if self.client is None:
            return None
        headers = None
        item: dict = {}
        try:
            response = self.client.fetch([uid], ["BODY.PEEK[HEADER]", "FLAGS", "INTERNALDATE"])
            item = response.get(uid, {})
            headers = _value(item, "BODY[HEADER]")
        except Exception:
            pass
        if headers is None:
            try:
                response = self.client.fetch([uid], ["ENVELOPE", "FLAGS", "INTERNALDATE"])
                item = response.get(uid, {})
            except Exception:
                return None
            envelope = _value(item, "ENVELOPE")
            if envelope is None:
                return None
            headers = _headers_from_envelope(envelope)
        flags = [_flag_text(flag) for flag in (_value(item, "FLAGS", ()) or ())]
        return RemoteMessage(
            uid=uid,
            raw=_stub_message(bytes(headers)),
            flags=flags + [BODY_MISSING_FLAG],
            internal_date=_value(item, "INTERNALDATE"),
            body_missing=True,
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
