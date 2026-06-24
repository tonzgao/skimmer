from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from server.skimmer_server.config import Config
from server.skimmer_server.state import StateStore
from server.skimmer_server.sync import BackgroundSync


def _config(tmp_path: Path) -> Config:
    return Config(
        miniflux_url="https://example.invalid", miniflux_username=None, miniflux_password=None,
        miniflux_token=None, fetch_limit=10, data_dir=tmp_path,
        overrides_path=tmp_path / "overrides", history_path=tmp_path / "history",
        write_back=False, host="127.0.0.1", port=0, must_read_keywords=[],
        possible_interest_keywords=[], ignore_keywords=[],
    )


def _read_client(entry_id: int):
    now = "2026-08-22T00:00:00+00:00"

    class Client:
        def read_entries(self, limit: int = 500, changed_after: str | None = None):
            return [{
                "id": entry_id, "title": f"Entry {entry_id}", "url": f"https://example.com/{entry_id}",
                "status": "read", "published_at": now, "content": "<p>Example</p>",
                "feed": {"id": 1, "title": "Feed"}, "category": {"id": 1, "title": "News"},
            }]

        def unread_changes(self, limit: int = 500, changed_after: str | None = None):
            return []

    return Client()


def test_reconcile_never_reopens_done_entry(tmp_path: Path) -> None:
    """A done entry must stay done even if reconcile runs with a stale snapshot."""
    config = _config(tmp_path)
    store = StateStore(config.decisions_path, config.history_path)
    sync = BackgroundSync(config, store)
    now = "2026-08-22T00:00:00+00:00"

    # Entry was classified (manual), read in Miniflux, then marked done in skimmer.
    store.append([{
        "entry_id": 7, "classification": "must_read", "confidence": 1.0,
        "manual": True, "miniflux_read": True, "done": True, "status": "read",
        "archived_at": now, "observed_at": now,
    }])

    # Stale snapshot taken before the done row (the mid-sync race).
    stale_known = {7: {
        "entry_id": 7, "classification": "must_read", "confidence": 1.0,
        "manual": True, "status": "unread", "observed_at": now,
    }}
    sync._reconcile(_read_client(7), stale_known, changed_after=now)

    row = store.entry(7)
    assert row["done"] is True
    assert row["status"] == "read"

    # And with the current state too — repeated syncs must not append reopen rows.
    known = {int(row["entry_id"]): row for row in store.rows()}
    sync._reconcile(_read_client(7), known, changed_after=now)
    assert store.entry(7)["done"] is True


def test_reconcile_never_reopens_miniflux_read_open_entry(tmp_path: Path) -> None:
    """Open-but-read-in-Miniflux entries keep their recorded state; no rewrite loop."""
    config = _config(tmp_path)
    store = StateStore(config.decisions_path, config.history_path)
    sync = BackgroundSync(config, store)
    now = "2026-08-22T00:00:00+00:00"

    store.append([{
        "entry_id": 9, "classification": "ignore", "confidence": 1.0,
        "manual": True, "miniflux_read": True, "done": False, "status": "unread",
        "observed_at": now,
    }])
    before = store.entry(9)

    known = {int(before["entry_id"]): dict(before)}
    sync._reconcile(_read_client(9), known, changed_after=now)

    after = store.entry(9)
    assert after["done"] is False  # manual entry stays open until user marks it done
    assert after["observed_at"] == now  # no new row appended by a repeat pass
