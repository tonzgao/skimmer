from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from .classifier import Classifier
from .config import Config
from .miniflux import MinifluxClient
from .state import StateStore
from .storage import append_decisions, normalize_confidence

REVIEW_THRESHOLD = 0.7


class BackgroundSync:
    def __init__(self, config: Config, store: StateStore, interval_seconds: int = 300) -> None:
        self.config = config
        self.store = store
        self.interval_seconds = interval_seconds
        self.classifier: Classifier | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._wake = threading.Event()
        self._status_lock = threading.Lock()
        self._pending_read_ids: set[int] = set()
        self._pending_unread_ids: set[int] = set()
        self.last_sync_at: str | None = None
        self.reconcile_after: str | None = self._load_reconcile_after()
        self.last_error: str | None = None
        self._catalog: dict | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        if getattr(self.config, "_fixture", False):
            self.store.refresh()
            return
        self._thread = threading.Thread(target=self._run, name="skimmer-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def request_status_change(self, entry_id: int, status: str) -> None:
        with self._status_lock:
            if status == "read":
                self._pending_read_ids.add(int(entry_id))
                self._pending_unread_ids.discard(int(entry_id))
            elif status == "unread":
                # Queue unread writes too — otherwise Miniflux keeps its old
                # "read" state and the next reconcile undoes the toggle.
                self._pending_unread_ids.add(int(entry_id))
                self._pending_read_ids.discard(int(entry_id))
            else:
                return
        self.wake()

    def wake(self) -> None:
        self._wake.set()

    @property
    def status(self) -> dict:
        with self._status_lock:
            pending_count = len(self._pending_read_ids) + len(self._pending_unread_ids)
        return {
            "last_sync_at": self.last_sync_at,
            "last_error": self.last_error,
            "interval_seconds": self.interval_seconds,
            "pending_write_back_count": pending_count,
            "catalog_ready": self._catalog is not None,
        }

    def catalog(self) -> dict:
        if getattr(self.config, "_fixture", False):
            raise RuntimeError("Fixture mode does not use the background catalog.")
        if self._catalog is None:
            return {"categories": [], "feeds": []}
        return self._catalog

    def fetch_entry(self, entry_id: int) -> dict | None:
        """Fetch a single entry (with content) straight from Miniflux."""
        if getattr(self.config, "_fixture", False):
            return None
        try:
            return MinifluxClient(self.config).entry(int(entry_id))
        except Exception:
            return None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.sync_once()
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)
                # Sync failures must be visible in docker logs, not just the
                # status endpoint.
                import traceback

                print(
                    f"SYNC ERROR: {exc}\n{traceback.format_exc()}",
                    file=sys.stderr,
                    flush=True,
                )
            self._wake.wait(self.interval_seconds)
            self._wake.clear()

    def sync_once(self) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        client = MinifluxClient(self.config)
        self._refresh_catalog(client)
        # Flush our own pending status writes BEFORE reconciling. Reconcile
        # treats Miniflux as authoritative, so Miniflux must first reflect
        # everything the user clicked in skimmer — otherwise a just-made
        # "read" gets echoed back as "unread" on this pass and flipped again
        # next pass (the bouncing-nav-counter bug).
        write_back_count = self._flush_pending(client)
        known = {int(row["entry_id"]): row for row in self.store.rows()}
        reconciled = self._reconcile(client, known, changed_after=self.reconcile_after)
        classifier = self._ensure_classifier()
        new_rows = self._fetch_and_classify(client, known, classifier)
        if new_rows:
            self.store.append(new_rows)
        rescored = self._rescore_open_entries(classifier)
        self._save_reconcile_after(now)
        self.reconcile_after = now
        self.last_sync_at = now or datetime.now(timezone.utc).isoformat()
        return {"new_entries": len(new_rows), "reconciled": len(reconciled), "write_back_count": write_back_count, "rescored": rescored}

    def _rescore_open_entries(self, classifier: Classifier) -> int:
        """Re-score undecided open entries after retraining.

        Only entries the user has never interacted with get moved; manual
        decisions are immutable. Entries that cross the review threshold leave
        the uncategorized queue.
        """
        changed = 0
        observed_at = datetime.now(timezone.utc).isoformat()
        for row in self.store.rows():
            if row.get("manual") or row.get("done") or row.get("status") == "read":
                continue
            confidence = float(row.get("confidence") or 0)
            if confidence >= REVIEW_THRESHOLD:
                continue
            entry = {
                "title": row.get("title"),
                "url": row.get("url"),
                "author": (row.get("source_entry") or {}).get("author") or row.get("author"),
                "content": (row.get("source_entry") or {}).get("content") or row.get("content"),
                "feed": (row.get("source_entry") or {}).get("feed") or {"title": row.get("feed")},
                "category": (row.get("source_entry") or {}).get("category") or {"title": row.get("category_source")},
                "published_at": row.get("published_at"),
            }
            label, probability = classifier.model.predict(entry)
            if probability < REVIEW_THRESHOLD:
                continue
            updated = normalize_confidence({
                **row,
                "classification": label,
                "confidence": probability,
                "reasons": [f"model predicts {label.replace('_', ' ')} ({probability:.0%})"],
                "observed_at": observed_at,
            })
            append_decisions(self.config.decisions_path, [updated])
            changed += 1
        if changed:
            self.store.refresh()
        return changed

    def _ensure_classifier(self) -> Classifier:
        """Build/load the classifier once per process; normal syncs reuse it.

        Training is an explicit `train-model` cron operation because expected
        manual-label volume is high enough for retraining to be expensive.
        """
        if self.classifier is None:
            model_path = self.config.data_dir / "model.json"
            self.classifier = Classifier(
                must_read_keywords=self.config.must_read_keywords,
                possible_interest_keywords=self.config.possible_interest_keywords,
                ignore_keywords=self.config.ignore_keywords,
                history_rows=[],
                model_path=model_path,
            )
        self._last_manual_count = len(self._manual_rows())
        return self.classifier

    def train_model(self) -> int:
        """Explicitly rebuild and persist the model for cron use."""
        classifier = self.classifier or Classifier(
            must_read_keywords=self.config.must_read_keywords,
            possible_interest_keywords=self.config.possible_interest_keywords,
            ignore_keywords=self.config.ignore_keywords,
            history_rows=[],
            model_path=self.config.data_dir / "model.json",
        )
        self.classifier = classifier
        used = classifier.retrain(self._manual_rows())
        self._last_manual_count = len(self._manual_rows())
        return used

    def _manual_rows(self) -> list[dict]:
        """Only rows with explicit user labels are training data (and only
        their metadata is needed — content is stripped before training)."""
        slim = []
        for row in self.store.rows():
            if not row.get("manual"):
                continue
            row = dict(row)
            row.pop("content", None)
            source = row.get("source_entry")
            if isinstance(source, dict):
                row["source_entry"] = {k: v for k, v in source.items() if k != "content"}
            slim.append(row)
        return slim

    def _reconcile(
        self,
        client,
        known: dict[int, dict],
        *,
        changed_after: str | None = None,
    ) -> list[dict]:
        rows = []
        for entry in client.read_entries(limit=500, changed_after=changed_after):
            row = self._reconciled_status_row(entry, known, status="read")
            if row:
                rows.append(row)

        for entry in client.unread_changes(limit=500, changed_after=changed_after):
            row = self._reconciled_status_row(entry, known, status="unread")
            if row:
                rows.append(row)

        if rows:
            append_decisions(self.config.decisions_path, rows)
            self.store.refresh()
        return rows

    def _reconciled_status_row(self, entry: dict, known: dict[int, dict], *, status: str) -> dict | None:
        entry_id = entry.get("id")
        if not isinstance(entry_id, int):
            return None
        # A click queued after this sync's flush must win over stale Miniflux
        # state: skip reconciling anything with a pending status write.
        with self._status_lock:
            pending = entry_id in self._pending_read_ids or entry_id in self._pending_unread_ids
        if pending:
            return None
        current = self.store.entry(entry_id) or known.get(entry_id)

        if status == "read":
            if current and (current.get("miniflux_read") or current.get("status") == "read"):
                return None
            return _read_state_row(entry, current, now=datetime.now(timezone.utc).isoformat())

        if not current:
            return None
        if not (current.get("miniflux_read") or current.get("status") == "read" or current.get("done")):
            return None
        # Marked unread in Miniflux: clear the read flag and reopen (done
        # cleared), so it returns to its working list. The user's manual
        # label (if any) is preserved — read state and labels are independent.
        return normalize_confidence({
            **current,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "source_entry": {
                **(current.get("source_entry") or {}),
                **(entry.get("_source_entry") or {}),
                "id": entry_id,
                "author": entry.get("author"),
                "status": "unread",
                "feed": entry.get("feed") or (current.get("source_entry") or {}).get("feed"),
                "category": entry.get("category") or (current.get("source_entry") or {}).get("category"),
            },
            "reasons": ["marked unread in miniflux"],
            "override_reason": (current.get("override_reason") or "manual override") if current.get("manual") else None,
            "miniflux_read": False,
            "status": "unread",
            "done": False,
            "archived_at": None,
        })

    @property
    def _reconcile_checkpoint_path(self) -> Path:
        return self.config.data_dir / "sync-checkpoint.json"

    def _load_reconcile_after(self) -> str | None:
        path = self._reconcile_checkpoint_path
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            checkpoint = value.get("reconcile_after")
            schema = value.get("schema", 1)
            if schema < 2:
                return None
            return str(checkpoint) or None
        except (OSError, ValueError, TypeError):
            return None

    def _save_reconcile_after(self, value: str) -> None:
        path = self._reconcile_checkpoint_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"reconcile_after": value, "schema": 2}) + "\n",
            encoding="utf-8",
        )

    def _fetch_and_classify(self, client: MinifluxClient, known: dict[int, dict], classifier: Classifier | None = None) -> list[dict]:
        entries = client.unread_entries(self.config.fetch_limit)
        if classifier is None:
            classifier = self._ensure_classifier()
        rows = []
        observed_at = datetime.now(timezone.utc).isoformat()
        for entry in entries:
            entry_id = entry.get("id")
            if entry_id in known:
                continue
            result = classifier.classify(entry)
            write_back_action = "mark_read" if self.config.write_back and result.category == "ignore" else None
            if write_back_action and isinstance(entry_id, int):
                self.request_status_change(entry_id, "read")
            rows.append(normalize_confidence({
                **_decision_fields(entry),
                "classification": result.category,
                "confidence": result.confidence,
                "reasons": result.reasons,
                "ai_written_signal": result.ai_written_signal,
                "observed_at": observed_at,
                "manual": False,
                "status": "unread",
                "write_back": False,
                "write_back_action": write_back_action,
            }))
        return rows

    def _flush_pending(self, client: MinifluxClient) -> int:
        with self._status_lock:
            read_ids = sorted(self._pending_read_ids)
            unread_ids = sorted(self._pending_unread_ids)
            self._pending_read_ids.clear()
            self._pending_unread_ids.clear()
        flushed = 0
        try:
            client.mark_entries_read(read_ids)
            client.mark_entries_unread(unread_ids)
            flushed = len(read_ids) + len(unread_ids)
        except Exception:
            with self._status_lock:
                self._pending_read_ids.update(read_ids)
                self._pending_unread_ids.update(unread_ids)
            raise
        return flushed



    def _refresh_catalog(self, client: MinifluxClient) -> None:
        categories = [{"id": row["id"], "title": row["title"]} for row in client.categories()]
        feeds_by_category: dict[int, list[dict]] = {}
        for feed in client.feeds():
            feeds_by_category.setdefault(feed.get("category", {}).get("id"), []).append(feed)
        for category in categories:
            category_feeds = feeds_by_category.get(category["id"], [])
            category["feed_count"] = len(category_feeds)
            category["entry_count"] = sum(int(feed.get("unread_count") or 0) for feed in category_feeds)
        feed_rows = []
        for feed in client.feeds():
            feed_rows.append({
                "id": feed["id"],
                "title": feed["title"],
                "site_url": feed.get("site_url"),
                "category": feed.get("category", {}).get("title", "Uncategorized"),
                "entry_count": int(feed.get("unread_count") or 0),
            })
        self._catalog = {"categories": categories, "feeds": feed_rows}


