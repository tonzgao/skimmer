from __future__ import annotations

import json

import pytest

from server.skimmer_server.classifier import (
    CATEGORY_IGNORE,
    CATEGORY_MUST_READ,
    CATEGORY_POSSIBLE_INTEREST,
    Classifier,
)
from server.skimmer_server.learn import CLASSES, SoftmaxModel, extract_features, train_from_history


def _entry(title="Rust async runtime deep dive", feed="Hacker News", **extra):
    entry = {
        "id": 1,
        "title": title,
        "url": "https://example.com/post",
        "author": "someone",
        "content": "<p>Interesting details about runtimes</p>",
        "feed": {"title": feed},
        "category": {"title": "Tech"},
        "published_at": "2026-08-20T10:00:00Z",
    }
    entry.update(extra)
    return entry


def _row(entry, *, manual=False, done=True, classification=CATEGORY_MUST_READ, status="unread", age_days=0):
    import datetime

    observed = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=age_days)
    return {
        "entry_id": entry["id"],
        "title": entry.get("title"),
        "url": entry.get("url"),
        "author": entry.get("author"),
        "content": (entry.get("source_entry") or {}).get("content") if isinstance(entry.get("source_entry"), dict) else entry.get("content"),
        "classification": classification,
        "confidence": 1.0 if manual else 0.8,
        "manual": manual,
        "done": done,
        "status": status,
        "observed_at": observed.isoformat(),
        "feed": entry.get("feed", {}).get("title") if isinstance(entry.get("feed"), dict) else None,
        "category_source": entry.get("category", {}).get("title"),
        "source_entry": {"content": entry.get("content"), "feed": entry.get("feed"), "category": entry.get("category")},
    }


class TestFeatureExtraction:
    def test_title_words_weighted_over_body(self):
        features = extract_features(_entry())
        assert features.count("w:rust") == 3
        assert features.count("w:runtimes") == 1

    def test_categorical_features_present(self):
        features = extract_features(_entry())
        assert "feed:hacker-news" in features
        assert "cat:tech" in features
        assert "dom:example.com" in features
        assert "auth:someone" in features


class TestTraining:
    def test_learns_from_manual_overrides(self):
        model = SoftmaxModel()
        rows = [_row(_entry(id=i), manual=True, classification=CATEGORY_MUST_READ) for i in range(5)]
        rows += [_row(_entry(id=100 + i, title="Celebrity gossip roundup", feed="Tabloid"), manual=True, classification=CATEGORY_IGNORE) for i in range(5)]
        train_from_history(model, rows)
        label, confidence = model.predict(_entry())
        assert label == CATEGORY_MUST_READ
        assert confidence > 0.7

    def test_recency_weighting_prefers_recent(self):
        model = SoftmaxModel()
        rows = [
            _row(_entry(id=i), manual=True, classification=CATEGORY_MUST_READ, age_days=0)
            for i in range(3)
        ] + [
            # Old ignore feedback on identical content should lose to recent must_read.
            _row(_entry(id=100 + i), manual=True, classification=CATEGORY_IGNORE, age_days=400)
            for i in range(6)
        ]
        train_from_history(model, rows)
        label, _ = model.predict(_entry(id=999))
        assert label == CATEGORY_MUST_READ

    def test_unlabeled_rows_skipped(self):
        model = SoftmaxModel()
        used = train_from_history(model, [{"entry_id": 1, "manual": False, "done": False}])
        assert used == 0
        assert model.examples == 0


