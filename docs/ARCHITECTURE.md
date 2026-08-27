# EMBOXA architecture

EMBOXA is a single FastAPI application, meant to run as one container next to your data — typically on a home NAS or a small server, not behind a multi-tenant SaaS deployment.

```text
app/
├── main.py             FastAPI routes: auth, accounts, backup, archive, IMAP Transfer, admin, public pages
├── models.py            SQLAlchemy schema (users, accounts, snapshots, messages, attachments, jobs)
├── backup.py             Backup worker: connects a mailbox, stages a snapshot, commits it once complete
├── imap_adapter.py       Provider-neutral IMAP client (list/select/fetch/append)
├── graph_adapter.py       Microsoft Graph OAuth + mail API client
├── restore_providers.py  Restore destinations (Microsoft Graph, Gmail, generic IMAP) behind one interface
├── imap_transfer.py       IMAP Transfer worker: restores an archived snapshot into a destination mailbox
├── mbox_import.py          Offline MBOX import (no live mailbox required)
├── archive.py, mail_parser.py   Message parsing, storage layout, search
├── scheduler.py            Background scheduling for recurring backups
├── static/, templates/     Web app UI (vanilla JS + Jinja2, no build step)
```

## Data flow

```text
IMAP mailbox → queued backup job → versioned local snapshot → search / read / export / IMAP Transfer
```

A backup first writes a staging snapshot; only a completed, validated run is promoted to the active version, so an interrupted backup never replaces a previously good archive. IMAP/OAuth credentials are encrypted with a key stored under `/data/secrets`, generated on first start and never leaves the volume.

## Restore providers

Restoring an archived snapshot into another mailbox goes through one interface (`RestoreTarget` in `app/restore_providers.py`) with three implementations: Microsoft Graph (OAuth mailboxes, no IMAP), Gmail and generic IMAP (both via IMAP APPEND). The UI never asks which technology to use — it is picked automatically from the destination account.

## Background jobs

Backups, IMAP Transfer restores and archive imports are persisted rows (`BackupJob`, `IMAPTransferJob`, in-process import jobs), processed by small thread-pool managers, and survive a page reload or a closed browser tab — progress is polled from the database, not held in the browser.

## Everything lives in one place

There is no separate backend for the public marketing site: `public-site/` is a static build (Cloudflare Pages) generated from `public-site/build.py`, independent of the FastAPI app. The application itself has no external runtime dependency beyond the SQLite database and the local filesystem under `/data`.
