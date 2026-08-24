from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def append_decisions(path: Path, decisions: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for decision in decisions:
            handle.write(json.dumps(decision, sort_keys=True))
            handle.write("\n")


def write_decisions(path: Path, decisions: list[dict]) -> None:
    """Atomically replace the decisions log (used by compaction)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for decision in decisions:
                handle.write(json.dumps(decision, sort_keys=True))
                handle.write("\n")
        os.replace(tmp_path, path)
    except BaseException:
        Path(tmp_path).unlink(missing_ok=True)
        raise


def read_latest_decisions(path: Path, limit: int | None = None) -> list[dict]:
    """Return one current state per entry, newest update winning.

    JSONL remains append-only for durability, but it is a mutable-state log:
    every read reconstructs the latest record by entry id instead of exposing
    repeated classification snapshots.
    """
    latest: dict[int, dict] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            entry_id = int(row["entry_id"])
            row.setdefault("manual", False)
            row.setdefault("status", entry_status(row))
            # Single source of truth: when the row records a Miniflux read
            # flag, status is derived from it, never trusted separately.
            if "miniflux_read" in row:
                row["status"] = "read" if row["miniflux_read"] else "unread"
            latest[entry_id] = normalize_confidence(row)
    rows = sorted(
        latest.values(),
        key=lambda row: str(row.get("published_at") or row.get("observed_at")),
        reverse=True,
    )
    return rows[:limit] if limit is not None else rows


def read_overrides(path: Path) -> dict[int, dict]:
    if not path.exists():
        return {}

    overrides = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        entry_id = int(row["entry_id"])
        overrides[entry_id] = {
            "entry_id": entry_id,
            "classification": row["classification"],
            "reason": row.get("reason") or "manual override",
            "updated_at": row["updated_at"],
            "categorized_at": row.get("categorized_at"),
            "archived": bool(row.get("archived", False)),
            "archived_at": row.get("archived_at"),
        }
    return overrides


def append_override(path: Path, entry_id: int, classification: str, reason: str | None, *, categorized: bool = False) -> dict:
    from datetime import datetime, timezone

    row = {
        "entry_id": int(entry_id),
        "classification": classification,
            "reason": reason or "manual override",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "categorized_at": datetime.now(timezone.utc).isoformat() if categorized else None,
            "archived": False,
            "archived_at": None,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True))
        handle.write("\n")
    return row


def atomic_write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, sort_keys=True))
                handle.write("\n")
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def compact_decisions(path: Path) -> None:
    if not path.exists():
        return
    latest: dict[int, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        key = int(row["entry_id"])
        latest[key] = row
    atomic_write_jsonl(path, sorted(latest.values(), key=lambda row: (row["observed_at"], int(row["entry_id"]))))


def append_history(path: Path, rows: list[dict]) -> None:
    append_decisions(path, rows)


def entry_status(row: dict) -> str:
    return str(row.get("status") or ("read" if row.get("archived_at") else "unread"))


def normalize_confidence(row: dict) -> dict:
    confidence = float(row.get("confidence", 0))
    manual = bool(row.get("manual"))
    row["manual"] = manual
    row["confidence"] = 1.0 if manual else min(confidence, 0.99)
    return row


def archive_override(path: Path, entry_id: int) -> dict | None:
    return _update_latest_override(path, int(entry_id), {"archived": True, "archived_at": _now()})


def _update_latest_override(path: Path, entry_id: int, changes: dict) -> dict | None:
    latest_index = None
    latest_row = None
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines() if path.exists() else []):
        if not line.strip():
            continue
        row = json.loads(line)
        if int(row["entry_id"]) == entry_id:
            latest_index = index
            latest_row = row
    if latest_row is None:
        return None
    latest_row.update(changes)
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[latest_index] = json.dumps(latest_row, sort_keys=True)
    atomic_write_jsonl(path, [json.loads(line) for line in lines])
    return latest_row


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
