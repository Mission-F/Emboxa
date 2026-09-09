from __future__ import annotations

from app.imap_adapter import StandardIMAPAdapter


class TruncatingClient:
    """A server that answers SEARCH ALL with a capped list and reports no error.

    Yahoo behaves this way at 10 000 UIDs, which made a 38 000-message mailbox archive as 10 000
    without a single warning anywhere.
    """

    def __init__(self, total: int, search_cap: int):
        self.uids = list(range(1000, 1000 + total))
        self.search_cap = search_cap
        self.use_uid = True
        self.fetch_calls: list[str] = []

    def search(self, _criteria):
        return self.uids[: self.search_cap]

    def fetch(self, sequence, fields):
        assert self.use_uid is False, "sequence-number enumeration must leave UID mode"
        assert fields == ["UID"]
        self.fetch_calls.append(sequence)
        start, end = (int(part) for part in sequence.split(":"))
        # Sequence numbers are 1-based positions into the folder.
        return {position: {b"UID": self.uids[position - 1]} for position in range(start, end + 1)}


def test_truncated_search_falls_back_to_sequence_numbers():
    client = TruncatingClient(total=38211, search_cap=10000)
    adapter = StandardIMAPAdapter("imap.mail.yahoo.com", 993, "ssl", "user", "password")
    adapter.client = client

    uids = adapter.message_uids(expected=38211)

    assert len(uids) == 38211
    assert uids == sorted(client.uids)
    assert client.use_uid is True, "UID mode has to be restored for the rest of the backup"
    assert len(client.fetch_calls) == 20, "enumeration should be chunked, not one huge FETCH"


def test_complete_search_is_left_alone():
    client = TruncatingClient(total=1200, search_cap=10000)
    adapter = StandardIMAPAdapter("imap.example.com", 993, "ssl", "user", "password")
    adapter.client = client

    uids = adapter.message_uids(expected=1200)

    assert len(uids) == 1200
    assert client.fetch_calls == [], "a server that answers fully must not be queried twice"


def test_missing_expected_count_keeps_previous_behaviour():
    client = TruncatingClient(total=38211, search_cap=10000)
    adapter = StandardIMAPAdapter("imap.example.com", 993, "ssl", "user", "password")
    adapter.client = client

    assert len(adapter.message_uids()) == 10000
    assert client.fetch_calls == []
