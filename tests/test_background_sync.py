from __future__ import annotations

from pathlib import Path

from server.skimmer_server.config import Config
from server.skimmer_server.state import StateStore
from server.skimmer_server.sync import BackgroundSync, _read_state_row


class FakeMiniflux:
    def __init__(self) -> None:
        self.read_calls = 0
        self.unread_calls = 0
        self.marked: list[int] = []
        self.changed_after_values: list[str | None] = []

    def read_entries(self, limit: int = 200, changed_after: str | None = None) -> list[dict]:
        self.read_calls += 1
        self.changed_after_values.append(changed_after)
        return []

    def unread_changes(self, limit: int = 200, changed_after: str | None = None) -> list[dict]:
        return []

    def unread_entries(self, limit: int) -> list[dict]:
        self.unread_calls += 1
        return [{
            "id": 1, "title": "Example", "url": "https://example.com", "status": "unread",
            "published_at": "2026-08-21T00:00:00Z", "content": "<p>Example</p>",
            "feed": {"id": 1, "title": "Feed"}, "category": {"id": 1, "title": "News"},
        }]

    def mark_entries_read(self, entry_ids: list[int]) -> None:
        self.marked.extend(entry_ids)

    def mark_entries_unread(self, entry_ids: list[int]) -> None:
        self.marked.extend(entry_ids)


def test_sync_reconciles_then_classifies_once_then_batches(tmp_path: Path) -> None:
    config = Config(
        miniflux_url="https://example.invalid", miniflux_username=None, miniflux_password=None,
        miniflux_token=None, fetch_limit=10, data_dir=tmp_path,
        overrides_path=tmp_path / "overrides", history_path=tmp_path / "history",
        write_back=True, host="127.0.0.1", port=0, must_read_keywords=[],
        possible_interest_keywords=[], ignore_keywords=[],
    )
    store = StateStore(config.decisions_path, config.history_path)
    sync = BackgroundSync(config, store)
    client = FakeMiniflux()
    sync.request_status_change(1, "read")

    # Invoke the ordered phases directly to avoid constructing a real Miniflux client.
    known = {int(row["entry_id"]): row for row in store.rows()}
    reconciled = sync._reconcile(client, known)
    new_rows = sync._fetch_and_classify(client, known)
    store.append(new_rows)
    write_back = sync._flush_pending(client)

    assert client.read_calls == 1
    assert client.unread_calls == 1
    assert len(new_rows) == 1
    assert write_back == 1
    assert client.marked == [1]
    assert store.entry(1)["status"] == "unread"


def test_reconcile_marks_locally_unread_entries_read(tmp_path: Path) -> None:
    config = Config(
        miniflux_url="https://example.invalid", miniflux_username=None, miniflux_password=None,
        miniflux_token=None, fetch_limit=10, data_dir=tmp_path,
        overrides_path=tmp_path / "overrides", history_path=tmp_path / "history",
        write_back=False, host="127.0.00.1", port=0, must_read_keywords=[],
        possible_interest_keywords=[], ignore_keywords=[],
    )
    store = StateStore(config.decisions_path, config.history_path)
    sync = BackgroundSync(config, store)
    entry = {
        "id": 2, "title": "Already read", "url": "https://example.com/read", "status": "read",
        "published_at": "2026-08-21T00:00:00Z", "content": "<p>Example</p>",
        "feed": {"id": 1, "title": "Feed"}, "category": {"id": 1, "title": "News"},
    }

    class Client:
        def read_entries(self, limit: int = 500, changed_after: str | None = None):
            return [entry]

        def unread_changes(self, limit: int = 500, changed_after: str | None = None):
            return []

    sync._reconcile(Client(), {}, changed_after="2026-08-20T00:00:00+00:00")

    assert store.entry(2)["status"] == "read"


def test_reconcile_read_sets_done_never_manual(tmp_path: Path) -> None:
    config = Config(
        miniflux_url="https://example.invalid", miniflux_username=None, miniflux_password=None,
        miniflux_token=None, fetch_limit=10, data_dir=tmp_path,
        overrides_path=tmp_path / "overrides", history_path=tmp_path / "history",
        write_back=False, host="127.0.0.1", port=0, must_read_keywords=[],
        possible_interest_keywords=[], ignore_keywords=[],
    )
    store = StateStore(config.decisions_path, config.history_path)
    sync = BackgroundSync(config, store)
    now = "2026-08-22T00:00:00+00:00"

    class Client:
        def read_entries(self, limit: int = 500, changed_after: str | None = None):
            return [{
                "id": 3, "title": "Read in Miniflux after skimmer interaction",
                "url": "https://example.com/3", "status": "read",
                "published_at": now, "content": "<p>Example</p>",
                "feed": {"id": 1, "title": "Feed"}, "category": {"id": 1, "title": "News"},
            }]

        def unread_changes(self, limit: int = 500, changed_after: str | None = None):
            return []

    # Entry was manually overridden in skimmer, then read in Miniflux.
    manual_row = {"entry_id": 3, "classification": "must_read", "confidence": 1.0,
                  "manual": True, "status": "unread", "observed_at": now}
    store.append([manual_row])

    sync._reconcile(Client(), {int(row["entry_id"]): row for row in store.rows()}, changed_after=now)

    row = store.entry(3)
    assert row["miniflux_read"] is True
    # Read-in-Miniflux finishes the entry (read = done) but never sets the
    # manual label.
    assert row["manual"] is True  # preserved from the user's earlier label
    assert row["done"] is True
    assert row["archived_at"]
    assert row["status"] == "read"

    # An entry with no skimmer interaction: read = done as well.
    no_interaction = _read_state_row(Client().read_entries()[0], None, now=now)
    assert no_interaction["done"] is True
    assert no_interaction["manual"] is False
    assert no_interaction["status"] == "read"


