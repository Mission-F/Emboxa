from __future__ import annotations

import app.backup as backup


class FlakyAdapter:
    """A server that refuses some fetches the way Yahoo does under load.

    `[UNAVAILABLE] UID FETCH Server error - Please try again later` used to end an entire backup —
    hours of downloading discarded because one batch of twenty messages was refused.
    """

    def __init__(self, failing_uids=(), fail_times=None):
        self.failing_uids = set(failing_uids)
        self.fail_times = fail_times  # None = fail forever
        self.attempts = 0
        self.logged_out = 0
        self.selected = []

    def fetch_messages(self, uids):
        self.attempts += 1
        offending = self.failing_uids.intersection(uids)
        if offending and (self.fail_times is None or self.attempts <= self.fail_times):
            raise RuntimeError("[UNAVAILABLE] UID FETCH Server error - Please try again later")
        return [f"msg-{uid}" for uid in uids]

    def logout(self):
        self.logged_out += 1

    def select_folder(self, name):
        self.selected.append(name)


def _no_waiting(monkeypatch, adapter):
    """Keep the backoff logic exercised but the test instant."""
    monkeypatch.setattr(backup.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(backup, "_connect_with_retry", lambda _account, _password: adapter)


def test_transient_failure_is_retried_and_succeeds(monkeypatch):
    adapter = FlakyAdapter(failing_uids=[5], fail_times=2)
    _no_waiting(monkeypatch, adapter)

    result, messages, unreachable = backup._fetch_batch(adapter, None, "pw", "Inbox", [1, 5, 9])

    assert unreachable == []
    assert messages == ["msg-1", "msg-5", "msg-9"]
    assert adapter.attempts == 3, "the first two attempts fail, the third goes through"
    assert result is adapter


def test_one_bad_message_does_not_lose_the_rest_of_the_batch(monkeypatch):
    adapter = FlakyAdapter(failing_uids=[7])       # this one never comes back
    _no_waiting(monkeypatch, adapter)

    _result, messages, unreachable = backup._fetch_batch(adapter, None, "pw", "Inbox", [1, 2, 7, 8])

    assert unreachable == [7]
    assert sorted(messages) == ["msg-1", "msg-2", "msg-8"], "the healthy messages still arrive"


def test_reconnects_between_attempts(monkeypatch):
    adapter = FlakyAdapter(failing_uids=[1], fail_times=1)
    _no_waiting(monkeypatch, adapter)

    backup._fetch_batch(adapter, None, "pw", "Archive", [1])

    assert adapter.logged_out >= 1
    assert adapter.selected == ["Archive"], "the folder has to be reselected on the new connection"


def test_healthy_batch_is_fetched_once(monkeypatch):
    adapter = FlakyAdapter()
    _no_waiting(monkeypatch, adapter)

    _result, messages, unreachable = backup._fetch_batch(adapter, None, "pw", "Inbox", [1, 2, 3])

    assert unreachable == []
    assert messages == ["msg-1", "msg-2", "msg-3"]
    assert adapter.attempts == 1, "a server behaving normally must not be retried or split"
