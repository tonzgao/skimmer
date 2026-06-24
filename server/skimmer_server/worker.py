from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from .classifier import Classifier
from .classifier import CATEGORY_IGNORE
from .config import Config
from .fixtures import fixture_entries
from .miniflux import MinifluxClient
from .storage import append_decisions, normalize_confidence


def run_once(config: Config, limit: int | None = None, fixture: bool = False) -> dict:
    fetch_limit = limit or config.fetch_limit
    client = None if fixture else MinifluxClient(config)
    history_rows = [] if fixture else _history_rows(config)
    classifier = Classifier(
        must_read_keywords=config.must_read_keywords,
        possible_interest_keywords=config.possible_interest_keywords,
        ignore_keywords=config.ignore_keywords,
        history_rows=history_rows,
        model_path=None if fixture else config.data_dir / "model.json",
    )

    entries = fixture_entries() if fixture else client.unread_entries(fetch_limit)
    observed_at = datetime.now(timezone.utc).isoformat()
    decisions = []
    ignore_entry_ids: list[int] = []

    for entry in entries:
        classification = classifier.classify(entry)
        entry_id = entry.get("id")
        write_back_action = None
        if classification.category == CATEGORY_IGNORE and isinstance(entry_id, int):
            write_back_action = "mark_read"
            ignore_entry_ids.append(entry_id)

        decisions.append(
            normalize_confidence({
                "observed_at": observed_at,
                "entry_id": entry_id,
                "title": entry.get("title"),
                "url": entry.get("url"),
                "feed": _title(entry.get("feed")),
                "category_source": _title(entry.get("category")),
                "classification": classification.category,
                "confidence": classification.confidence,
                "reasons": classification.reasons,
                "ai_written_signal": classification.ai_written_signal,
                "write_back": False,
                "write_back_action": write_back_action,
                "manual": False,
                "status": "unread",
            })
        )

    write_back_count = 0
    if not fixture and config.write_back and ignore_entry_ids:
        client.mark_entries_read(ignore_entry_ids)
        write_back_count = len(ignore_entry_ids)
        ignored = set(ignore_entry_ids)
        for decision in decisions:
            if decision["entry_id"] in ignored:
                decision["write_back"] = True

    append_decisions(config.decisions_path, decisions)
    counts = Counter(decision["classification"] for decision in decisions)

    return {
        "fetched": len(entries),
        "stored": len(decisions),
        "write_back_enabled": config.write_back,
        "write_back_performed": write_back_count > 0,
        "write_back_count": write_back_count,
        "counts": dict(sorted(counts.items())),
        "decisions_path": str(config.decisions_path),
    }


def _title(value: object) -> str | None:
    if isinstance(value, dict):
        title = value.get("title")
        return str(title) if title else None
    return None


def _history_rows(config: Config) -> list[dict]:
    from .storage import read_latest_decisions

    try:
        return read_latest_decisions(config.history_path)
    except (OSError, ValueError, KeyError):
        return []
