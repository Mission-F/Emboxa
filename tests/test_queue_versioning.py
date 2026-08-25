import json
import uuid

from sqlalchemy import select

from app.backup import _snapshot_comparison, backup_manager, recover_interrupted_jobs, rotate_versions, snapshot_root
from app.database import SessionLocal
from app.migrations import run_migrations
from app.models import Account, BackupJob, Snapshot, utcnow


def _account(db, name="Queue"):
    item = Account(archive_uuid=str(uuid.uuid4()), display_name=name,
                   email=f"{name.lower()}@example.com", imap_enabled=True,
                   encrypted_password="test", retention_versions=3)
    db.add(item); db.flush()
    return item


def test_fifo_persistence_duplicate_guard_cancel_and_recovery(monkeypatch):
    run_migrations()
    submitted = []
    monkeypatch.setattr(backup_manager, "_submit", submitted.append)
    with SessionLocal() as db:
        first = _account(db, "First"); second = _account(db, "Second"); db.commit()
        first_id, second_id = first.id, second.id
    job_a, created_a = backup_manager.start(first_id)
    duplicate, created_duplicate = backup_manager.start(first_id)
    job_b, created_b = backup_manager.start(second_id)
    assert created_a and created_b and duplicate == job_a and not created_duplicate
    assert submitted == [job_a, job_b]
    assert backup_manager.cancel(job_b)
    with SessionLocal() as db:
        assert db.get(BackupJob, job_b).status == "cancelled"
        running = db.get(BackupJob, job_a); running.status = "running"; running.started_at = utcnow(); db.commit()
    submitted.clear(); recover_interrupted_jobs()
    with SessionLocal() as db:
        recovered = db.get(BackupJob, job_a)
        assert recovered.status == "queued" and recovered.started_at is None
    assert submitted == [job_a]


def test_anomaly_protection_and_safe_version_rotation():
    previous = Snapshot(id=10, account_id=1, snapshot_uuid=str(uuid.uuid4()), status="completed",
                        message_count=100, archive_size=1000, attachment_count=20,
                        folder_counts_json=json.dumps({"INBOX": 80, "Sent": 20}))
    current = Snapshot(id=11, account_id=1, snapshot_uuid=str(uuid.uuid4()), status="completed",
                       message_count=70, archive_size=700, attachment_count=10,
                       folder_counts_json=json.dumps({"INBOX": 70}))
    comparison = _snapshot_comparison(previous, current)
    assert comparison["suspicious"] is True and comparison["messages_removed"] == 30
    assert comparison["folders_removed"] == ["Sent"]
    with SessionLocal() as db:
        account = _account(db, "Versions"); account.retention_versions = 2; db.flush(); rows = []
        for index in range(4):
            row = Snapshot(account_id=account.id, snapshot_uuid=str(uuid.uuid4()), status="completed",
                           completed_at=utcnow(), message_count=100 + index, protected=index == 0)
            db.add(row); db.flush(); rows.append(row)
            snapshot_root(account.archive_uuid, row.snapshot_uuid).mkdir(parents=True, exist_ok=True)
        account.active_snapshot_id = rows[-1].id
        account_id, protected_id, stale_id = account.id, rows[0].id, rows[1].id
        db.commit(); rotate_versions(db, account)
    with SessionLocal() as db:
        remaining = set(db.scalars(select(Snapshot.id).where(Snapshot.account_id == account_id)).all())
        assert protected_id in remaining and stale_id not in remaining and len(remaining) == 3
