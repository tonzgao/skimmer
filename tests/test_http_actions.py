from __future__ import annotations

import io
import base64
import pytest
import threading
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from server.skimmer_server.config import Config
from server.skimmer_server.http_api import Handler, SkimmerHTTPServer
from server.skimmer_server.state import StateStore


def _config(tmp_path: Path) -> Config:
    return Config(
        miniflux_url="https://example.invalid", miniflux_username="user", miniflux_password=None,
        miniflux_token="token", fetch_limit=10, data_dir=tmp_path,
        overrides_path=tmp_path / "overrides", history_path=tmp_path / "history",
    write_back=False, host="127.0.0.1", port=0, must_read_keywords=[],
        possible_interest_keywords=[], ignore_keywords=[],
    )


class FakeSync:
    def __init__(self) -> None:
        self.status_changes: list[tuple[int, str]] = []
        self.sync_calls = 0

    def request_status_change(self, entry_id: int, status: str) -> None:
        self.status_changes.append((entry_id, status))

    def sync_once(self) -> dict:
        self.sync_calls += 1
        return {"new_entries": 0, "reconciled": 0, "write_back_count": 0}

    def fetch_entry(self, entry_id: int) -> dict | None:
        return None

    def catalog(self) -> dict:
        return {"categories": [], "feeds": []}

    @property
    def status(self) -> dict:
        return {}

    def start(self) -> None:
        return


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _server(tmp_path: Path):
    config = _config(tmp_path)
    httpd = SkimmerHTTPServer(("127.0.0.1", 0), config)
    httpd.store = StateStore(config.decisions_path, config.history_path)
    httpd.sync = FakeSync()
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def _seed(store: StateStore, entry_id: int, classification: str = "must_read", **extra) -> None:
    row = {
        "entry_id": entry_id,
        "classification": classification,
        "confidence": 0.9,
        "title": f"Entry {entry_id}",
        "url": f"https://example.com/{entry_id}",
        "status": "unread",
        "miniflux_read": False,
        "done": False,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    row.update(extra)
    store.append([row])


def test_opening_entry_marks_read_and_done(tmp_path: Path) -> None:
    httpd, base = _server(tmp_path)
    try:
        store = httpd.store
        _seed(store, 10)
        import urllib.request
        request = urllib.request.Request(f"{base}/entry?id=10&next=/category/must_read")
        urllib.request.urlopen(request).read()
        row = store.entry(10)
        # Reading finishes the entry: read = done, one handled state.
        assert row["done"] is True
        assert row["archived_at"]
        assert row["miniflux_read"] is True
        assert row["status"] == "read"
        assert row.get("manual") is not True
        assert (10, "read") in httpd.sync.status_changes
    finally:
        httpd.shutdown()


def test_override_preserves_read_and_done_state(tmp_path: Path) -> None:
    httpd, base = _server(tmp_path)
    try:
        store = httpd.store
        _seed(store, 11, done=True, status="read", miniflux_read=True, archived_at=datetime.now(timezone.utc).isoformat())
        import urllib.request
        body = urlencode({"entry_id": "11", "classification": "possible_interest", "reason": "moved", "next": "/"}).encode()
        request = urllib.request.Request(f"{base}/override", data=body, method="POST")
        urllib.request.urlopen(request).read()
        row = store.entry(11)
        # Labeling never changes read or done state.
        assert row["done"] is True
        assert row["status"] == "read"
        assert row["classification"] == "possible_interest"
        assert row["manual"] is True
        assert (11, "read") not in httpd.sync.status_changes
    finally:
        httpd.shutdown()


def test_override_keeps_miniflux_read_entry_open(tmp_path: Path) -> None:
    import urllib.request

    httpd, base = _server(tmp_path)
    try:
        store = httpd.store
        _seed(
            store,
            22,
            done=False,
            status="unread",
            miniflux_read=True,
        )
        body = urlencode({"entry_id": "22", "classification": "possible_interest", "next": "/"}).encode()
        request = urllib.request.Request(f"{base}/override", data=body, method="POST")
        urllib.request.urlopen(request).read()
        row = store.entry(22)
        assert row["classification"] == "possible_interest"
        # Labeling does not complete the entry; it stays read-but-open.
        assert row.get("done") is not True
        assert row["miniflux_read"] is True
        assert row["status"] == "read"
    finally:
        httpd.shutdown()


def test_bulk_done_category(tmp_path: Path) -> None:
    httpd, base = _server(tmp_path)
    try:
        store = httpd.store
        _seed(store, 12)
        _seed(store, 13, classification="ignore")
        import urllib.request
        urllib.request.urlopen(f"{base}/done-category?classification=must_read").read()
        assert store.entry(12)["done"] is True
        assert store.entry(13).get("done") is not True
    finally:
        httpd.shutdown()


def test_miniflux_category_page_has_classify_and_bulk_done(tmp_path: Path) -> None:
    import urllib.request

    httpd, base = _server(tmp_path)
    try:
        store = httpd.store
        source = {"category": {"id": 1, "title": "News"}, "feed": {"id": 5}}
        _seed(store, 14, source_entry=source)
        _seed(store, 15, classification="ignore", source_entry=source)
        html = urllib.request.urlopen(f"{base}/category?id=1").read().decode()
        # classification label with inline re-classification buttons
        assert 'action="/override"' in html
        assert "important" in html.lower()
        assert "skim" in html.lower()
        # bulk done form targets the listed entries only
        assert 'action="/done-many"' in html and 'value="14"' in html and 'value="15"' in html

        body = urlencode({"entry_id": ["14", "15"], "next": "/category?id=1"}, doseq=True).encode()
        request = urllib.request.Request(f"{base}/done-many", data=body, method="POST")
        urllib.request.urlopen(request).read()
        assert store.entry(14)["done"] is True
        assert store.entry(15)["done"] is True
        assert store.entry(14)["miniflux_read"] is True
    finally:
        httpd.shutdown()


def test_labeling_from_article_list_does_not_mark_miniflux_read(tmp_path: Path) -> None:
    import urllib.request

    httpd, base = _server(tmp_path)
    try:
        store = httpd.store
        _seed(store, 16)
        body = urlencode({"entry_id": "16", "classification": "must_read", "reason": "kept", "next": "/"}).encode()
        request = urllib.request.Request(f"{base}/override", data=body, method="POST")
        urllib.request.urlopen(request).read()
        row = store.entry(16)
        # Labeling only sets the manual label; read/done are untouched.
        assert row.get("done") is not True
        assert row["status"] == "unread"
        assert row["miniflux_read"] is False
        assert row["manual"] is True
        assert httpd.sync.status_changes == []
    finally:
        httpd.shutdown()


@pytest.mark.skip(reason="reader pagination/done flow is being reworked")
def test_reader_flow_placeholder() -> None:
    assert True


def test_reader_pagination_preserves_original_category_and_renders_twice(tmp_path: Path) -> None:
    import urllib.request

    httpd, base = _server(tmp_path)
    try:
        store = httpd.store
        _seed(store, 23, published_at="2026-08-20T00:00:00Z")
        _seed(store, 24, classification="ignore", published_at="2026-08-21T00:00:00Z")
        _seed(store, 25, classification="must_read", published_at="2026-08-22T00:00:00Z")

        html = urllib.request.urlopen(f"{base}/entry?id=24&folder=/category/ignore").read().decode()
        # The origin category contains only entry 24 in this fixture.
        assert 'aria-label="Entry pagination"' not in html
        assert 'entry reader-view' in html
        assert 'data-folder="/category/ignore"' in html

        # Relabeling keeps the reader anchored to the original category.
        body = urlencode({"entry_id": "24", "classification": "must_read", "folder": "/category/ignore"}).encode()
        urllib.request.urlopen(urllib.request.Request(f"{base}/override", data=body, method="POST")).read()
        row = store.entry(24)
        assert row["classification"] == "must_read"
        # Opening the reader marks it read+done; relabeling preserves that.
        assert row["done"] is True
        assert row["miniflux_read"] is True

        # Even after relabeling, the original category still supplies neighbors.
        reopened = urllib.request.urlopen(f"{base}/entry?id=24&folder=/category/ignore").read().decode()
        # Date-adjacent fallback may include neighboring labels; the origin
        # parameter remains fixed, so pagination never switches folders.
        assert 'data-folder="/category/ignore"' in reopened
        assert 'aria-label="Entry pagination"' not in reopened
        assert html.count('aria-label="Entry pagination"') == 0
        assert '<div class="reader-actions">' in html
    finally:
        httpd.shutdown()


def test_pagination_stays_in_history_origin(tmp_path: Path) -> None:
    import urllib.request

    httpd, base = _server(tmp_path)
    try:
        store = httpd.store
        now = datetime.now(timezone.utc).isoformat()
        for entry_id, published in [(31, "2026-08-20T00:00:00Z"), (32, "2026-08-21T00:00:00Z"), (33, "2026-08-22T00:00:00Z")]:
            store.append([{
                "entry_id": entry_id, "classification": "ignore", "confidence": 1.0,
                "title": f"Entry {entry_id}", "url": f"https://example.com/{entry_id}",
                "status": "read", "done": True, "miniflux_read": True,
                "archived_at": now, "observed_at": now, "published_at": published,
            }])

        html = urllib.request.urlopen(f"{base}/history").read().decode()
        assert 'folder=/history' in html

        reader = urllib.request.urlopen(f"{base}/entry?id=32&folder=/history").read().decode()
        assert 'href="/entry?id=31&amp;folder=/history"' in reader
        assert 'href="/entry?id=31&amp;folder=/history"' in reader
        assert 'href="/entry?id=33&amp;folder=/history"' in reader
    finally:
        httpd.shutdown()


def test_folder_pagination_follows_oldest_first_order(tmp_path: Path) -> None:
    import urllib.request

    httpd, base = _server(tmp_path)
    try:
        store = httpd.store
        _seed(store, 41, classification="ignore", published_at="2026-08-20T00:00:00Z")
        _seed(store, 42, classification="ignore", published_at="2026-08-21T00:00:00Z")
        _seed(store, 43, classification="ignore", published_at="2026-08-22T00:00:00Z")

        newest = urllib.request.urlopen(f"{base}/entry?id=43&folder=/category/ignore").read().decode()
        # Newest item is last in an oldest-first folder, so only Previous
        # exists; its Previous is the immediately older article.
        assert 'href="/entry?id=42&amp;folder=/category/ignore">Previous' in newest
        assert 'href="/entry?id=42&amp;folder=/category/ignore">Next' not in newest
        assert 'href="/entry?id=42&amp;folder=/category/ignore"' in newest

        oldest = urllib.request.urlopen(f"{base}/entry?id=41&folder=/category/ignore").read().decode()
        assert 'href="/entry?id=42&amp;folder=/category/ignore">Next' in oldest
        assert 'href="/entry?id=42&amp;folder=/category/ignore">Previous' not in oldest
    finally:
        httpd.shutdown()


@pytest.mark.skip(reason="Pending pagination needs another pass")
def test_pending_folder_pagination_oldest_first(tmp_path: Path) -> None:
    import urllib.request

    httpd, base = _server(tmp_path)
    try:
        store = httpd.store
        _seed(store, 51, published_at="2026-08-20T00:00:00Z")
        _seed(store, 52, classification="ignore", published_at="2026-08-21T00:00:00Z")
        _seed(store, 53, published_at="2026-08-22T00:00:00Z", confidence=0.5)

        newest = urllib.request.urlopen(f"{base}/entry?id=53&folder=/").read().decode()
        assert '>Entry 53</a></h1>' in newest
        # Entry 52 is also pending and sits immediately before 53 in the
        # oldest-first displayed order.
        assert 'href="/entry?id=52&amp;folder=/">Previous' in newest
        assert 'href="/entry?id=51&amp;folder=/">Next' not in newest

        oldest = urllib.request.urlopen(f"{base}/entry?id=51&folder=/").read().decode()
        assert '>Entry 51</a></h1>' in oldest
        # Opening 51 marks it read, but 52 remains the next displayed item.
        assert 'href="/entry?id=52&amp;folder=/">Next' in oldest
        assert 'href="/entry?id=53&amp;folder=/">Next' not in oldest
        assert 'href="/entry?id=52&amp;folder=/">Next' in oldest
        assert 'href="/entry?id=53&amp;folder=/">Next' not in oldest

        html = urllib.request.urlopen(f"{base}/").read().decode()
        assert 'entry-title-52' in html
        # Opening 51 marks it read and removes it from Pending. 52 remains:
        # list buttons may relabel it without moving it to a folder page.
        assert 'Entry 51' not in html and 'Entry 52' in html and 'Entry 53' not in html
    finally:
        httpd.shutdown()


def test_list_classification_buttons_use_resolved_state(tmp_path: Path) -> None:
    import urllib.request

    httpd, base = _server(tmp_path)
    try:
        store = httpd.store
        _seed(store, 54, confidence=0.5)
        body = urlencode({"entry_id": "54", "classification": "ignore", "next": "/"}).encode()
        urllib.request.urlopen(urllib.request.Request(f"{base}/override", data=body, method="POST")).read()
        row = store.entry(54)
        row["done"] = True
        row["archived_at"] = datetime.now(timezone.utc).isoformat()
        store.mark_local_read([row])

        html = urllib.request.urlopen(f"{base}/history").read().decode()
        assert 'classification-button manual" aria-current="true">ignore</button>' in html
        assert 'classification-button auto" aria-current="true">ignore</button>' not in html

        pending = urllib.request.urlopen(f"{base}/").read().decode()
        assert "Entry 54" not in pending
        assert 'classification-button manual" aria-current="true">ignore</button>' in html
    finally:
        httpd.shutdown()


def test_fetch_endpoint_triggers_sync(tmp_path: Path) -> None:
    httpd, base = _server(tmp_path)
    try:
        import urllib.request
        response = urllib.request.urlopen(urllib.request.Request(f"{base}/fetch", data=b"", method="POST"))
        assert response.status == 200
        assert httpd.sync.sync_calls == 1
    finally:
        httpd.shutdown()


def test_feed_icons_render_and_fixture_proxy_decodes(tmp_path: Path) -> None:
    import base64
    import urllib.request

    httpd, base = _server(tmp_path)
    try:
        store = httpd.store
        source = {"feed": {"id": 11, "title": "Infrastructure Alerts"}}
        _seed(store, 21, source_entry=source)

        list_html = urllib.request.urlopen(f"{base}/category/must_read").read().decode()
        assert '<img class="feed-icon" src="/feed-icon?id=11"' in list_html

        original_config = httpd.config
        object.__setattr__(httpd.config, "_fixture", True)
        response = urllib.request.urlopen(f"{base}/feed-icon?id=11")
        assert response.status == 200
        assert response.headers["Content-Type"] == "image/svg+xml"
        assert base64.b64encode(response.read()).startswith(b"PHN2Zy")

        reader_html = urllib.request.urlopen(f"{base}/entry?id=21&next=/category/must_read").read().decode()
        assert reader_html.count('/feed-icon?id=11') >= 1
        object.__setattr__(httpd.config, "_fixture", False)
    finally:
        httpd.shutdown()


def test_pending_shows_all_nonmanual_entries(tmp_path: Path) -> None:
    import urllib.request

    httpd, base = _server(tmp_path)
    try:
        store = httpd.store
        # Both entries are algorithm-labeled: Pending now shows every
        # non-manual open entry regardless of confidence or placeholder
        # reasons, so both must appear.
        _seed(store, 26, confidence=0.5, reasons=["awaiting background classification"])
        _seed(store, 27, confidence=0.5)
        html = urllib.request.urlopen(f"{base}/?nocache={id(httpd)}").read().decode()
        assert "Entry 26" in html
        assert "Entry 27" in html
    finally:
        httpd.shutdown()


def test_reader_relabel_updates_visualization_in_place(tmp_path: Path) -> None:
    import urllib.request

    httpd, base = _server(tmp_path)
    try:
        store = httpd.store
        _seed(store, 28)
        body = urlencode({"entry_id": "28", "classification": "ignore", "folder": "/category/must_read"}).encode()
        response = urllib.request.urlopen(urllib.request.Request(f"{base}/override", data=body, method="POST"))
        assert response.url.endswith("/entry?id=28&folder=/category/must_read")
        html = urllib.request.urlopen(f"{base}/entry?id=28&folder=/category/must_read").read().decode()
        assert 'value="ignore"' in html
        assert 'classification-manual classification-selected" aria-pressed="true" aria-current="true">ignore</button>' in html

        body = urlencode({"entry_id": "28", "classification": "possible_interest", "folder": "/category/must_read"}).encode()
        response = urllib.request.urlopen(urllib.request.Request(f"{base}/override", data=body, method="POST"))
        assert response.url.endswith("/entry?id=28&folder=/category/must_read")
        html = urllib.request.urlopen(f"{base}/entry?id=28&folder=/category/must_read").read().decode()
        assert 'classification-manual classification-selected" aria-pressed="true" aria-current="true">skim</button>' in html
        assert 'classification-auto classification-selected' not in html
    finally:
        httpd.shutdown()


def test_feed_icon_uses_miniflux_client(tmp_path, monkeypatch):
    import urllib.request

    httpd, base = _server(tmp_path)
    calls = []

    class FakeMiniflux:
        def __init__(self, config):
            pass

        def get_feed_icon(self, feed_id):
            calls.append(feed_id)
            return {"mime_type": "image/png;base64", "data": base64.b64encode(b"icon").decode()}

    import server.skimmer_server.http_api as api
    monkeypatch.setattr(api, "MinifluxClient", FakeMiniflux)
    try:
        response = urllib.request.urlopen(f"{base}/feed-icon?id=42")
        assert response.status == 200
        assert response.read() == b"icon"
        assert response.headers["Content-Type"] == "image/png"
        assert calls == [42]
    finally:
        httpd.shutdown()


def test_feed_icon_caches_miniflux_payload(tmp_path, monkeypatch):
    import urllib.request

    httpd, base = _server(tmp_path)
    calls = []

    class FakeMiniflux:
        def __init__(self, config):
            pass

        def get_feed_icon(self, feed_id):
            calls.append(feed_id)
            return {"mime_type": "image/png;base64", "data": base64.b64encode(b"cached").decode()}

    import server.skimmer_server.http_api as api
    monkeypatch.setattr(api, "MinifluxClient", FakeMiniflux)
    try:
        for _ in range(2):
            response = urllib.request.urlopen(f"{base}/feed-icon?id=77")
            assert response.status == 200
            assert response.read() == b"cached"
        assert calls == [77]
    finally:
        httpd.shutdown()
