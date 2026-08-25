# EMBOXA architecture

The self-hosted reference remains in its separate application directory. The parser, IMAP adapter and design tokens can be synchronized into this Web distribution by `scripts/sync_shared.py`; the sync is verified with SHA-256. The Web i18n catalogue is maintained here because it also owns authentication, administration and public-service strings that do not exist in the appliance catalogue.

Archive parsing, safe email rendering, attachment viewers, export/import and backup semantics remain the reference implementation. Web-only authentication, quotas, retention, admin, public pages, consent and Telegram stay in the separate Web application and never connect to the self-hosted database or storage.

For a common UI change, update the canonical self-hosted asset and run the Web sync script. For Web-only behavior, change only the Web package. Each container vendors its sources at build time, so neither deployment requires runtime access to the other.
