from __future__ import annotations

import json
import logging
from html import escape as html_escape
from urllib.request import Request, urlopen

from sqlalchemy import select

from .database import SessionLocal
from .models import Account, TelegramLink
from .settings_service import get_bool_setting, get_setting

log = logging.getLogger("emboxa.telegram")

STATUS_BADGE = {"completed": "✅ Completed", "failed": "❌ Failed"}


def _send(chat_id: str, token: str, text: str) -> bool:
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
               "reply_markup": {"inline_keyboard": [[{"text": "Open dashboard", "callback_data": "dashboard"}]]}}
    request = Request(f"https://api.telegram.org/bot{token}/sendMessage", data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=12) as response:
        json.loads(response.read())
    return True


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
        badge = STATUS_BADGE.get(status, status.title())
        text = f"📬 <b>EMBOXA</b>\n\n<b>{html_escape(account.display_name)}</b>\nBackup {badge}."
        try:
            _send(link.chat_id, token, text)
        except Exception:
            log.warning("Telegram notification failed for account %s", account_id)


def notify_user(user_id: int, text: str, preference: str) -> bool:
    with SessionLocal() as db:
        link = db.scalar(select(TelegramLink).where(TelegramLink.user_id == user_id))
        enabled = get_bool_setting("telegram_enabled", db=db)
        token = get_setting("telegram_bot_token", db=db)
        if not enabled or not token or not link or not getattr(link, preference, False):
            return False
        try:
            return _send(link.chat_id, token, f"📬 <b>EMBOXA</b>\n\n{text}")
        except Exception:
            log.warning("Telegram user notification failed for user %s", user_id)
            return False


def notify_transfer(user_id: int, destination: str, status: str, processed: int, skipped: int) -> bool:
    badge = STATUS_BADGE.get(status, status.title())
    text = (
        f"⇄ <b>IMAP Transfer</b> {badge}\n\n"
        f"<b>Destination</b>  {html_escape(destination)}\n"
        f"<b>Processed</b>  {processed:,}\n"
        f"<b>Duplicates skipped</b>  {skipped:,}"
    )
    return notify_user(user_id, text, "notify_completed" if status == "completed" else "notify_failed")
