"""Verify: (1) selected category toggle styling, (2) prev/next orientation,
(3) unread survives folder navigation (sync does not re-mark it read)."""
import tempfile, urllib.request, urllib.parse, re
from pathlib import Path
from tests.test_http_actions import _server, _seed

httpd, base = _server(Path(tempfile.mkdtemp()))
try:
    store = httpd.store
    _seed(store, 500, classification="ignore", published_at="2026-08-20T00:00:00Z")
    _seed(store, 501, classification="ignore", published_at="2026-08-21T00:00:00Z")
    # manual label for 501 to compare styling
    import sys
    sys.path.insert(0, "server")
    from skimmer_server.storage import append_override
    append_override(httpd.config.overrides_path, 501, "ignore", None, categorized=True)

    html = urllib.request.urlopen(f"{base}/category/ignore").read().decode()
    # auto entry (500): its selected button must carry 'auto' class
    m = re.search(r'<button type="submit" class="classification-button (\w+)" aria-current="true">ignore</button>', html)
    assert m, "no selected button found"
    print("selected button classes seen:", sorted(set(re.findall(r'classification-button (auto|manual)" aria-current', html))))
    assert "auto" in set(re.findall(r'classification-button (auto|manual)" aria-current', html))
    assert "manual" in set(re.findall(r'classification-button (auto|manual)" aria-current', html))
    print("auto + manual selected buttons both present: OK")
    # no yellow classification-label spans on list pages
    assert 'classification-label' not in html
    print("no yellow labels on category page: OK")

    # --- prev/next orientation: folders are oldest-first (Miniflux style);
    # reading order follows the list, so Next goes down (newer id 501) and
    # Previous goes up (older). toggled=1 renders without marking entries
    # done, keeping both in the folder.
    html = urllib.request.urlopen(f"{base}/entry?id=500&toggled=1&folder=/category/ignore").read().decode()
    next_link = re.search(r'pagination-next[^>]*href="([^"]+)"', html)
    assert next_link and "id=501" in next_link.group(1), f"Next should go to newer entry 501, got {next_link and next_link.group(1)}"
    assert not re.search(r'pagination-prev[^>]*href=', html), "oldest entry should have no Previous"
    print("Next -> newer (list order), no Previous on oldest: OK")

    html = urllib.request.urlopen(f"{base}/entry?id=501&toggled=1&folder=/category/ignore").read().decode()
    prev_link = re.search(r'class="pagination-prev" href="([^"]+)"', html)
    assert prev_link and "id=500" in prev_link.group(1), f"Previous should be 500, got {prev_link and prev_link.group(1)}"
    assert not re.search(r'class="pagination-next" href=', html)
    print("Previous -> older, no Next on newest: OK")

    # --- unread persistence across navigation
    body = urllib.parse.urlencode({"entry_id": "501", "status": "unread", "folder": "/category/ignore"}).encode()
    urllib.request.urlopen(urllib.request.Request(f"{base}/status", data=body, method="POST"))
    assert store.entry(501)["miniflux_read"] is False
    # navigate around: pending page, other categories, back to ignore
    for path in ("/", "/category/must_read", "/history", "/category/ignore"):
        urllib.request.urlopen(f"{base}{path}").read()
    row = store.entry(501)
    assert row["miniflux_read"] is False, "unread must survive navigation"
    cat_html = urllib.request.urlopen(f"{base}/category/ignore").read().decode()
    assert "Entry 501" in cat_html
    print("unread persists across folder navigation: OK")

    # FakeSync records the miniflux write that real sync would flush:
    body = urllib.parse.urlencode({"entry_id": "501", "status": "read", "from": "reader", "folder": "/category/ignore"}).encode()
    resp = urllib.request.urlopen(urllib.request.Request(f"{base}/status", data=body, method="POST"))
    assert (501, "read") == httpd.sync.status_changes[-1], httpd.sync.status_changes
    body = urllib.parse.urlencode({"entry_id": "501", "status": "unread", "folder": "/category/ignore"}).encode()
    resp = urllib.request.urlopen(urllib.request.Request(f"{base}/status", data=body, method="POST"))
    assert (501, "unread") == httpd.sync.status_changes[-1]
    print("every toggle queues a matching Miniflux status write: OK")
finally:
    httpd.shutdown()
print("ALL OK")
