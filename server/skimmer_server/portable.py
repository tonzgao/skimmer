"""Portable classification state.

The full model.json is a big blob of hashed weights (unfriendly for git and
for sharing). This module distills it into two smaller artifacts:

- ``model_summary.json``: per-feature class preferences as compact
  human-readable records (top tokens/feeds/authors/domains with smoothed
  weights), plus aggregate counts. Small enough to commit; can seed training
  on a fresh machine ("rolling averages, not every number").
- ``checkpoint`` fetch: when developing locally without Miniflux access, a
  trained checkpoint can be pulled from a URL (SKIMMER_MODEL_URL) instead of
  being rebuilt from history.

Summary -> model conversion is lossy by design: it captures the *direction*
of learned preference per feature, not the exact optimizer trajectory.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

from .learn import CLASSES, HASH_MOD, SoftmaxModel, _hash_feature

SUMMARY_VERSION = 1
TOP_K_PER_KIND = 200


def summarize(model: SoftmaxModel, top_k: int = TOP_K_PER_KIND) -> dict:
    """Distill hashed weights into a readable, git-sized summary."""
    # Invert the hash space using the same feature vocabulary construction is
    # impossible without the original strings, so summarization must be done
    # from features seen in history. Callers should prefer
    # summarize_from_history(); this variant summarizes raw weight rows.
    return _summarize_weights(model.weights, top_k)


def summarize_from_history(model: SoftmaxModel, history_rows: list[dict]) -> dict:
    """Build the portable summary from history + trained weights."""
    from .learn import extract_features

    feature_class_scores: dict[str, list[float]] = {}
    for row in history_rows:
        entry = _entry_view_public(row)
        if not entry.get("title") and not entry.get("url"):
            continue
        for feature in extract_features(entry):
            index = _hash_feature(feature)
            weight_row = model.weights.get(index)
            if not weight_row:
                continue
            acc = feature_class_scores.setdefault(feature, [0.0, 0.0, 0.0])
            for i in range(3):
                acc[i] += weight_row[i]

    def top(kind_prefix: str):
        items = [
            (feature, scores)
            for feature, scores in feature_class_scores.items()
            if feature.startswith(kind_prefix)
        ]
        # Rank by margin between best and worst class (most informative first).
        items.sort(key=lambda kv: max(kv[1]) - min(kv[1]), reverse=True)
        out = []
        for feature, scores in items[:top_k]:
            best = max(range(3), key=lambda i: scores[i])
            out.append({
                "feature": feature,
                "class": CLASSES[best],
                "margin": round(max(scores) - sorted(scores)[1], 4),
            })
        return out

    return {
        "version": SUMMARY_VERSION,
        "classes": list(CLASSES),
        "examples": model.examples,
        "trained_at": model.trained_at,
        "features_seen": len(feature_class_scores),
        "top": {
            "words": top("w:"),
            "bigrams": top("b:"),
            "feeds": top("feed:"),
            "domains": top("dom:"),
            "authors": top("auth:"),
            "categories": top("cat:"),
        },
    }


class SummaryModel(SoftmaxModel):
    """A SoftmaxModel reconstructed from a portable summary.

    Weights are set only for summarized features; everything else behaves as
    unseen. Good enough to bootstrap classification on a new install or to
    develop locally without pulling the whole history.
    """

    @classmethod
    def from_summary(cls, summary: dict) -> "SummaryModel":
        model = cls(trained_at=summary.get("trained_at"), examples=summary.get("examples", 0))
        for kind_items in (summary.get("top") or {}).values():
            for item in kind_items:
                feature = item["feature"]
                target = item["class"]
                margin = float(item.get("margin") or 0)
                if target not in CLASSES:
                    continue
                index = _hash_feature(feature)
                row = model.weights.setdefault(index, [0.0, 0.0, 0.0])
                row[CLASSES.index(target)] += margin
        return model


def load_or_fetch_summary(path: Path, url: str | None = None, timeout: float = 10.0) -> dict | None:
    """Load the committed summary; optionally refresh from a URL endpoint.

    Development flow: keep model_summary.json in the repo, and point
    SKIMMER_MODEL_URL at your server's /model/summary endpoint to train on
    the live artifact instead.
    """
    data: bytes | None = None
    source = "file"
    if url:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                data = response.read()
            source = f"url:{url}"
        except Exception:
            data = None  # fall back to the committed copy
    if data is None and path.exists():
        data = path.read_bytes()
        source = "file"
    if data is None:
        return None
    try:
        summary = json.loads(data)
    except ValueError:
        return None
    summary.setdefault("_source", source)
    return summary


def save_summary(summary: dict, path: Path) -> None:
    path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")


def _summarize_weights(weights: dict[int, list[float]], top_k: int) -> dict:
    ranked = []
    for index, row in weights.items():
        ordered = sorted(row, reverse=True)
        margin = ordered[0] - ordered[1] if len(ordered) > 1 else ordered[0]
        ranked.append((index, row, margin))
    ranked.sort(key=lambda x: x[2], reverse=True)
    return {
        "version": SUMMARY_VERSION,
        "classes": list(CLASSES),
        "hashed_features": [
            {"hash": index, "weights": row} for index, row, _ in ranked[:top_k]
        ],
    }


def _entry_view_public(row: dict) -> dict:
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
        "category_source": row.get("category_source"),
    }
