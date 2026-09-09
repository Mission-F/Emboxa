from __future__ import annotations

from datetime import datetime

from app.imap_adapter import BODY_MISSING_FLAG, BODY_MISSING_HEADER, StandardIMAPAdapter


class Address:
    def __init__(self, mailbox, host, name=None):
        self.mailbox, self.host, self.name = mailbox, host, name


class Envelope:
    def __init__(self, **fields):
        for attr in ("subject", "date", "from_", "sender", "reply_to", "to", "cc", "bcc",
                     "message_id", "in_reply_to"):
            setattr(self, attr, fields.get(attr))


class RefusingClient:
    """Refuses RFC822 the way Yahoo does, but still answers the header-only requests."""

    def __init__(self, header=None, envelope=None):
        self.header, self.envelope = header, envelope
        self.asked = []

    def fetch(self, uids, fields):
        self.asked.append(fields)
        if fields[0] == "RFC822":
            raise RuntimeError("[UNAVAILABLE] UID FETCH Server error")
        if fields[0] == "BODY.PEEK[HEADER]":
            if self.header is None:
                raise RuntimeError("[UNAVAILABLE] UID FETCH Server error")
            return {uids[0]: {b"BODY[HEADER]": self.header, b"FLAGS": (b"\\Seen",),
                              b"INTERNALDATE": datetime(2015, 1, 5)}}
        return {uids[0]: {b"ENVELOPE": self.envelope, b"FLAGS": (b"\\Seen",),
                          b"INTERNALDATE": datetime(2015, 1, 5)}}


def _adapter(client):
    adapter = StandardIMAPAdapter("imap.example.com", 993, "ssl", "user", "password")
    adapter.client = client
    return adapter


def test_the_original_headers_are_kept_when_the_body_is_refused():
    """The mailbox is the owner's: a message the provider broke is still theirs to keep."""
    header = (b"From: giorgio.picchi99@gmail.com\r\n"
              b"To: frencifagio@yahoo.com\r\n"
              b"Subject: Preventivo\r\n"
              b"Date: Fri, 13 Feb 2015 10:04:00 +0100\r\n")

    message = _adapter(RefusingClient(header=header)).fetch_headers_only(1198)

    assert message.body_missing is True
    assert b"giorgio.picchi99@gmail.com" in message.raw
    assert b"Subject: Preventivo" in message.raw
    assert BODY_MISSING_HEADER.encode() in message.raw, "never mistakable for the real message"
    assert BODY_MISSING_FLAG in message.flags
    assert "\\Seen" in message.flags, "the original flags are kept too"
    assert message.internal_date == datetime(2015, 1, 5)


def test_the_envelope_stands_in_when_even_the_header_is_refused():
    envelope = Envelope(
        subject=b"Fattura 2015",
        from_=(Address(b"noreply", b"banca.it", name=b"Banca"),),
        to=(Address(b"frencifagio", b"yahoo.com"),),
        date=datetime(2015, 3, 13),
        message_id=b"<abc@banca.it>",
    )
    client = RefusingClient(header=None, envelope=envelope)

    message = _adapter(client).fetch_headers_only(1288)

    assert b'From: "Banca" <noreply@banca.it>' in message.raw
    assert b"To: frencifagio@yahoo.com" in message.raw
    assert b"Subject: Fattura 2015" in message.raw
    assert b"Message-ID: <abc@banca.it>" in message.raw
    assert b"Date: Fri, 13 Mar 2015" in message.raw
    assert [fields[0] for fields in client.asked] == ["BODY.PEEK[HEADER]", "ENVELOPE"]


def test_nothing_is_invented_when_the_server_gives_nothing():
    client = RefusingClient(header=None, envelope=None)
    assert _adapter(client).fetch_headers_only(7) is None


def test_the_stub_parses_as_a_real_message(tmp_path):
    """It has to survive the same parser as everything else, or it will not reach the archive."""
    from app.mail_parser import parse_and_store

    header = b"From: a@b.c\r\nSubject: =?UTF-8?Q?Perch=C3=A9?=\r\nDate: Mon, 5 Jan 2015 09:00:00 +0100\r\n"
    message = _adapter(RefusingClient(header=header)).fetch_headers_only(1036)
    parsed = parse_and_store(message.raw, tmp_path)

    assert parsed.subject == "Perché"
    assert parsed.sender and "a@b.c" in parsed.sender
    assert parsed.date_utc is not None