def test_miniflux_unread_reopens_locally_read_entry(tmp_path: Path) -> None:
    config = Config(
        miniflux_url="https://example.invalid", miniflux_username=None, miniflux_password=None,
        miniflux_token=None, fetch_limit=10, data_dir=tmp_path,
        overrides_path=tmp_path / "overrides", history_path=tmp_path / "history",
        write_back=False, host="127.0.0.1", port=0, must_read_keywords=[],
        possible_interest_keywords=[], ignore_keywords=[],
    )
    store = StateStore(config.decisions_path, config.history_path)
    sync = BackgroundSync(config, store)
    now = "2026-08-22T00:00:00+00:00"

    store.append([{
        "entry_id": 4, "classification": "must_read", "confidence": 1.0,
        "manual": True, "miniflux_read": True, "done": True, "status": "read",
        "archived_at": now, "observed_at": now,
    }])

    entry = {
        "id": 4, "title": "Reopened in Miniflux", "url": "https://example.com/4",
        "status": "unread", "published_at": now, "content": "<p>Example</p>",
        "feed": {"id": 1, "title": "Feed"}, "category": {"id": 1, "title": "News"},
    }

    class Client:
        def read_entries(self, limit: int = 500, changed_after: str | None = None):
            return []

        def unread_changes(self, limit: int = 500, changed_after: str | None = None):
            return [entry]

    sync._reconcile(Client(), {int(row["entry_id"]): row for row in store.rows()}, changed_after="2026-08-21T00:00:00+00:00")

    row = store.entry(4)
    assert row["status"] == "unread"
    # Unread-in-Miniflux reopens the entry: read and done both cleared, so it
    # returns to its working category.
    assert row["done"] is False
    assert row["archived_at"] is None
    assert row["miniflux_read"] is False
    assert row["manual"] is True
    assert row["classification"] == "must_read"


def test_reconcile_skips_entries_with_pending_status_writes(tmp_path: Path) -> None:
    """A click queued after the flush must win over stale Miniflux state.

    Regression: the user opens an article (skimmer records read and queues a
    write), then sync reconciles against Miniflux before flushing — Miniflux
    still says "unread" — and reconcile overwrote the fresh local decision,
    bouncing the nav counter between 0 and 1 across pages.
    """
    config = Config(
        miniflux_url="https://example.invalid", miniflux_username=None, miniflux_password=None,
        miniflux_token=None, fetch_limit=10, data_dir=tmp_path,
        overrides_path=tmp_path / "overrides", history_path=tmp_path / "history",
        write_back=True, host="127.0.0.1", port=0, must_read_keywords=[],
        possible_interest_keywords=[], ignore_keywords=[],
    )
    store = StateStore(config.decisions_path, config.history_path)
    sync = BackgroundSync(config, store)
    now = "2026-08-22T00:00:00+00:00"

    store.append([{
        "entry_id": 5, "classification": "must_read", "confidence": 1.0,
        "manual": True, "status": "read", "miniflux_read": True, "done": False,
        "observed_at": now,
    }])
    sync.request_status_change(5, "read")  # clicked; write not yet flushed

    entry = {
        "id": 5, "title": "Just opened", "url": "https://example.com/5",
        "status": "unread",  # Miniflux hasn't seen our flush yet
        "published_at": now, "content": "<p>Example</p>",
        "feed": {"id": 1, "title": "Feed"}, "category": {"id": 1, "title": "News"},
    }

    class Client:
        def read_entries(self, limit: int = 500, changed_after: str | None = None):
            return []

        def unread_changes(self, limit: int = 500, changed_after: str | None = None):
            return [entry]

    sync._reconcile(Client(), {int(row["entry_id"]): row for row in store.rows()}, changed_after=now)

    row = store.entry(5)
    assert row["miniflux_read"] is True, "pending click must not be reverted by stale Miniflux state"
    assert row["status"] == "read"
