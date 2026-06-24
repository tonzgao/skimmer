"""Built-in sample data for local interface testing."""

from __future__ import annotations


def fixture_entries() -> list[dict]:
    content = "<p>This is sample article content used to exercise the Skimmer reader.</p>"
    return [
        {
            "id": 1001,
            "title": "Security advisory affects production deployments",
            "url": "https://example.com/security-advisory",
            "author": "Example Security Team",
            "content": content,
            "published_at": "2026-08-21T12:00:00Z",
            "status": "unread",
            "feed": {"id": 11, "title": "Infrastructure Alerts", "site_url": "https://example.com", "category": {"id": 1, "title": "Operations"}},
            "category": {"id": 1, "title": "Operations"},
        },
        {
            "id": 1002,
            "title": "A field guide to local-first software",
            "url": "https://example.com/local-first",
            "author": "Casey Example",
            "content": content,
            "published_at": "2026-08-21T10:00:00Z",
            "status": "unread",
            "feed": {"id": 12, "title": "Software Design", "site_url": "https://example.com", "category": {"id": 2, "title": "Technology"}},
            "category": {"id": 2, "title": "Technology"},
        },
        {
            "id": 1003,
            "title": "Unlock the power of effortless productivity",
            "url": "https://example.com/ai-productivity",
            "author": "Content Engine",
            "content": content,
            "published_at": "2026-08-20T08:00:00Z",
            "status": "unread",
            "feed": {"id": 13, "title": "Syndicated Marketing", "site_url": "https://example.com", "category": {"id": 3, "title": "Promotions"}},
            "category": {"id": 3, "title": "Promotions"},
        },
    ]


def fixture_catalog() -> tuple[list[dict], list[dict]]:
    entries = fixture_entries()
    category_map = {}
    for entry in entries:
        category = entry["category"]
        feed = entry["feed"]
        category_map[category["id"]] = {
            "id": category["id"],
            "title": category["title"],
            "feed_count": category_map.get(category["id"], {}).get("feed_count", 0) + 1,
            "entry_count": category_map.get(category["id"], {}).get("entry_count", 0) + 1,
        }
    feed_counts = {}
    for entry in entries:
        feed_counts[entry["feed"]["id"]] = feed_counts.get(entry["feed"]["id"], 0) + 1
    feeds = []
    for entry in entries:
        feed = dict(entry["feed"])
        feed["entry_count"] = feed_counts[feed["id"]]
        feeds.append(feed)
    categories = sorted(category_map.values(), key=lambda row: row["title"])
    return categories, sorted(feeds, key=lambda row: row["title"])