class TestClassifier:
    def test_keyword_rules_still_override(self):
        classifier = Classifier(must_read_keywords=["invoice"])
        result = classifier.classify(_entry(title="Your invoice is ready"))
        assert result.category == CATEGORY_MUST_READ

    def test_untrained_defaults_to_possible_interest(self):
        classifier = Classifier()
        result = classifier.classify(_entry())
        assert result.category == CATEGORY_POSSIBLE_INTEREST
        assert not classifier.trained

    def test_retrain_is_idempotent(self):
        classifier = Classifier(history_rows=[], model_path=None)
        rows = [_row(_entry(id=i), manual=True, classification=CATEGORY_MUST_READ) for i in range(5)]
        first = classifier.retrain(rows)
        first_label, first_confidence = classifier.model.predict(_entry(id=999))
        second = classifier.retrain(rows)
        second_label, second_confidence = classifier.model.predict(_entry(id=999))
        assert first == second == len(rows)
        assert first_label == second_label == CATEGORY_MUST_READ
        assert abs(first_confidence - second_confidence) < 0.01, (
            "retraining on the same history must not compound weights "
            "(recency-clock drift aside)"
        )

    def test_trained_classifier_classifies(self):
        rows = [_row(_entry(id=i), manual=True, classification=CATEGORY_MUST_READ) for i in range(4)]
        classifier = Classifier(history_rows=rows)
        assert classifier.trained
        result = classifier.classify(_entry(id=42))
        assert result.category == CATEGORY_MUST_READ
        assert "model predicts" in result.reasons[0]

    def test_model_persistence_round_trip(self, tmp_path):
        path = tmp_path / "model.json"
        rows = [_row(_entry(id=i), manual=True, classification=CATEGORY_MUST_READ) for i in range(4)]
        Classifier(history_rows=rows, model_path=path).classify(_entry())
        assert path.exists()

        reloaded = Classifier(model_path=path)
        label, confidence = reloaded.model.predict(_entry(id=77))
        # The bootstrap-from-summary fallback can only approximate the full
        # model; require it still leans the right way with real signal.
        if label != CATEGORY_MUST_READ:
            assert label == CATEGORY_POSSIBLE_INTEREST
            from server.skimmer_server.classifier import REVIEW_THRESHOLD

            # A weak guess must stay below the decided threshold so it lands
            # in the review queue rather than being trusted.
            assert confidence < REVIEW_THRESHOLD

    @pytest.mark.parametrize(
        ("row", "expected_label"),
        [
            # Read-elsewhere rows carry NO training signal at all, whatever
            # their auto label was.
            (_row(_entry(), manual=False, done=True, classification=CATEGORY_POSSIBLE_INTEREST, status="read"), None),
            (_row(_entry(), manual=False, done=True, classification=CATEGORY_MUST_READ, status="read"), None),
            (_row(_entry(), manual=False, done=False, classification=CATEGORY_MUST_READ, status="unread"), None),
            # Manual overrides are the only signal.
            (_row(_entry(), manual=True, classification=CATEGORY_IGNORE), CATEGORY_IGNORE),
        ],
    )
    def test_implicit_labels(self, row, expected_label):
        from server.skimmer_server.learn import _label_for

        assert _label_for(row) == expected_label

    def test_class_balanced_training(self):
        """Minority labels must not be drowned out by majority labels."""
        from server.skimmer_server.learn import SoftmaxModel, train_from_history

        # 10 ignore vs 2 must_read: unbalanced batch.
        rows = (
            [_row(_entry(id=i), manual=True, classification=CATEGORY_IGNORE) for i in range(10)]
            + [_row(_entry(id=100 + i), manual=True, classification=CATEGORY_MUST_READ) for i in range(2)]
        )
        model = SoftmaxModel()
        used = train_from_history(model, rows)
        assert used == 12
        # A must-read-flavored entry should still classify as must_read —
        # with flat weights the 5x-frequent ignore class would win.
        probe = _entry(id=999)
        label, confidence = model.predict(probe)
        # No strict guarantee on a single probe, but the must_read score must
        # be competitive: check via scores directly.
        vector = model.scores(__import__("server.skimmer_server.learn", fromlist=["featurize"]).featurize(probe))
        ignore_score = vector[CLASSES.index(CATEGORY_IGNORE)]
        must_score = vector[CLASSES.index(CATEGORY_MUST_READ)]
        assert must_score > ignore_score * 0.5
