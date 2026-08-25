from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage, Message as EmailPart
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path

from .security import safe_filename


@dataclass(slots=True)
class ParsedAttachment:
    filename: str
    content_type: str
    size: int
    sha256: str
    relpath: str
    content_id: str | None
    is_inline: bool


@dataclass(slots=True)
class ParsedMail:
    message_id: str | None
    in_reply_to: str | None
    references_json: str
    thread_key: str
    subject: str
    sender: str
    recipients_to: str
    recipients_cc: str
    recipients_bcc: str
    reply_to: str
    date_utc: datetime | None
    headers_json: str
    text_body: str
    html_body: str
    mime_json: str
    attachments: list[ParsedAttachment]
    raw_sha256: str
    raw_relpath: str


def _safe_text(value: object | None) -> str:
    """Return UTF-8 encodable text while keeping the original EML untouched.

    Some IMAP providers (notably Yahoo) expose malformed legacy header bytes.
    Python deliberately represents those bytes as surrogate characters in
    ``raw_items()``; SQLite and UTF-8 JSON cannot encode them.  Replacing only
    those invalid code points keeps the backup usable while the byte-perfect
    source remains available in ``raw/``.
    """
    text = str(value or "")
    return text.encode("utf-8", "replace").decode("utf-8")


def _normalise_id(value: str | None) -> str | None:
    value = _safe_text(value)
    if not value:
        return None
    match = re.search(r"<[^>]+>", value)
    return (match.group(0) if match else value.strip())[:1000]


def _references(value: str | None) -> list[str]:
    value = _safe_text(value)
    if not value:
        return []
    found = re.findall(r"<[^>]+>", value)
    return found if found else [item for item in value.split() if item]


def _addresses(message: EmailMessage, header: str) -> str:
    values = message.get_all(header, [])
    return _safe_text(", ".join(
        f"{name} <{address}>" if name else address
        for name, address in getaddresses([str(value) for value in values])
        if name or address
    ))


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        result = parsedate_to_datetime(value)
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result.astimezone(timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError):
        return None


def _part_structure(part: EmailPart) -> dict:
    item = {
        "content_type": _safe_text(part.get_content_type()),
        "disposition": _safe_text(part.get_content_disposition()) or None,
        "filename": safe_filename(_safe_text(part.get_filename())) if part.get_filename() else None,
        "content_id": _safe_text(part.get("Content-ID")).strip("<>") or None,
    }
    if part.is_multipart():
        item["children"] = [_part_structure(child) for child in part.iter_parts()]
    return item


def _write_cas(directory: Path, payload: bytes) -> tuple[str, str]:
    digest = hashlib.sha256(payload).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / digest
    if not target.exists():
        temporary = directory / f".{digest}.tmp"
        temporary.write_bytes(payload)
        temporary.replace(target)
    return digest, target.name


def _plain_text_from_html(value: str) -> str:
    text = re.sub(r"<\s*(br|/p|/div|/li)\b[^>]*>", "\n", value, flags=re.I)
    return re.sub(r"<[^>]+>", " ", text)


def parse_and_store(raw: bytes, snapshot_dir: Path) -> ParsedMail:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    raw_sha, raw_name = _write_cas(snapshot_dir / "raw", raw)
    text_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[ParsedAttachment] = []

    for part in message.walk():
        if part.is_multipart():
            continue
        content_type = _safe_text(part.get_content_type())
        disposition = part.get_content_disposition()
        filename = part.get_filename()
        content_id = _safe_text(part.get("Content-ID")).strip("<>") or None
        is_attachment = disposition == "attachment" or bool(filename) or (disposition == "inline" and content_id)

        if is_attachment:
            payload = part.get_payload(decode=True) or b""
            digest, blob_name = _write_cas(snapshot_dir / "attachments", payload)
            attachments.append(ParsedAttachment(
                filename=safe_filename(_safe_text(filename), "inline" if content_id else "attachment"),
                content_type=content_type,
                size=len(payload),
                sha256=digest,
                relpath=f"attachments/{blob_name}",
                content_id=content_id,
                is_inline=disposition == "inline" or bool(content_id),
            ))
            continue

        if content_type in {"text/plain", "text/html"}:
            try:
                content = part.get_content()
            except (LookupError, UnicodeError):
                payload = part.get_payload(decode=True) or b""
                content = payload.decode(part.get_content_charset() or "utf-8", "replace")
            if content_type == "text/plain":
                text_parts.append(_safe_text(content))
            else:
                html_parts.append(_safe_text(content))

    text_body = "\n\n".join(text_parts)
    html_body = "\n".join(html_parts)
    if not text_body and html_body:
        text_body = _plain_text_from_html(html_body)

    message_id = _normalise_id(str(message.get("Message-ID", "")))
    in_reply_to = _normalise_id(str(message.get("In-Reply-To", "")))
    refs = _references(str(message.get("References", "")))
    subject = _safe_text(message.get("Subject", ""))
    if refs:
        thread_key = refs[0]
    elif in_reply_to:
        thread_key = in_reply_to
    elif message_id:
        thread_key = message_id
    else:
        normal_subject = re.sub(r"^\s*((re|fw|fwd)\s*:\s*)+", "", subject, flags=re.I).strip().lower()
        thread_key = "subject:" + hashlib.sha256(normal_subject.encode()).hexdigest()

    headers = [(_safe_text(key), _safe_text(value)) for key, value in message.raw_items()]
    return ParsedMail(
        message_id=message_id,
        in_reply_to=in_reply_to,
        references_json=json.dumps(refs, ensure_ascii=False),
        thread_key=thread_key[:1000],
        subject=subject,
        sender=_addresses(message, "From"),
        recipients_to=_addresses(message, "To"),
        recipients_cc=_addresses(message, "Cc"),
        recipients_bcc=_addresses(message, "Bcc"),
        reply_to=_addresses(message, "Reply-To"),
        date_utc=_date(str(message.get("Date", ""))),
        headers_json=json.dumps(headers, ensure_ascii=False),
        text_body=text_body,
        html_body=html_body,
        mime_json=json.dumps(_part_structure(message), ensure_ascii=False),
        attachments=attachments,
        raw_sha256=raw_sha,
        raw_relpath=f"raw/{raw_name}",
    )
