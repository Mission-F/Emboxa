from __future__ import annotations

from datetime import datetime

from app.imap_adapter import StandardIMAPAdapter


class Address:
    def __init__(self, mailbox: bytes, host: bytes):
        self.mailbox = mailbox
        self.host = host


class Envelope:
    def __init__(self, subject, from_=(), date=None):
        self.subject = subject
        self.from_ = from_
        self.date = date


class EnvelopeOnlyClient:
    """A server that will not give up a body but still answers ENVELOPE.

    That is the useful case: the owner is deciding whether to empty a mailbox, and a count of
    losses tells them nothing about whether the losses matter.
    """

    def __init__(self, envelope=None, raises=False):
        self.envelope = envelope
        self.raises = raises

    def fetch(self, uids, fields):
        if self.raises:
            raise RuntimeError("[UNAVAILABLE] UID FETCH Server error")
        assert fields == ["ENVELOPE"]
        return {uids[0]: {b"ENVELOPE": self.envelope}}


def _adapter(client):
    adapter = StandardIMAPAdapter("imap.example.com", 993, "ssl", "user", "password")
    adapter.client = client
    return adapter


def test_summary_names_the_message():
    envelope = Envelope(
        subject=b"=?UTF-8?Q?Conferma_prenotazione?=",
        from_=(Address(b"noreply", b"banca.it"),),
        date=datetime(2013, 4, 17),
    )

    summary = _adapter(EnvelopeOnlyClient(envelope)).message_summary(1015)

    assert summary == '"Conferma prenotazione" da noreply@banca.it del 17/04/2013'


def test_summary_survives_a_broken_subject():
    """Yahoo serves malformed legacy header bytes; a log line must not be what crashes a backup."""
    envelope = Envelope(subject=b"\xff\xfe promo", date=datetime(2020, 1, 2))

    summary = _adapter(EnvelopeOnlyClient(envelope)).message_summary(7)

    assert "promo" in summary and "del 02/01/2020" in summary


def test_summary_is_empty_when_even_the_envelope_is_refused():
    assert _adapter(EnvelopeOnlyClient(raises=True)).message_summary(7) == ""
    assert _adapter(EnvelopeOnlyClient(envelope=None)).message_summary(7) == ""


def test_summary_without_a_connection():
    adapter = StandardIMAPAdapter("imap.example.com", 993, "ssl", "user", "password")
    assert adapter.message_summary(7) == ""
    assert adapter.is_alive() is False
