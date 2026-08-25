# EMBOXA architecture

The self-hosted application remains in this repository root. `app/` owns the local authentication adapter and appliance deployment. Shared browser language resources and design tokens are source assets designed to be synchronized into the Web distribution by `Emboxa Web/scripts/sync_shared.py`; the sync is verified by tests using SHA-256.

Archive parsing, safe email rendering, attachment viewers, export/import and backup semantics remain the reference implementation. Web-only authentication, quotas, retention, admin, public pages, consent and Telegram stay in the separate Web application and never connect to the self-hosted database or storage.

For a common UI change, update the canonical self-hosted asset and run the Web sync script. For Web-only behavior, change only the Web package. Each container vendors its sources at build time, so neither deployment requires runtime access to the other.
