from __future__ import annotations

import json
import threading

from .storage import normalize_confidence, read_latest_decisions


class StateStore:
    def __init__(self, decisions_path, history_path) -> None:
        self.decisions_path = decisions_path
        self.history_path = history_path
        self._lock = threading.RLock()
        self._cache: dict[int, dict] | None = None

    def refresh(self) -> None:
        with self._lock:
            self._cache = {int(row["entry_id"]): row for row in read_latest_decisions(self.decisions_path)}

    def rows(self) -> list[dict]:
        with self._lock:
            if self._cache is None:
                self.refresh()
            return sorted((normalize_confidence(dict(row)) for row in self._cache.values()), key=self._sort_key, reverse=True)

    def unread(self, limit: int | None = 200) -> list[dict]:
        return [row for row in self.rows() if row.get("status") != "read"][:limit] if limit is not None else [row for row in self.rows() if row.get("status") != "read"]

    def compact(self) -> dict:
        """Rewrite both logs, dropping superseded rows and article content.

        The decisions log is last-write-wins on read and the training path
        (learn.py extract_features) uses the same metadata the classifier
        sees at serve time — title/url/author/feed/category — so stripping
        stored article bodies is lossless for behavior. Returns counts.
        """
        from .storage import write_decisions

        with self._lock:
            self.refresh()
            before = sum(1 for _ in open(self.decisions_path, encoding="utf-8")) if self.decisions_path.exists() else 0
            rows = [self._slim(row) for row in self._cache.values()]
            write_decisions(self.decisions_path, sorted(rows, key=self._sort_key, reverse=True))
            self._cache = {int(row["entry_id"]): row for row in rows}

            history_rows = []
            if self.history_path.exists():
                for line in self.history_path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        try:
                            history_rows.append(self._slim(json.loads(line)))
                        except ValueError:
                            continue
                write_decisions(self.history_path, history_rows)

            return {
                "decision_rows_before": before,
                "decision_rows_after": len(rows),
                "history_rows_kept": len(history_rows),
            }

    @staticmethod
    def _slim(row: dict) -> dict:
        row = dict(row)
        row.pop("content", None)
        source = row.get("source_entry")
        if isinstance(source, dict) and "content" in source:
            row["source_entry"] = {k: v for k, v in source.items() if k != "content"}
        return row

    def by_category(self, classification: str) -> list[dict]:
        # Working category: UNREAD entries with this label — same definition
        # as the nav counter, so the number on a folder always matches what
        # opening it shows. This MUST also match _folder_rows' review_category
        # branch, which drives reader pagination over the same list.
        return [
            row for row in self.rows()
            if row.get("classification") == classification and row.get("status") != "read"
        ]

    def entry(self, entry_id: int) -> dict | None:
        with self._lock:
            if self._cache is None:
                self.refresh()
            row = self._cache.get(int(entry_id))
            return dict(row) if row else None

    def append(self, rows: list[dict]) -> None:
        from .storage import append_decisions

        with self._lock:
            if not rows:
                return
            append_decisions(self.decisions_path, rows)
            self.refresh()

    def mark_local_read(self, rows: list[dict]) -> None:
        from .storage import append_decisions

        with self._lock:
            append_decisions(self.decisions_path, rows)
            self.refresh()

    @staticmethod
    def _sort_key(row: dict) -> str:
        return str(row.get("published_at") or row.get("observed_at"))
