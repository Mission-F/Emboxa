from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import ARCHIVES_DIR
from .models import Account, Snapshot


def directory_size(path: Path) -> int:
    total = 0
    if not path.is_dir():
        return 0
    try:
        for item in path.rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


def snapshot_path(account: Account, snapshot: Snapshot) -> Path:
    return ARCHIVES_DIR / account.archive_uuid / "snapshots" / snapshot.snapshot_uuid


def snapshot_disk_size(account: Account, snapshot: Snapshot) -> int:
    return directory_size(snapshot_path(account, snapshot))


def account_active_archive_size(db: Session, account: Account) -> int:
    if not account.active_snapshot_id:
        return 0
    snapshot = db.get(Snapshot, account.active_snapshot_id)
    if not snapshot or snapshot.status not in {"completed", "active"}:
        return 0
    return snapshot_disk_size(account, snapshot)


def user_storage_used(db: Session, user_id: int) -> int:
    rows = db.execute(
        select(Account, Snapshot)
        .join(Snapshot, Snapshot.account_id == Account.id)
        .where(Account.owner_id == user_id, Snapshot.status.in_(["completed", "active"]))
    ).all()
    return int(sum(snapshot_disk_size(account, snapshot) for account, snapshot in rows))


def total_archive_storage_used(db: Session) -> int:
    rows = db.execute(
        select(Account, Snapshot)
        .join(Snapshot, Snapshot.account_id == Account.id)
        .where(Snapshot.status.in_(["completed", "active"]))
    ).all()
    return int(sum(snapshot_disk_size(account, snapshot) for account, snapshot in rows))
