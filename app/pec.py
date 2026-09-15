from __future__ import annotations

import json
import logging
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, getaddresses, make_msgid
from typing import Callable

from sqlalchemy import func, select

from .backup import _connect_with_retry, _fetch_batch, snapshot_root, store_remote_message
from .config import IMAP_FETCH_BATCH, IMAP_TIMEOUT_SECONDS
from .database import SessionLocal
from .models import Account, Folder, Message, Snapshot
from .security import decrypt_secret

log = logging.getLogger("emboxa.pec")

# The three receipt types a PEC sender may ask for with X-TipoRicevuta. The provider reads the
# header at submission and shapes the delivery receipt accordingly; anything else is ignored by
# the provider, so it is refused here rather than silently becoming "completa".
RECEIPT_TYPES = ("completa", "breve", "sintetica")

# Where a mail client would have filed a sent message, for servers that do not flag the folder.
SENT_NAMES = {"sent", "sent items", "sent messages", "posta inviata", "inviata", "inviati",
              "elementi inviati", "inbox.sent"}

_ADDRESS = re.compile(r"[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+")


class PecError(RuntimeError):
    """Something the person sending has to fix: an address, a missing setting, a receipt type."""


def smtp_connect(host: str, port: int, security: str, username: str | None, password: str | None):
    context = ssl.create_default_context()
    if security == "ssl":
        client = smtplib.SMTP_SSL(host, port, timeout=IMAP_TIMEOUT_SECONDS, context=context)
    else:
        client = smtplib.SMTP(host, port, timeout=IMAP_TIMEOUT_SECONDS)
        client.ehlo()
        if security == "starttls":
            client.starttls(context=context)
            client.ehlo()
    try:
        if username:
            client.login(username, password or "")
        return client
    except Exception:
        try:
            client.close()
        except Exception:
            pass
        raise


def test_smtp(host: str, port: int, security: str, username: str | None, password: str | None) -> None:
    client = smtp_connect(host, port, security, username, password)
    try:
        client.noop()
    finally:
        try:
            client.quit()
        except Exception:
            client.close()


def parse_recipients(value: str) -> list[str]:
    addresses = [address.strip() for _name, address in getaddresses([(value or "").replace(";", ",")])
                 if address.strip()]
    invalid = [address for address in addresses if not _ADDRESS.fullmatch(address)]
    if invalid:
        raise PecError("Indirizzi non validi: " + ", ".join(invalid[:5]))
    return list(dict.fromkeys(addresses))


