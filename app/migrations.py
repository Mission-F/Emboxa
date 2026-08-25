from __future__ import annotations

import logging

from sqlalchemy import inspect, text

from . import models as _models  # Registers every ORM table before create_all.
from .config import ensure_data_dirs
from .database import Base, engine

log = logging.getLogger("mailvault.migrations")


def run_migrations() -> None:
    """Small, versioned and idempotent migration runner for the appliance database."""
    ensure_data_dirs()
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        ))
        applied = {row[0] for row in conn.execute(text("SELECT version FROM schema_migrations"))}

    if 1 not in applied:
        log.info("Applying database migration 1 (base schema)")
        Base.metadata.create_all(engine)
        with engine.begin() as conn:
            conn.execute(text("INSERT OR IGNORE INTO schema_migrations(version) VALUES (1)"))
    else:
        # Keeps fresh tables safe if a future interrupted deployment created only part of the schema.
        Base.metadata.create_all(engine)

    if 2 not in applied:
        log.info("Applying database migration 2 (full-text index)")
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5("
                "message_id UNINDEXED, snapshot_id UNINDEXED, subject, sender, recipients, body, "
                "tokenize='unicode61 remove_diacritics 2')"
            ))
            conn.execute(text("INSERT OR IGNORE INTO schema_migrations(version) VALUES (2)"))

    if 3 not in applied:
        log.info("Applying database migration 3 (queue metrics and version retention)")
        additions = {
            "accounts": [("retention_versions", "INTEGER NOT NULL DEFAULT 3")],
            "snapshots": [
                ("attachment_count", "INTEGER NOT NULL DEFAULT 0"),
                ("protected", "BOOLEAN NOT NULL DEFAULT 0"),
                ("protection_reason", "TEXT"),
                ("comparison_json", "TEXT"),
                ("folder_counts_json", "TEXT NOT NULL DEFAULT '{}'")],
            "backup_jobs": [
                ("throughput", "FLOAT NOT NULL DEFAULT 0"),
                ("eta_seconds", "INTEGER"),
                ("updated_at", "DATETIME")],
        }
        with engine.begin() as conn:
            inspector = inspect(conn)
            for table, columns in additions.items():
                present = {column["name"] for column in inspector.get_columns(table)}
                for name, definition in columns:
                    if name not in present:
                        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}"))
            conn.execute(text("UPDATE accounts SET retention_versions=3 WHERE retention_versions IS NULL"))
            conn.execute(text("UPDATE snapshots SET status='completed' WHERE status IN ('active','retired')"))
            conn.execute(text(
                "UPDATE backup_jobs SET updated_at=COALESCE(finished_at,started_at,created_at,CURRENT_TIMESTAMP) "
                "WHERE updated_at IS NULL"))
            conn.execute(text(
                "UPDATE backup_jobs SET status='queued', cancel_requested=0 WHERE status='running'"))
            conn.execute(text(
                "UPDATE backup_jobs SET status='cancelled', finished_at=COALESCE(finished_at,CURRENT_TIMESTAMP), "
                "cancel_requested=1 WHERE status='cancelling'"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_snapshots_protected ON snapshots(protected)"))
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_single_running_backup "
                "ON backup_jobs((1)) WHERE status='running'"))
            conn.execute(text("INSERT OR IGNORE INTO schema_migrations(version) VALUES (3)"))

    if 4 not in applied:
        log.info("Applying database migration 4 (local archive trash and audit)")
        with engine.begin() as conn:
            present = {column["name"] for column in inspect(conn).get_columns("messages")}
            if "is_deleted" not in present:
                conn.execute(text("ALTER TABLE messages ADD COLUMN is_deleted BOOLEAN NOT NULL DEFAULT 0"))
            if "deleted_at" not in present:
                conn.execute(text("ALTER TABLE messages ADD COLUMN deleted_at DATETIME"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_messages_is_deleted ON messages(is_deleted)"))
        Base.metadata.create_all(engine)
        with engine.begin() as conn:
            conn.execute(text("INSERT OR IGNORE INTO schema_migrations(version) VALUES (4)"))

    if 5 not in applied:
        log.info("Applying database migration 5 (welcome and locale preferences)")
        with engine.begin() as conn:
            present = {column["name"] for column in inspect(conn).get_columns("users")}
            if "locale" not in present:
                conn.execute(text("ALTER TABLE users ADD COLUMN locale VARCHAR(10) NOT NULL DEFAULT 'auto'"))
            if "tutorial_completed" not in present:
                conn.execute(text("ALTER TABLE users ADD COLUMN tutorial_completed BOOLEAN NOT NULL DEFAULT 0"))
            conn.execute(text("INSERT OR IGNORE INTO schema_migrations(version) VALUES (5)"))

    if 6 not in applied:
        log.info("Applying database migration 6 (Web multi-tenant platform)")
        additions = {
            "users": [("email", "VARCHAR(320)"), ("verified_at", "DATETIME"), ("role", "VARCHAR(20) NOT NULL DEFAULT 'user'"),
                      ("plan", "VARCHAR(20) NOT NULL DEFAULT 'STANDARD'"),
                      ("storage_limit_bytes", "BIGINT NOT NULL DEFAULT 16106127360"),
                      ("status", "VARCHAR(20) NOT NULL DEFAULT 'active'"), ("last_login_at", "DATETIME")],
            "accounts": [("owner_id", "INTEGER"), ("mailbox_identity", "VARCHAR(64)"),
                         ("is_permanent", "BOOLEAN NOT NULL DEFAULT 0"), ("permanent_since", "DATETIME"),
                         ("permanent_locked_until", "DATETIME")],
            "snapshots": [("expires_at", "DATETIME")],
        }
        with engine.begin() as conn:
            inspector = inspect(conn)
            for table, columns in additions.items():
                present = {column["name"] for column in inspector.get_columns(table)}
                for name, definition in columns:
                    if name not in present:
                        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}"))
            conn.execute(text("UPDATE users SET email=username WHERE email IS NULL"))
            conn.execute(text("INSERT OR IGNORE INTO schema_migrations(version) VALUES (6)"))
        Base.metadata.create_all(engine)

    if 7 not in applied:
        log.info("Applying database migration 7 (runtime-configurable backup concurrency)")
        with engine.begin() as conn:
            conn.execute(text("DROP INDEX IF EXISTS uq_single_running_backup"))
            conn.execute(text("INSERT OR IGNORE INTO schema_migrations(version) VALUES (7)"))

    if 8 not in applied:
        log.info("Applying database migration 8 (persistent IMAP transfer queue)")
        Base.metadata.create_all(engine)
        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE imap_transfer_jobs SET status='queued', cancel_requested=0 "
                "WHERE status='running'"
            ))
            conn.execute(text(
                "UPDATE imap_transfer_jobs SET status='cancelled', finished_at=COALESCE(finished_at,CURRENT_TIMESTAMP), "
                "encrypted_password=NULL WHERE status='cancelling'"
            ))
            conn.execute(text("INSERT OR IGNORE INTO schema_migrations(version) VALUES (8)"))

    if 9 not in applied:
        log.info("Applying database migration 9 (permanent web exports)")
        Base.metadata.create_all(engine)
        with engine.begin() as conn:
            inspector = inspect(conn)
            if "web_exports" in inspector.get_table_names():
                columns = {column["name"]: column for column in inspector.get_columns("web_exports")}
                expires_column = columns.get("expires_at")
                if expires_column and not expires_column.get("nullable", True) and engine.dialect.name == "sqlite":
                    conn.execute(text("ALTER TABLE web_exports RENAME TO web_exports_old_m9"))
                    conn.execute(text(
                        "CREATE TABLE web_exports ("
                        "id INTEGER NOT NULL, public_id VARCHAR(36) NOT NULL, owner_id INTEGER NOT NULL, "
                        "account_id INTEGER NOT NULL, filename VARCHAR(500) NOT NULL, relpath VARCHAR(500) NOT NULL, "
                        "size BIGINT NOT NULL, expires_at DATETIME, created_at DATETIME NOT NULL, "
                        "PRIMARY KEY (id), UNIQUE (public_id), "
                        "FOREIGN KEY(owner_id) REFERENCES users (id) ON DELETE CASCADE, "
                        "FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE)"
                    ))
                    conn.execute(text(
                        "INSERT INTO web_exports "
                        "(id, public_id, owner_id, account_id, filename, relpath, size, expires_at, created_at) "
                        "SELECT id, public_id, owner_id, account_id, filename, relpath, size, expires_at, created_at "
                        "FROM web_exports_old_m9"
                    ))
                    conn.execute(text("DROP TABLE web_exports_old_m9"))
                    conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_web_exports_public_id ON web_exports(public_id)"))
                    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_web_exports_owner_id ON web_exports(owner_id)"))
                    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_web_exports_account_id ON web_exports(account_id)"))
                    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_web_exports_expires_at ON web_exports(expires_at)"))
            conn.execute(text("INSERT OR IGNORE INTO schema_migrations(version) VALUES (9)"))

    if 10 not in applied:
        log.info("Applying database migration 10 (passkey credentials)")
        Base.metadata.create_all(engine)
        with engine.begin() as conn:
            conn.execute(text("INSERT OR IGNORE INTO schema_migrations(version) VALUES (10)"))

    # Fail clearly if the Python SQLite build unexpectedly lacks FTS5.
    with engine.connect() as conn:
        if "message_fts" not in inspect(conn).get_table_names():
            raise RuntimeError("SQLite FTS5 is required but unavailable")
