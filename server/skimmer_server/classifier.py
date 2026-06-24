from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from .learn import SoftmaxModel, load_model, min_training_examples, train_from_history


CATEGORY_MUST_READ = "must_read"
CATEGORY_POSSIBLE_INTEREST = "possible_interest"
CATEGORY_IGNORE = "ignore"

# Confidence at or above this is treated as a decided classification; below it
# the entry lands in the uncategorized review queue.
REVIEW_THRESHOLD = 0.7


@dataclass(frozen=True)
class Classification:
    category: str
    confidence: float
    reasons: list[str]
    ai_written_signal: bool


class Classifier:
    """Learns from skimmer feedback history; optional keyword rules act as priors.

    The model is a multinomial logistic regression trained on every manual
    override and implicit read signal in the decision log (see learn.py).
    Keyword lists from config are still honoured as hard overrides when set,
    but they are no longer required for useful classifications.
    """

    def __init__(
        self,
        must_read_keywords: list[str] | None = None,
        possible_interest_keywords: list[str] | None = None,
        ignore_keywords: list[str] | None = None,
        *,
        history_rows: list[dict] | None = None,
        model_path: Path | None = None,
    ) -> None:
        self.must_read_keywords = _normalize_keywords(must_read_keywords or [])
        self.possible_interest_keywords = _normalize_keywords(possible_interest_keywords or [])
        self.ignore_keywords = _normalize_keywords(ignore_keywords or [])
        self.model_path = model_path
        self._lock = threading.Lock()
        self.model = SoftmaxModel()
        if history_rows:
            self.retrain(history_rows)
        elif model_path is not None:
            # No feedback yet: fall back to the last persisted pass so
            # classifications stay consistent across restarts.
            self.model = load_model(model_path)

    def retrain(self, history_rows: list[dict]) -> int:
        """Rebuild the model from the full decision log.

        Every retrain starts from zero weights so the same row is never
        applied twice; recency weighting inside train_from_history is what
        makes recent feedback dominate.
        """
        with self._lock:
            fresh = SoftmaxModel()
            used = train_from_history(fresh, history_rows)
            self.model = fresh
            if self.model_path is not None:
                self.model.save(self.model_path)
        return used

    @property
    def trained(self) -> bool:
        return self.model.examples >= min_training_examples()

    def classify(self, entry: dict) -> Classification:
        haystack = _entry_text(entry)
        reasons: list[str] = []
        ai_signal = _has_ai_written_signal(haystack)

        rule = _rule_classification(haystack, self)
        if rule is not None:
            category, confidence, reason = rule
            reasons.append(reason)
            return Classification(category, confidence, reasons, ai_signal)

        label, probability = self.model.predict(entry)
        reasons.append(_model_reason(label, probability))
        return Classification(label, probability, reasons, ai_signal)


def _rule_classification(haystack: str, classifier: "Classifier") -> tuple[str, float, str] | None:
    ignore_hits = _hits(haystack, classifier.ignore_keywords)
    must_hits = _hits(haystack, classifier.must_read_keywords)
    possible_hits = _hits(haystack, classifier.possible_interest_keywords)

    if must_hits:
        detail = f"must-read keyword: {', '.join(must_hits)}"
        if ignore_hits:
            detail += f" overrode ignore keyword: {', '.join(ignore_hits)}"
        return CATEGORY_MUST_READ, 0.88, detail
    if ignore_hits:
        return CATEGORY_IGNORE, 0.86, f"ignore keyword: {', '.join(ignore_hits)}"
    if possible_hits:
        return CATEGORY_POSSIBLE_INTEREST, 0.72, f"possible-interest keyword: {', '.join(possible_hits)}"
    return None


def _model_reason(category: str, confidence: float) -> str:
    return f"model predicts {category.replace('_', ' ')} ({confidence:.0%})"


def _normalize_keywords(values: list[str]) -> list[str]:
    return sorted({value.strip().lower() for value in values if value.strip()})


def _hits(haystack: str, needles: list[str]) -> list[str]:
    return [needle for needle in needles if needle in haystack]


def _entry_text(entry: dict) -> str:
    parts = [
        entry.get("title"),
        entry.get("url"),
        entry.get("author"),
    ]
    feed = entry.get("feed")
    if isinstance(feed, dict):
        parts.append(feed.get("title"))
    else:
        parts.append(entry.get("feed"))
    category = entry.get("category")
    if isinstance(category, dict):
        parts.append(category.get("title"))
    else:
        parts.append(entry.get("category_source"))
    return " ".join(str(part) for part in parts if part).lower()


def _has_ai_written_signal(haystack: str) -> bool:
    signals = [
        "as an ai",
        "as a large language model",
        "i cannot browse",
        "in today's digital landscape",
        "delve into",
        "unlock the power",
    ]
    return any(signal in haystack for signal in signals)
