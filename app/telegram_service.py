from __future__ import annotations

import json
import logging
from urllib.request import Request, urlopen

from sqlalchemy import select

from .database import SessionLocal
from .models import Account, TelegramLink
from .settings_service import get_bool_setting, get_setting

log = logging.getLogger("emboxa.telegram")


def notify_backup(account_id: int, status: str) -> None:
    with SessionLocal() as db:
        account = db.get(Account, account_id)
        if not account:
            return
        link = db.scalar(select(TelegramLink).where(TelegramLink.user_id == account.owner_id))
        enabled = get_bool_setting("telegram_enabled", db=db)
        token = get_setting("telegram_bot_token", db=db)
        allowed = link and ((status == "completed" and link.notify_completed) or (status == "failed" and link.notify_failed))
        if not enabled or not token or not allowed:
            return
        text = f"EMBOXA\n\n{account.display_name}\nBackup {status}."
        method = "sendMessage"
        payload = {"chat_id": link.chat_id, "text": text,
                   "reply_markup": {"inline_keyboard": [[{"text": "Open dashboard", "callback_data": "dashboard"}]]}}
        try:
            request = Request(f"https://api.telegram.org/bot{token}/{method}", data=json.dumps(payload).encode(),
                              headers={"Content-Type": "application/json"}, method="POST")
            with urlopen(request, timeout=12) as response:
                result = json.loads(response.read()).get("result") or {}
        except Exception:
            log.warning("Telegram notification failed for account %s", account_id)


def notify_user(user_id: int, text: str, preference: str) -> bool:
    with SessionLocal() as db:
        link = db.scalar(select(TelegramLink).where(TelegramLink.user_id == user_id))
        enabled = get_bool_setting("telegram_enabled", db=db)
        token = get_setting("telegram_bot_token", db=db)
        if not enabled or not token or not link or not getattr(link, preference, False):
            return False
        method = "sendMessage"
        payload = {"chat_id": link.chat_id, "text": f"EMBOXA\n\n{text}",
                   "reply_markup": {"inline_keyboard": [[{"text": "Open dashboard", "callback_data": "dashboard"}]]}}
        try:
            request = Request(f"https://api.telegram.org/bot{token}/{method}", data=json.dumps(payload).encode(),
                              headers={"Content-Type": "application/json"}, method="POST")
            with urlopen(request, timeout=12) as response:
                result = json.loads(response.read()).get("result") or {}
            return True
        except Exception:
            log.warning("Telegram user notification failed for user %s", user_id)
            return False


def notify_transfer(user_id: int, destination: str, status: str, processed: int, skipped: int) -> bool:
    label = "completed" if status == "completed" else "failed"
    return notify_user(
        user_id,
        f"IMAP transfer {label}.\nDestination: {destination}\nProcessed: {processed}\nDuplicates skipped: {skipped}",
        "notify_completed" if status == "completed" else "notify_failed",
    )
