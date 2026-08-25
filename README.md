# Emboxa Web — EMBOXA multi-user service

This folder is the isolated Web distribution for `https://emboxa.eu`. It has its own database, storage volume, container, port and secrets; it never opens the self-hosted installation. The only bridge between products is the validated `.mailvault` format.

## Deploy

```bash
cp .env.example .env
# Fill only bootstrap administrator and optional infrastructure secrets.
docker compose up -d --build
```

The default port is `49273`. Production must run behind an HTTPS reverse proxy. Set `PUBLIC_APP_URL=https://emboxa.eu`, `COOKIE_SECURE=true`, a long `APP_SECRET`, and either `ENCRYPTION_KEY` or a safely backed-up `/data/secrets/fernet.key`.

For TrueNAS, copy [`deploy/truenas-web.example.yaml`](deploy/truenas-web.example.yaml), replace `/mnt/POOL/apps/Emboxa-Web` with the host dataset path and keep `/data` separate from every other Emboxa installation. The host-specific `deploy/truenas-web.yaml` is deliberately ignored by Git.

## First administrator

Set `ADMIN_EMAIL` and `ADMIN_PASSWORD` for the first start. The account is created as verified `PLUS` and can access `/admin`. Remove `ADMIN_PASSWORD` from the runtime environment after successful creation. System Settings is the runtime source of truth for SMTP, Telegram, public identity, registration, Standard limits, backup, export, cleanup and Analytics. Environment values are bootstrap fallbacks only.

## SMTP

Email verification and password reset require SMTP to be configured and enabled in **Administration → System Settings → Email / SMTP**. Passwords are encrypted at rest, masked in responses and replaceable without editing YAML. Connection and test-email actions return sanitized messages.

## Plans and retention

Standard defaults:

- 15 GiB total per user;
- 5 mailboxes;
- 30-day expiration for normal backup versions;
- one permanent mailbox with a 31-day rotation lock.

Quota is enforced server-side before and during backup. A failed over-quota staging snapshot is removed and the previous valid backup remains active. Reducing quota below usage marks the account over quota without deleting data. Plus removes mailbox, storage, retention and permanent-mailbox limits.

The scheduler cleans expired versions, temporary exports, security tokens, stale staging directories and stuck jobs idempotently. Browser exports use authenticated UUID URLs and default to a 24-hour TTL.

## Telegram

Paste the BotFather token under `/admin` and save. Token validation and webhook delivery have separate status indicators: a valid bot remains connected even if the public webhook is temporarily unreachable, and **Retry webhook** can register it later. Each user then opens the bot, sends `/start`, and saves the returned Chat ID under **Preferences → Telegram**. The webhook is `/api/telegram/webhook` and requires Telegram’s generated secret header. Callbacks resolve `chat_id → user` before every mailbox or backup action.

## Public site, SEO and consent

Public localized routes live under `/it/` and `/en/`. `robots.txt`, localized sitemap, canonical/hreflang, Open Graph and public metadata use `PUBLIC_APP_URL`. `/app`, `/admin`, auth and API routes emit `noindex`.

Analytics loads only when enabled with a valid Measurement ID in Administration and the visitor accepts optional cookies. The allowlist excludes payload fields and mailbox content. Privacy, Cookies, Legal and Terms pages are linked in the public footer. Complete `LEGAL_ENTITY_NAME`, `LEGAL_ADDRESS` and `LEGAL_VAT_ID` after legal review; the application deliberately does not invent those values.

## Backup and restore

Back up together:

- `/data/db`;
- `/data/archives`;
- `/data/exports` when active downloads must survive;
- `/data/secrets`, especially `fernet.key`;
- the effective environment/reverse-proxy configuration outside Git.

Without the original encryption key, saved IMAP and service credentials cannot be recovered. Restore database, archives and key as one consistent set before starting the container.

## Shared maintenance

The stable parser, IMAP adapter, i18n catalogue and design tokens are vendored from the self-hosted reference:

```bash
python scripts/sync_shared.py
python scripts/sync_shared.py --check
```

The check proves a shared source change reaches both builds, while Web-only auth, quotas, public pages, admin and Telegram remain here. Containers vendor sources at build time and have no cross-installation runtime dependency.

## Verification

```bash
python -m compileall -q app tests
node --check app/static/app.js
pytest -q
python scripts/sync_shared.py --check
```

Run the release check before any publication. Follow [`docs/GITHUB_RELEASE.md`](docs/GITHUB_RELEASE.md) for the exact include/exclude list and first-push commands. The repository/owner is intentionally not invented; GHCR publication starts automatically after the real repository receives `main`.
