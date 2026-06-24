from __future__ import annotations

import tempfile
import threading
import urllib.parse
import urllib.request
from pathlib import Path

from tests.test_http_actions import _seed
from server.skimmer_server.config import Config
from server.skimmer_server.http_api import SkimmerHTTPServer
from server.skimmer_server.state import StateStore


def _config(tmp_path: Path, password: str | None) -> Config:
    return Config(
        miniflux_url="https://example.invalid", miniflux_username=None, miniflux_password=None,
        miniflux_token=None, fetch_limit=10, data_dir=tmp_path,
        overrides_path=tmp_path / "overrides", history_path=tmp_path / "history",
        write_back=False, host="127.0.0.1", port=0, must_read_keywords=[],
        possible_interest_keywords=[], ignore_keywords=[],
        auth_password=password,
    )


def _server(tmp_path: Path, password: str | None):
    config = _config(Path(tmp_path), password)
    httpd = SkimmerHTTPServer(("127.0.0.1", 0), config)
    httpd.store = StateStore(config.decisions_path, config.history_path)
    from tests.test_http_actions import FakeSync

    httpd.sync = FakeSync()
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _opener():
    return urllib.request.build_opener(NoRedirect)


def test_no_password_means_open_access(tmp_path: Path) -> None:
    httpd, base = _server(tempfile.mkdtemp(), None)
    response = urllib.request.urlopen(f"{base}/")
    assert b"Skim" in response.read()


def test_unauthenticated_requests_redirect_to_login(tmp_path: Path) -> None:
    httpd, base = _server(Path(tempfile.mkdtemp()), "hunter2")
    opener = _opener()
    for path in ("/", "/category/must_read", "/history"):
        try:
            opener.open(f"{base}{path}")
            raised = False
        except urllib.error.HTTPError as exc:
            assert exc.status == 303
            assert exc.headers["Location"].startswith("/login")
            raised = True
        assert raised, path

    # POST actions are blocked too — no anonymous write-backs.
    data = urllib.parse.urlencode({"entry_id": "1", "classification": "ignore"}).encode()
    try:
        opener.open(urllib.request.Request(f"{base}/override", data=data, method="POST"))
        raised = False
    except urllib.error.HTTPError as exc:
        assert exc.status == 303
        assert exc.headers["Location"] == "/login"
        raised = True
    assert raised


def test_wrong_password_shows_error_and_correct_password_logs_in(tmp_path: Path) -> None:
    tmp = Path(tempfile.mkdtemp())
    httpd, base = _server(tmp, "hunter2")
    store = httpd.store
    _seed(store, 10)

    opener = _opener()

    def login(password: str):
        data = urllib.parse.urlencode({"password": password}).encode()
        request = urllib.request.Request(f"{base}/login", data=data, method="POST")
        try:
            response = opener.open(request)
            return response.status, response.headers.get("Set-Cookie", ""), response.headers.get("Location", "")
        except urllib.error.HTTPError as exc:
            return exc.status, exc.headers.get("Set-Cookie", ""), exc.headers.get("Location", "")

    status, _, location = login("wrong")
    assert status == 303 and location.startswith("/login?error=1")

    # Still locked out.
    try:
        opener.open(f"{base}/")
        raised = False
    except urllib.error.HTTPError as exc:
        raised = exc.status == 303
    assert raised

    status, cookie, location = login("hunter2")
    assert status == 303
    assert "skimmer_session=" in cookie and "HttpOnly" in cookie
    session_cookie = cookie.split(";", 1)[0]

    # Authenticated now (category tabs list all entries regardless of confidence).
    page = opener.open(urllib.request.Request(f"{base}/category/must_read", headers={"Cookie": session_cookie})).read().decode()
    assert "Entry 10" in page

    # Logout clears the session (303 expected with the no-redirect opener).
    try:
        opener.open(urllib.request.Request(f"{base}/logout", headers={"Cookie": session_cookie}))
    except urllib.error.HTTPError as exc:
        assert exc.status == 303
    try:
        opener.open(f"{base}/")
        raised = False
    except urllib.error.HTTPError as exc:
        raised = exc.status == 303
    assert raised


def test_health_stays_public_for_monitoring(tmp_path: Path) -> None:
    httpd, base = _server(Path(tempfile.mkdtemp()), "hunter2")
    payload = urllib.request.urlopen(f"{base}/health").read().decode()
    assert '"ok": true' in payload.replace("True", "true")


def test_open_redirect_is_blocked(tmp_path: Path) -> None:
    httpd, base = _server(Path(tempfile.mkdtemp()), "hunter2")
    data = urllib.parse.urlencode({"password": "hunter2", "next": "https://evil.example/"}).encode()
    try:
        _opener().open(urllib.request.Request(f"{base}/login", data=data, method="POST"))
        raised = False
    except urllib.error.HTTPError as exc:
        assert exc.headers["Location"] == "/"
        raised = True
    assert raised