def _decision_fields(entry: dict) -> dict:
    feed = entry.get("feed")
    category = entry.get("category")
    # Article content is deliberately NOT persisted: it is bulky (~40KB/row),
    # immutable reference data, and the reader fetches it live from Miniflux.
    # Only classification-relevant metadata is kept here.
    return {
        "entry_id": entry.get("id"),
        "title": entry.get("title"),
        "url": entry.get("url"),
        "author": entry.get("author"),
        "published_at": entry.get("published_at"),
        "feed": feed.get("title") if isinstance(feed, dict) else None,
        "category_source": category.get("title") if isinstance(category, dict) else None,
        "source_entry": {
            "id": entry.get("id"),
            "author": entry.get("author"),
            "status": entry.get("status"),
            "feed": feed,
            "category": category,
        },
    }


def _read_state_row(entry: dict, current: dict | None, *, now: str) -> dict:
    """Record that an entry was read in Miniflux.

    Read = done: one handled state. Reading never changes ``manual`` (that
    flag is reserved for explicit user labels).
    """
    fields = _decision_fields(entry)
    confidence = float(current.get("confidence", 0.5)) if current else 0.5
    reasons = [reason for reason in (current.get("reasons") or []) if reason != "read in miniflux"] if current else []
    reasons.append("read in miniflux")
    return normalize_confidence({
        **fields,
        **(current or {}),
        "classification": current["classification"] if current else "ignore",
        "confidence": confidence,
        "reasons": reasons,
        "observed_at": current.get("observed_at") if current else now,
        "archived_at": now,
        "manual": bool(current and current.get("manual")),
        "miniflux_read": True,
        "done": True,
        "status": "read",
    })
