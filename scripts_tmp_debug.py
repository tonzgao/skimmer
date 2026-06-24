"""Trace: why is the override not making manual=True on the category page?"""
import tempfile, urllib.request, re, sys
from pathlib import Path
sys.path.insert(0, "server")
from tests.test_http_actions import _server, _seed
from skimmer_server.http_api import _item_markup, _entry_state
from skimmer_server.storage import append_override, read_overrides

httpd, base = _server(Path(tempfile.mkdtemp()))
try:
    _seed(store := httpd.store, 500, classification="ignore")
    append_override(httpd.config.overrides_path, 500, "ignore", None, categorized=True)
    ov = read_overrides(httpd.config.overrides_path)
    print("overrides:", ov)
    row = store.entry(500)
    print("row manual:", row.get("manual"))
    print("state:", _entry_state(row, ov.get(500)))
finally:
    httpd.shutdown()
