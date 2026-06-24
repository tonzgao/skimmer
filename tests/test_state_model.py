from __future__ import annotations

import json
from pathlib import Path

from server.skimmer_server.storage import read_latest_decisions
from server.skimmer_server.http_api import _archived_after
from datetime import datetime, timezone


def test_latest_decision_wins_and_snapshots_do_not_duplicate(tmp_path: Path) -> None:
    path = tmp_path / "decisions.jsonl"
    append_rows(path, [
        {"entry_id": 1, "classification": "possible_interest", "confidence": 0.5, "observed_at": "2026-08-20", "status": "unread"},
        {"entry_id": 1, "classification": "must_read", "confidence": 0.8, "observed_at": "2026-08-21", "status": "unread", "manual": True},
        {"entry_id": 2, "classification": "ignore", "confidence": 0.99, "observed_at": "2026-08-21", "status": "read"},
    ])

    rows = {row["entry_id"]: row for row in read_latest_decisions(path)}

    assert len(rows) == 2
    assert rows[1]["classification"] == "must_read"
    assert rows[1]["confidence"] == 1.0
    assert rows[2]["status"] == "read"


def test_automatic_confidence_is_capped() -> None:
    path = Path("/dev/null")
    row = {"entry_id": 1, "confidence": 0.99, "manual": False}

    from server.skimmer_server.storage import normalize_confidence

    assert normalize_confidence(row)["confidence"] == 0.99


def test_manual_state_has_full_certainty_and_history_retains_one_week() -> None:
    path = Path("/dev/null")
    row = {"entry_id": 1, "confidence": 0.8, "manual": True}

    from server.skimmer_server.storage import normalize_confidence

    assert normalize_confidence(row)["confidence"] == 1.0
    cutoff = datetime.now(timezone.utc)
    assert _archived_after({"archived_at": cutoff.isoformat()}, cutoff)


def append_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
