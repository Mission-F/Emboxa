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
        method = "editMessageText" if link.dashboard_message_id else "sendMessage"
        payload = {"chat_id": link.chat_id, "text": text,
                   "reply_markup": {"inline_keyboard": [[{"text": "Open dashboard", "callback_data": "dashboard"}]]}}
        if link.dashboard_message_id:
            payload["message_id"] = int(link.dashboard_message_id)
        try:
            request = Request(f"https://api.telegram.org/bot{token}/{method}", data=json.dumps(payload).encode(),
                              headers={"Content-Type": "application/json"}, method="POST")
            with urlopen(request, timeout=12) as response:
                result = json.loads(response.read()).get("result") or {}
            if not link.dashboard_message_id and result.get("message_id"):
                link.dashboard_message_id = str(result["message_id"]); db.commit()
        except Exception:
            log.warning("Telegram notification failed for account %s", account_id)


def notify_user(user_id: int, text: str, preference: str) -> bool:
    with SessionLocal() as db:
        link = db.scalar(select(TelegramLink).where(TelegramLink.user_id == user_id))
        enabled = get_bool_setting("telegram_enabled", db=db)
        token = get_setting("telegram_bot_token", db=db)
        if not enabled or not token or not link or not getattr(link, preference, False):
            return False
        method = "editMessageText" if link.dashboard_message_id else "sendMessage"
        payload = {"chat_id": link.chat_id, "text": f"EMBOXA\n\n{text}",
                   "reply_markup": {"inline_keyboard": [[{"text": "Open dashboard", "callback_data": "dashboard"}]]}}
        if link.dashboard_message_id:
            payload["message_id"] = int(link.dashboard_message_id)
        try:
            request = Request(f"https://api.telegram.org/bot{token}/{method}", data=json.dumps(payload).encode(),
                              headers={"Content-Type": "application/json"}, method="POST")
            with urlopen(request, timeout=12) as response:
                result = json.loads(response.read()).get("result") or {}
            if not link.dashboard_message_id and result.get("message_id"):
                link.dashboard_message_id = str(result["message_id"]); db.commit()
            return True
        except Exception:
            log.warning("Telegram user notification failed for user %s", user_id)
            return False
