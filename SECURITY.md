# Security

Report vulnerabilities privately to `info@missionf.it`. Do not open public issues containing credentials, mailbox content, tokens or exploit details.

EMBOXA stores IMAP credentials encrypted at rest. Keep the persistent encryption key, session secret and data volume outside Git, restrict their filesystem permissions, and use HTTPS through a trusted reverse proxy when exposed beyond a private network.
