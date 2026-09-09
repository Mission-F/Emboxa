from __future__ import annotations

import app.backup as backup


class FlakyAdapter:
    """A server that refuses some fetches the way Yahoo does.

    `[UNAVAILABLE] UID FETCH Server error - Please try again later` reads like a load problem, but
    on a real 38 000-message mailbox it came from individual messages the server would never hand
    over: the same UIDs failed run after run, every attempt, and the connection stayed perfectly
    usable throughout. Set `drops_connection` for the other kind of failure, where the link dies.
    """

    def __init__(self, failing_uids=(), fail_times=None, drops_connection=False):
        self.failing_uids = set(failing_uids)
        self.fail_times = fail_times  # None = refuse forever
        self.drops_connection = drops_connection
        self.attempts = 0
        self.batch_sizes = []
        self.logged_out = 0
        self.connects = 0
        self.selected = []
        self.alive = True

    def fetch_messages(self, uids):
        self.attempts += 1
        self.batch_sizes.append(len(uids))
        offending = self.failing_uids.intersection(uids)
        if offending and (self.fail_times is None or self.attempts <= self.fail_times):
            if self.drops_connection:
                self.alive = False
            raise RuntimeError("[UNAVAILABLE] UID FETCH Server error - Please try again later")
        return [f"msg-{uid}" for uid in uids]

    def is_alive(self):
        return self.alive

    def message_summary(self, uid):
        return f"messaggio {uid}"

    def logout(self):
        self.logged_out += 1
        self.alive = False

    def reconnect(self):
        self.connects += 1
        self.alive = True
        return self

    def select_folder(self, name):
        self.selected.append(name)


def _no_waiting(monkeypatch, adapter):
    """Keep the backoff logic exercised but the test instant."""
    monkeypatch.setattr(backup.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(backup, "_connect_with_retry", lambda _account, _password: adapter.reconnect())


def test_a_refused_batch_is_split_rather_than_retried(monkeypatch):
    adapter = FlakyAdapter(failing_uids=[7])
    _no_waiting(monkeypatch, adapter)

    _result, messages, unreachable = backup._fetch_batch(adapter, None, "pw", "Inbox", [1, 2, 7, 8])

    assert unreachable == [7]
    assert sorted(messages) == ["msg-1", "msg-2", "msg-8"], "the healthy messages still arrive"
    # 4 -> 2+2 -> the bad half becomes 1+1. Retrying the four-message batch first would only have
    # repeated a refusal the server was never going to withdraw.
    assert adapter.batch_sizes[:2] == [4, 2], "it splits straight after the first refusal"


def test_a_live_connection_is_not_torn_down_after_a_refusal(monkeypatch):
    """The expensive half of the old behaviour: a login and a SELECT per refused fetch.

    Against a 38 000-message folder that was about twenty-five seconds each, and bisecting one bad
    message out of a batch of twenty pays it ten times: five minutes to skip a single email.
    """
    adapter = FlakyAdapter(failing_uids=[3])
    _no_waiting(monkeypatch, adapter)

    backup._fetch_batch(adapter, None, "pw", "Inbox", [1, 2, 3, 4])

    assert adapter.connects == 0, "the server refused a message, it did not hang up"
    assert adapter.logged_out == 0
    assert adapter.selected == [], "no reconnect means no folder to reselect"


def test_a_dropped_connection_is_rebuilt(monkeypatch):
    adapter = FlakyAdapter(failing_uids=[1], fail_times=1, drops_connection=True)
    _no_waiting(monkeypatch, adapter)

    _result, messages, unreachable = backup._fetch_batch(adapter, None, "pw", "Archive", [1])

    assert unreachable == []
    assert messages == ["msg-1"], "the message arrives once the link is back"
    assert adapter.connects == 1
    assert adapter.selected == ["Archive"], "the folder has to be reselected on the new connection"


def test_a_single_message_is_retried_patiently(monkeypatch):
    adapter = FlakyAdapter(failing_uids=[3], fail_times=2)
    _no_waiting(monkeypatch, adapter)

    _result, messages, unreachable = backup._fetch_batch(adapter, None, "pw", "Inbox", [3])

    assert unreachable == []
    assert messages == ["msg-3"], "a lone message gets the waits, since giving up loses it"
    assert adapter.attempts == 3


def test_healthy_batch_is_fetched_once(monkeypatch):
    adapter = FlakyAdapter()
    _no_waiting(monkeypatch, adapter)

    _result, messages, unreachable = backup._fetch_batch(adapter, None, "pw", "Inbox", [1, 2, 3])

    assert unreachable == []
    assert messages == ["msg-1", "msg-2", "msg-3"]
    assert adapter.attempts == 1, "a server behaving normally must not be retried or split"


def test_several_bad_messages_in_one_batch_are_all_isolated(monkeypatch):
    adapter = FlakyAdapter(failing_uids=[2, 6])
    _no_waiting(monkeypatch, adapter)

    _result, messages, unreachable = backup._fetch_batch(
        adapter, None, "pw", "Inbox", [1, 2, 3, 4, 5, 6, 7, 8])

    assert sorted(unreachable) == [2, 6]
    assert sorted(messages) == ["msg-1", "msg-3", "msg-4", "msg-5", "msg-7", "msg-8"]


def test_an_unreachable_message_is_named_in_the_log(monkeypatch, caplog):
    """Counting the losses is not enough: the owner is about to empty the mailbox they came from."""
    adapter = FlakyAdapter(failing_uids=[9])
    _no_waiting(monkeypatch, adapter)

    with caplog.at_level("ERROR"):
        backup._fetch_batch(adapter, None, "pw", "Inbox", [9])

    assert "messaggio 9" in caplog.text
