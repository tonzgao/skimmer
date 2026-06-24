"""Online learning for skimmer classifications.

A small multinomial (softmax) logistic regression over hashed text and
categorical features. Pure standard library: training is one pass of SGD per
example, prediction is a dot product over the active feature weights. The
model persists as JSON in the data directory so it survives restarts.

Design constraints:
- fast: scoring an entry is a few hundred float multiplies;
- low resource: no numpy/sklearn, weights only exist for observed features;
- actually updating: every manual override and every implicit signal
  (read-without-interaction vs done-in-skimmer) becomes a fresh training row,
  with recency weighting so the model tracks drifting preferences;
- flexible: nothing about feeds, authors, domains, or words is hardcoded —
  everything is learned from the feedback history.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections import Counter
from hashlib import blake2b
from pathlib import Path

CATEGORY_MUST_READ = "must_read"
CATEGORY_POSSIBLE_INTEREST = "possible_interest"
CATEGORY_IGNORE = "ignore"

CLASSES = (CATEGORY_MUST_READ, CATEGORY_POSSIBLE_INTEREST, CATEGORY_IGNORE)
CLASS_INDEX = {name: index for index, name in enumerate(CLASSES)}

HASH_BITS = 18
HASH_MOD = 1 << HASH_BITS
DIM = HASH_MOD  # hashed word/bigram/categorical features share one space

LEARNING_RATE = 0.3
L2 = 1e-6
RECENCY_HALF_LIFE_DAYS = 30.0
MIN_TRAIN_ROWS = 4

_TOKEN_RE = re.compile(r"[a-z0-9]{2,}")
_DOMAIN_RE = re.compile(r"^(?:https?://)?(?:[^/@]+@)?([^/:?]+)")
_STOPWORDS = frozenset(
    "a an and are as at be but by for from has have in is it its of on or "
    "that the this to was were will with you your".split()
)


def extract_features(entry: dict) -> list[str]:
    """Map an entry to a list of symbolic features (later hash-encoded)."""
    title = str(entry.get("title") or "")
    content_text = _strip_html(str(entry.get("content") or ""))
    feed = entry.get("feed")
    feed_title = str(feed.get("title") or "") if isinstance(feed, dict) else str(feed or "")
    category = entry.get("category")
    category_title = str(category.get("title") or "") if isinstance(category, dict) else str(entry.get("category_source") or "")

    tokens = [
        token for token in _TOKEN_RE.findall(f"{title} {content_text}".lower())
        if token not in _STOPWORDS
    ]
    # Title words carry more signal than body words; emit them twice.
    title_tokens = [token for token in _TOKEN_RE.findall(title.lower()) if token not in _STOPWORDS]

    features = [f"w:{token}" for token in title_tokens] * 2
    features += [f"w:{token}" for token in tokens]
    bigrams = zip(title_tokens, title_tokens[1:])
    features += [f"b:{first} {second}" for first, second in bigrams]

    domain_match = _DOMAIN_RE.match(str(entry.get("url") or ""))
    domain = domain_match.group(1).removeprefix("www.") if domain_match else ""
    author = re.sub(r"\s+", " ", str(entry.get("author") or "").strip().lower())

    features.append(f"feed:{_slug(feed_title)}")
    if category_title:
        features.append(f"cat:{_slug(category_title)}")
    if domain:
        features.append(f"dom:{domain}")
    if author:
        features.append(f"auth:{author}")

    hour_bucket = _published_age_days(entry)
    if hour_bucket is not None:
        features.append(f"age:{min(hour_bucket, 5)}")
    return features


def featurize(entry: dict) -> Counter:
    """Hash symbolic features into a sparse count vector."""
    counts: Counter = Counter()
    for feature in extract_features(entry):
        counts[_hash_feature(feature)] += 1
    return +counts


class SoftmaxModel:
    """Multinomial logistic regression trained by online SGD."""

    def __init__(self, weights: dict[int, list[float]] | None = None, trained_at: str | None = None, examples: int = 0) -> None:
        self.weights: dict[int, list[float]] = weights or {}
        self.trained_at = trained_at
        self.examples = int(examples)

    def scores(self, vector: Counter) -> list[float]:
        result = [0.0, 0.0, 0.0]
        for index, weight_row in self.weights.items():
            count = vector.get(index)
            if not count:
                continue
            for class_index in range(3):
                result[class_index] += count * weight_row[class_index]
        return _softmax(result)

    def predict(self, entry: dict) -> tuple[str, float]:
        vector = featurize(entry)
        if not vector or not self.weights:
            return CATEGORY_POSSIBLE_INTEREST, 0.34
        probabilities = self.scores(vector)
        best_index = max(range(3), key=lambda i: probabilities[i])
        # Never claim certainty: cap automatic predictions at 99% so the UI
        # always distinguishes model output from manual labels (100%).
        return CLASSES[best_index], min(probabilities[best_index], 0.99)

    def train(self, entry: dict, label: str, weight: float = 1.0) -> None:
        target = CLASS_INDEX[label]
        vector = featurize(entry)
        if not vector:
            return
        probabilities = self.scores(vector)
        gradient_scale = LEARNING_RATE * weight
        for index, count in vector.items():
            row = self.weights.setdefault(index, [0.0, 0.0, 0.0])
            for class_index in range(3):
                error = (1.0 if class_index == target else 0.0) - probabilities[class_index]
                row[class_index] += gradient_scale * count * error
                row[class_index] -= L2 * row[class_index]
        self.examples += 1
        self.trained_at = _now()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "classes": list(CLASSES),
            "trained_at": self.trained_at,
            "examples": self.examples,
            "weights": {str(index): row for index, row in sorted(self.weights.items())},
        }
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        temporary.replace(path)

    @classmethod
    def load(cls, path: Path) -> "SoftmaxModel":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        weights = {
            int(index): [float(value) for value in row]
            for index, row in payload.get("weights", {}).items()
            if len(row) == 3
        }
        return cls(weights=weights, trained_at=payload.get("trained_at"), examples=payload.get("examples", 0))


def load_model(path: Path) -> SoftmaxModel:
    return SoftmaxModel.load(path)


def train_from_history(
    model: SoftmaxModel,
    rows: list[dict],
    *,
    now: float | None = None,
) -> int:
    """Train on every usable decision row; newest rows weigh most.

    Labels come from explicit manual overrides first, then implicit signals:
    entries read in Miniflux without any skimmer interaction are `ignore`,
    entries marked done inside skimmer carry their assigned classification.
    Returns the number of training examples applied.
    """
    clock = time.time() if now is None else now
    used = 0
    for row in rows:
        label = _label_for(row)
        if label is None:
            continue
        entry = _entry_view(row)
        age_days = max(_observed_age_days(row, clock), 0.0)
        recency_weight = 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)
        trust = 1.0 if row.get("manual") else 0.5
        model.train(entry, label, weight=trust * recency_weight)
        used += 1
    return used


def _softmax(scores: list[float]) -> list[float]:
    peak = max(scores)
    exponentials = [math.exp(score - peak) for score in scores]
    total = sum(exponentials)
    return [value / total for value in exponentials]


def _hash_feature(feature: str) -> int:
    digest = blake2b(feature.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "little") % DIM


def _slug(value: str) -> str:
    return re.sub(r"\s+", "-", value.strip().lower()) or "-"


def _strip_html(markup: str) -> str:
    return re.sub(r"<[^>]+>", " ", markup)


def _entry_view(row: dict) -> dict:
    """Rebuild a minimal Miniflux-shaped entry from a stored decision row."""
    source = row.get("source_entry") or {}
    feed = source.get("feed")
    if not isinstance(feed, dict):
        feed_title = row.get("feed")
        feed = {"title": feed_title} if feed_title else None
    category = source.get("category")
    if not isinstance(category, dict):
        category_title = row.get("category_source")
        category = {"title": category_title} if category_title else None
    return {
        "title": row.get("title"),
        "url": row.get("url"),
        "author": row.get("author") or source.get("author"),
        "content": source.get("content") or row.get("content"),
        "feed": feed,
        "category": category,
    }


def _label_for(row: dict) -> str | None:
    classification = row.get("classification")
    if classification not in CLASS_INDEX:
        return None
    if row.get("manual"):
        return str(classification)
    if not row.get("done"):
        return None
    # Done without any skimmer interaction means the user read it elsewhere.
    if str(classification) == CATEGORY_POSSIBLE_INTEREST and row.get("status") == "read":
        return CATEGORY_IGNORE
    return str(classification)


def _parse_utc(value: object) -> float | None:
    """Parse an ISO timestamp as UTC epoch seconds, never local time."""
    from datetime import datetime, timezone

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value)[:19].replace("Z", ""))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _observed_age_days(row: dict, clock: float) -> float:
    timestamp = _parse_utc(row.get("observed_at"))
    if timestamp is None:
        return 0.0
    return (clock - timestamp) / 86400.0


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _published_age_days(entry: dict) -> int | None:
    timestamp = _parse_utc(entry.get("published_at"))
    if timestamp is None:
        return None
    age_days = (time.time() - timestamp) / 86400.0
    if age_days < 0:
        return 0
    return min(int(age_days // 7), 5)


def min_training_examples() -> int:
    return MIN_TRAIN_ROWS