def build_pec(sender_email: str, sender_name: str | None, to: list[str], cc: list[str], subject: str,
              body: str, attachments: list[tuple[str, str, bytes]], receipt_type: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = formataddr((sender_name, sender_email)) if sender_name else sender_email
    message["To"] = ", ".join(to)
    if cc:
        message["Cc"] = ", ".join(cc)
    # A newline in a header is how one header becomes two; a subject never needs one.
    message["Subject"] = re.sub(r"[\r\n]+", " ", subject or "").strip()
    message["Date"] = formatdate(localtime=True)
    domain = sender_email.rsplit("@", 1)[-1] if "@" in sender_email else None
    message["Message-ID"] = make_msgid(domain=domain)
    message["X-TipoRicevuta"] = receipt_type
    message.set_content(body or "")
    for filename, content_type, data in attachments:
        maintype, _, subtype = (content_type or "application/octet-stream").partition("/")
        message.add_attachment(data, maintype=maintype or "application",
                               subtype=subtype or "octet-stream", filename=filename)
    return message


def find_sent_folder(adapter) -> str | None:
    folders = adapter.list_folders()
    for folder in folders:
        if any(flag.lower() == "\\sent" for flag in folder.flags):
            return folder.name
    for folder in folders:
        leaf = folder.name.split(folder.delimiter)[-1] if folder.delimiter else folder.name
        if leaf.strip().lower() in SENT_NAMES or folder.name.strip().lower() in SENT_NAMES:
            return folder.name
    return None


def send_pec(account_id: int, to: str, cc: str, subject: str, body: str,
             attachments: list[tuple[str, str, bytes]], receipt_type: str) -> dict:
    """Send a certified message through the mailbox's own PEC provider and file it under Sent.

    Once the provider's SMTP server has accepted the message it is sent, legally and for good —
    so nothing after that point is allowed to report the send as failed. Filing the copy in the
    Sent folder is a courtesy the provider does not do for SMTP submissions; if it fails the
    result says so, and the receipts arriving in the inbox remain the proof that matters.
    """
    if receipt_type not in RECEIPT_TYPES:
        raise PecError("Tipo di ricevuta non valido: scegli completa, breve o sintetica.")
    with SessionLocal() as db:
        account = db.get(Account, account_id)
        if not account or not account.is_pec:
            raise PecError("Questa casella non è configurata come PEC.")
        if not (account.smtp_host and account.smtp_port and account.encrypted_smtp_password):
            raise PecError("Mancano le impostazioni di invio: completale nella modifica della casella.")
        smtp = (account.smtp_host, account.smtp_port, account.smtp_security,
                account.smtp_username or account.imap_username or account.email,
                decrypt_secret(account.encrypted_smtp_password))
        imap_password = decrypt_secret(account.encrypted_password) if account.encrypted_password else None
        sender_email, sender_name = account.email, account.display_name

    recipients_to = parse_recipients(to)
    recipients_cc = parse_recipients(cc) if cc else []
    if not recipients_to:
        raise PecError("Indica almeno un destinatario.")

    message = build_pec(sender_email, sender_name, recipients_to, recipients_cc, subject, body,
                        attachments, receipt_type)
    client = smtp_connect(*smtp)
    try:
        refused = client.send_message(message, from_addr=sender_email,
                                      to_addrs=recipients_to + recipients_cc) or {}
    finally:
        try:
            client.quit()
        except Exception:
            client.close()
    log.info("PEC inviata dall'account %s a %s destinatari, ricevuta %s", account_id,
             len(recipients_to) + len(recipients_cc), receipt_type)

    saved, sent_folder = False, None
    if imap_password:
        try:
            adapter = _connect_with_retry(account, imap_password)
            try:
                sent_folder = find_sent_folder(adapter)
                if sent_folder:
                    adapter.select_write_folder(sent_folder)
                    if not adapter.has_message_id(message["Message-ID"]):
                        adapter.append_message(sent_folder, message.as_bytes(), ["\\Seen"], None)
                    saved = True
            finally:
                adapter.logout()
        except Exception:
            log.warning("PEC inviata ma non archiviata in Inviata per l'account %s", account_id,
                        exc_info=True)
    return {"message_id": message["Message-ID"], "refused": sorted(refused),
            "saved_to_sent": saved, "sent_folder": sent_folder}


def sync_new_messages(account_id: int, progress: Callable[[int, str], None] | None = None) -> dict:
    """Bring the archive up to date with the mailbox, downloading only what it does not hold.

    A full backup makes a new snapshot every time, which is right for a backup and absurd for
    pressing "refresh" in a mail client. This adds the new messages to the active snapshot
    instead, matched by UID per folder. A folder whose UIDVALIDITY has changed cannot be compared
    that way — its UIDs mean something else now — so it is left alone and reported, and a full
    backup is the way to read it again.
    """
    def report(percent: int, detail: str) -> None:
        if progress:
            progress(percent, detail)

    db = SessionLocal()
    adapter = None
    try:
        account = db.get(Account, account_id)
        if not account or not account.is_pec or not account.imap_enabled:
            raise PecError("Questa casella non è configurata come PEC.")
        snapshot = db.get(Snapshot, account.active_snapshot_id) if account.active_snapshot_id else None
        if not snapshot:
            raise PecError("Fai prima un backup completo: l'aggiornamento aggiunge a quello i messaggi nuovi.")
        password = decrypt_secret(account.encrypted_password)
        root = snapshot_root(account.archive_uuid, snapshot.snapshot_uuid)

        report(3, "Connessione alla casella.")
        adapter = _connect_with_retry(account, password)
        remote_folders = [folder for folder in adapter.list_folders(account.root_folder)
                          if "\\Noselect" not in folder.flags]
        added, skipped_folders = 0, []
        for index, remote in enumerate(remote_folders):
            report(5 + int(index / max(1, len(remote_folders)) * 90), f"Controllo di «{remote.name}».")
            uidvalidity, exists = adapter.select_folder(remote.name)
            folder = db.scalar(select(Folder).where(Folder.snapshot_id == snapshot.id,
                                                    Folder.name == remote.name))
            if folder is None:
                folder = Folder(snapshot_id=snapshot.id, name=remote.name, delimiter=remote.delimiter,
                                flags_json=json.dumps(remote.flags, ensure_ascii=False),
                                uidvalidity=uidvalidity, message_count=0, remote_count=exists)
                db.add(folder)
                db.flush()
            elif folder.uidvalidity and uidvalidity and str(folder.uidvalidity) != str(uidvalidity):
                log.warning("UIDVALIDITY cambiata per %s (account %s): cartella saltata",
                            remote.name, account_id)
                skipped_folders.append(remote.name)
                continue

            known = set(db.scalars(select(Message.imap_uid).where(Message.folder_id == folder.id)).all())
            fresh = [uid for uid in adapter.message_uids(expected=exists) if str(uid) not in known]
            for offset in range(0, len(fresh), IMAP_FETCH_BATCH):
                batch = fresh[offset:offset + IMAP_FETCH_BATCH]
                adapter, messages, _unreachable = _fetch_batch(adapter, account, password, remote.name, batch)
                for remote_message in messages:
                    attachments, written = store_remote_message(db, snapshot.id, folder.id, root, remote_message)
                    snapshot.attachment_count = (snapshot.attachment_count or 0) + attachments
                    snapshot.archive_size = (snapshot.archive_size or 0) + written
                    added += 1
                db.commit()

            folder.message_count = db.scalar(select(func.count(Message.id)).where(
                Message.folder_id == folder.id, Message.is_deleted.is_(False))) or 0
            folder.remote_count = exists
            folder.uidvalidity = folder.uidvalidity or uidvalidity
            db.commit()

        snapshot.message_count = db.scalar(select(func.count(Message.id)).where(
            Message.snapshot_id == snapshot.id, Message.is_deleted.is_(False))) or 0
        snapshot.folder_counts_json = json.dumps({
            folder.name: folder.message_count
            for folder in db.scalars(select(Folder).where(Folder.snapshot_id == snapshot.id))
        }, ensure_ascii=False)
        account.message_count = snapshot.message_count
        db.commit()
        report(100, f"{added} nuovi messaggi." if added else "Nessun nuovo messaggio.")
        return {"added": added, "skipped_folders": skipped_folders}
    finally:
        if adapter is not None:
            try:
                adapter.logout()
            except Exception:
                pass
        db.close()
