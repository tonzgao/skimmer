from __future__ import annotations

import json
import base64
import re
import signal
import threading
from datetime import datetime, timedelta, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .auth import COOKIE_NAME, check_password, issue_cookie_value, parse_cookies, verify_cookie_value
from .config import Config
from .fixtures import fixture_catalog
from .miniflux import MinifluxClient
from .state import StateStore
from .storage import append_decisions, append_override, archive_override, normalize_confidence, read_latest_decisions, read_overrides
from .sync import BackgroundSync, _read_state_row


CLASSIFICATIONS = ("must_read", "possible_interest", "ignore")
CLASSIFICATION_LABELS = {
    "must_read": "important",
    "possible_interest": "skim",
    "ignore": "ignore",
}

# Paths reachable without a valid session when a password is configured.
PUBLIC_PATHS = {"/login", "/logout", "/stylesheets/skimmer.css", "/favicon.svg", "/health"}
PUBLIC_PATH_PREFIXES = {"/feed-icon"}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        config = self.server.config
        store = self.server.store
        sync = self.server.sync
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if not self._authorized(config):
                self._redirect(f"/login?next={parsed.path}" if parsed.path != "/" else "/login")
                return

            if parsed.path == "/login":
                self._html(_login_html(query.get("error", [None])[0], query.get("next", [None])[0]))
                return

            if parsed.path == "/logout":
                self._set_cookie(f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
                self._redirect("/login")
                return

            if parsed.path in {"", "/"}:
                decisions = [row for row in store.unread(limit=None) if not row.get("manual")]
                self._html(_uncategorized_html(decisions, config.write_back, store=store))
                return

            if parsed.path == "/history":
                self._html(_history_html(_history_rows(store), next_url="/history", store=store))
                return

            if parsed.path == "/categories":
                categories, _ = _catalog(config, sync)
                self._html(_categories_html(categories))
                return

            if parsed.path == "/feeds":
                _, feeds = _catalog(config, sync)
                self._html(_feeds_html(feeds))
                return

            if parsed.path.startswith("/category/"):
                classification = parsed.path.removeprefix("/category/")
                if classification not in CLASSIFICATIONS:
                    raise ValueError("Unknown review category")
                decisions = store.by_category(classification)
                title = classification.replace("_", " ").title()
                next_url = f"/category/{classification}"
                self._html(_entry_list_html(decisions, {}, title, archive=True, classify=True, bulk_next=next_url, next_url=next_url, store=store, config=config))
                return

            if parsed.path == "/category":
                raw_id = query.get("id", [None])[0]
                if raw_id in CLASSIFICATIONS:
                    decisions = store.by_category(raw_id)
                    title = raw_id.replace("_", " ").title()
                    next_url = f"/category?id={raw_id}"
                    self._html(_entry_list_html(decisions, {}, title, archive=True, classify=True, bulk_next=next_url, next_url=next_url, store=store, config=config))
                    return
                # Numeric id: a Miniflux feed category, not a review category.
                category_id = int(raw_id or "0")
                _, feeds = _catalog(config, sync)
                catalog_categories = sync.catalog().get("categories", []) if not getattr(config, "_fixture", False) else []
                title = next((row["title"] for row in catalog_categories if row["id"] == category_id), f"Category {category_id}")
                feed_ids = {row["id"] for row in feeds if (row.get("category") or {}).get("id") == category_id}
                entries = [
                    row for row in store.unread(limit=None)
                    if ((row.get("source_entry") or {}).get("feed") or {}).get("id") in feed_ids
                    or (row.get("source_entry") or {}).get("category", {}).get("id") == category_id
                ]
                next_url = f"/category?id={category_id}"
                self._html(_entry_list_html(entries, {}, title, classify=True, bulk_next=next_url, next_url=next_url, store=store, config=config))
                return

            if parsed.path == "/feed":
                feed_id = int(query.get("id", ["0"])[0])
                _, feeds = _catalog(config, sync)
                title = next((row["title"] for row in feeds if row["id"] == feed_id), f"Feed {feed_id}")
                entries = [
                    row for row in store.unread(limit=None)
                    if (row.get("source_entry") or {}).get("feed", {}).get("id") == feed_id
                ]
                next_url = f"/feed?id={feed_id}"
                self._html(_entry_list_html(entries, {}, title, classify=True, bulk_next=next_url, next_url=next_url, store=store, config=config))
                return

            if parsed.path == "/entry" and query.get("id"):
                entry_id = int(query["id"][0])
                folder = _safe_next(query.get("folder", [None])[0]) or _safe_next(query.get("origin", [None])[0]) or _safe_next(query.get("next", [None])[0])
                # Opening an entry marks it READ in Miniflux (like clicking a
                # link there) — but only on a genuine open, never on the
                # reader reload that follows an explicit read/unread toggle.
                current = store.entry(entry_id)
                if query.get("toggled") and current:
                    pass  # toggle reload: keep the user's chosen state
                elif not (current and current.get("miniflux_read")):
                    # Reading an entry finishes it: read = done, one state.
                    now = datetime.now(timezone.utc).isoformat()
                    read_row = normalize_confidence({
                        **(current or _unclassified_decision(config, entry_id)),
                        "observed_at": now,
                        "miniflux_read": True,
                        "status": "read",
                        "done": True,
                        "archived_at": now,
                    })
                    append_decisions(config.history_path, [read_row])
                    store.mark_local_read([read_row])
                    sync.request_status_change(entry_id, "read")
                decision = store.entry(entry_id) or _unclassified_decision(config, entry_id)
                override = read_overrides(config.overrides_path).get(entry_id)
                live_entry = sync.fetch_entry(entry_id)
                neighbors = _entry_neighbors(folder, entry_id, config, store)
                self._html(_reader_html(
                    decision,
                    override,
                    config.write_back,
                    live_entry=live_entry,
                    folder=folder,
                    previous=neighbors[0],
                    following=neighbors[1],
                    store=store,
                ))
                return

            if parsed.path == "/done-category":
                classification = query.get("classification", [None])[0]
                if classification not in CLASSIFICATIONS:
                    raise ValueError("Unknown review category")
                now = datetime.now(timezone.utc).isoformat()
                for row in store.by_category(classification):
                    entry_id = int(row["entry_id"])
                    if row.get("done"):
                        continue
                    done_row = normalize_confidence({
                        **row,
                        "archived_at": now,
                        "observed_at": now,
                        "miniflux_read": True,
                        "done": True,
                        "status": "read",
                    })
                    append_decisions(config.history_path, [done_row])
                    store.mark_local_read([done_row])
                    sync.request_status_change(entry_id, "read")
                self._redirect(f"/category/{classification}")
                return

            if parsed.path == "/favicon.svg":
                favicon_path = Path(__file__).parent / "static" / "favicon.svg"
                data = favicon_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml")
                self.send_header("Cache-Control", "public, max-age=86400")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

            if parsed.path == "/stylesheets/skimmer.css":
                static_dir = Path(__file__).parent / "static"
                css_key = tuple((path.name, path.stat().st_mtime_ns) for path in sorted(static_dir.glob('miniflux-*.css')))
                cached = self.server.static_cache.get(css_key)
                if cached is None:
                    cached = "\n".join(path.read_text(encoding="utf-8") for path in sorted(static_dir.glob('miniflux-*.css')))
                    self.server.static_cache[css_key] = cached
                self._css(
                    cached
                )
                return

            if parsed.path == "/feed-icon":
                feed_id = int(query.get("id", ["0"])[0])
                if getattr(config, "_fixture", False):
                    payload = _fixture_feed_icon(feed_id)
                    self._icon(payload)
                    return
                cache_key = f"feed-icon:{feed_id}"
                icon = self.server.icon_cache.get(cache_key)
                if icon is None:
                    try:
                        icon = MinifluxClient(config).get_feed_icon(feed_id)
                    except Exception:
                        icon = {}
                    self.server.icon_cache[cache_key] = {"payload": icon}
                self._icon(icon)
                return

            if parsed.path == "/health":
                self._json({"ok": True, "mode": "consumption_ui", "write_back_enabled": config.write_back, "sync": sync.status})
                return

            if parsed.path == "/meta":
                self._json({
                    "source_of_truth": "server_worker",
                    "client_role": "consumption_ui",
                    "fixture": bool(getattr(config, "_fixture", False)),
                    "write_back_enabled": config.write_back,
                    "sync": sync.status,
                    "write_back_policy": "batch mark manual/read entries after response",
                })
                return

            if parsed.path == "/decisions":
                self._json({"decisions": store.rows()})
                return

            if parsed.path == "/decisions/latest":
                self._json({"decisions": store.unread(limit=200)})
                return

            if parsed.path == "/overrides":
                self._json({"overrides": list(read_overrides(config.overrides_path).values())})
                return
        except (ValueError, OSError, RuntimeError) as exc:
            self._json({"ok": False, "error": str(exc)}, 500)
            return

        self.send_error(404)

    def do_POST(self) -> None:
        config = self.server.config
        store = self.server.store
        sync = self.server.sync
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        values = parse_qs(self.rfile.read(length).decode("utf-8"))
        next_url = values.get("next", [None])[0]
        try:
            if parsed.path == "/login":
                if not config.auth_password:
                    self._redirect("/")
                    return
                password = (values.get("password", [""])[0] or "").strip()
                next_url = _safe_next(values.get("next", [None])[0])
                if check_password(password, config.auth_password):
                    cookie = f"{COOKIE_NAME}={issue_cookie_value(self.server.secret)}; Path=/; Max-Age={30 * 24 * 3600}; HttpOnly; SameSite=Lax"
                    self._set_cookie(cookie)
                    self._redirect(next_url or "/")
                else:
                    self._redirect(f"/login?error=1&next={next_url}" if next_url else "/login?error=1")
                return

            if not self._authorized(config):
                self._redirect("/login")
                return

            if parsed.path == "/override":
                entry_id = int(values.get("entry_id", ["0"])[0])
                if entry_id <= 0:
                    raise ValueError("Invalid entry")
                classification = values.get("classification", [None])[0]
                reason = values.get("reason", [None])[0]
                folder = _safe_next(values.get("folder", [None])[0]) or _safe_next(values.get("context", [None])[0]) or next_url
                # List pages post a `next` target: stay in the list. Reader
                # posts carry only a folder: return to the reader.
                if _safe_next(next_url):
                    return_to = _safe_next(next_url) or "/"
                else:
                    return_to = f"/entry?id={entry_id}"
                    if folder:
                        return_to += f"&folder={escape(folder, quote=True)}"
                if classification not in CLASSIFICATIONS:
                    raise ValueError("Invalid classification")
                override = read_overrides(config.overrides_path).get(entry_id)
                current = store.entry(entry_id) or _unclassified_decision(config, entry_id)
                append_override(config.overrides_path, entry_id, classification, reason, categorized=True)
                updated = normalize_confidence({
                    **current,
                    "classification": classification,
                    "confidence": 1.0,
                    "reasons": [reason or "manual override"],
                    "override_reason": reason or "manual override",
                    "manual": True,
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                })
                append_decisions(config.history_path, [updated])
                store.mark_local_read([updated])
                self._redirect(return_to)
                return

            if parsed.path == "/archive":
                entry_id = int(values.get("entry_id", ["0"])[0])
                override = read_overrides(config.overrides_path).get(entry_id)
                current = store.entry(entry_id) or _unclassified_decision(config, entry_id)
                if not override or not override.get("classification"):
                    raise ValueError("Entry must be categorized before archiving")
                sync.request_status_change(entry_id, "read")
                archive_override(config.overrides_path, entry_id)
                # Archive is an explicit done+read action.
                read_row = normalize_confidence({**current, **override, "archived_at": datetime.now(timezone.utc).isoformat(), "manual": True, "confidence": 1.0, "miniflux_read": True, "done": True, "status": "read"})
                append_decisions(config.history_path, [read_row])
                store.mark_local_read([read_row])
                self._redirect(values.get("next", ["/"])[0])
                return

            if parsed.path == "/status":
                entry_id = int(values.get("entry_id", ["0"])[0])
                status = values.get("status", ["unread"])[0]
                folder = _safe_next(values.get("folder", [None])[0]) or _safe_next(values.get("context", [None])[0]) or next_url
                if status not in {"read", "unread"}:
                    raise ValueError("Invalid status")
                # List-page posts (history, categories) stay on the list;
                # reader posts reload the article so the toggle text updates.
                if values.get("from", [None])[0] == "reader":
                    # `toggled=1` makes the reload a plain render, so the
                    # open-marks-read rule doesn't undo the toggle.
                    return_to = f"/entry?id={entry_id}&toggled=1"
                    if folder:
                        return_to += f"&folder={escape(folder, quote=True)}"
                elif folder:
                    return_to = folder
                else:
                    return_to = f"/entry?id={entry_id}"
                # Read/unread toggle, mirroring Miniflux: read = done (one
                # handled state); unread reopens the entry, clearing done so
                # it returns to its working list.
                current = store.entry(entry_id) or _unclassified_decision(config, entry_id)
                now = datetime.now(timezone.utc).isoformat()
                updated = normalize_confidence({
                    **current,
                    "observed_at": now,
                    "miniflux_read": status == "read",
                    "status": status,
                    "done": status == "read",
                    **({} if status == "read" else {"archived_at": None}),
                })
                append_decisions(config.history_path, [updated])
                store.mark_local_read([updated])
                sync.request_status_change(entry_id, status)
                self._redirect(return_to)
                return

            if parsed.path == "/done":
                entry_id = int(values.get("entry_id", ["0"])[0])
                current = store.entry(entry_id) or _unclassified_decision(config, entry_id)
                now = datetime.now(timezone.utc).isoformat()
                done_row = normalize_confidence({
                    **current,
                    "archived_at": now,
                    "observed_at": now,
                    "miniflux_read": True,
                    "done": True,
                    "status": "read",
                })
                append_decisions(config.history_path, [done_row])
                store.mark_local_read([done_row])
                sync.request_status_change(entry_id, "read")
                self._redirect(values.get("next", ["/"])[0])
                return

            if parsed.path == "/done-many":
                next_url = values.get("next", ["/"])[0]
                now = datetime.now(timezone.utc).isoformat()
                done_rows = []
                for raw_id in values.get("entry_id", []):
                    entry_id = int(raw_id)
                    current = store.entry(entry_id)
                    if not current or current.get("done"):
                        continue
                    done_row = normalize_confidence({
                        **current,
                        "archived_at": now,
                        "observed_at": now,
                        "miniflux_read": True,
                        "done": True,
                        "status": "read",
                    })
                    append_decisions(config.history_path, [done_row])
                    store.mark_local_read([done_row])
                    sync.request_status_change(entry_id, "read")
                    done_rows.append(done_row)
                self._redirect(next_url)
                return

            if parsed.path == "/fetch":
                result = sync.sync_once()
                next_url = values.get("next", [None])[0] or self.headers.get("Referer") or "/"
                self._redirect(next_url)
                return
        except (ValueError, OSError, RuntimeError) as exc:
            self._json({"ok": False, "error": str(exc)}, 500)
            return

        self.send_error(404)

    def log_message(self, format: str, *args: object) -> None:
        return

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self._emit_pending_cookie()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _set_cookie(self, cookie: str) -> None:
        self._pending_cookie = cookie

    def _emit_pending_cookie(self) -> None:
        cookie = getattr(self, "_pending_cookie", None)
        if cookie:
            self.send_header("Set-Cookie", cookie)

    def _authorized(self, config: Config) -> bool:
        if not config.auth_password:
            return True
        path = urlparse(self.path).path
        if path in PUBLIC_PATHS or any(path.startswith(prefix) for prefix in PUBLIC_PATH_PREFIXES):
            return True
        cookies = parse_cookies(self.headers.get("Cookie"))
        return verify_cookie_value(self.server.secret, cookies.get(COOKIE_NAME))

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, markup: str, status: int = 200) -> None:
        body = markup.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        # State changes must never be served from cache: the reader reload
        # after a read/unread toggle re-requests the same URL.
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _css(self, css: str) -> None:
        body = css.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/css; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _icon(self, payload: dict) -> None:
        if not isinstance(payload, dict):
            payload = {}
        try:
            raw_data = str(payload.get("data", ""))
            data = base64.b64decode(raw_data.partition("base64,")[2] if ";base64," in raw_data else raw_data)
        except (ValueError, TypeError):
            data = b""
        self.send_response(200 if data else 404)
        mimetype = str(payload.get("mimetype") or payload.get("mime_type") or "application/octet-stream")
        self.send_header("Content-Type", mimetype.partition(";base64")[0])
        self.send_header("Cache-Control", "public, max-age=3600")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)


class SkimmerHTTPServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], config: Config) -> None:
        self.config = config
        self.static_cache: dict[tuple, str] = {}
        self.icon_cache = {}
        self.secret = _load_or_create_secret(config)
        super().__init__(address, Handler)


def _load_or_create_secret(config: Config) -> str:
    """Per-installation cookie-signing secret, persisted in the data dir."""
    from .auth import make_secret

    path = config.data_dir / "session-secret"
    try:
        secret = path.read_text(encoding="utf-8").strip()
        if secret:
            return secret
    except OSError:
        pass
    secret = make_secret()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secret + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return secret


def serve(config: Config) -> None:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    httpd = SkimmerHTTPServer((config.host, config.port), config)
    httpd.store = StateStore(config.decisions_path, config.history_path)
    httpd.sync = BackgroundSync(config, httpd.store)
    httpd.sync.start()
    print(f"Serving Skimmer API on http://{config.host}:{config.port}", flush=True)

    def _shutdown(signum, frame) -> None:
        # SIGTERM/SIGINT (docker stop, Ctrl-C): stop the sync worker and the
        # HTTP loop promptly so container shutdown never hangs.
        httpd.sync.stop()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        httpd.sync.stop()


def _parse_dt(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _relative_time(value: object) -> str:
    parsed = _parse_dt(value)
    if parsed is None:
        return ""
    delta = datetime.now(timezone.utc) - parsed
    seconds = int(delta.total_seconds())
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    if days < 30:
        return f"{days} day{'s' if days != 1 else ''} ago"
    return escape(str(value)[:10])


def _reading_time(content: str) -> str | None:
    words = len(_strip_tags(content).split())
    if not words:
        return None
    return f"{max(1, round(words / 265))} min read"


def _strip_tags(markup: str) -> str:
    return re.sub(r"<[^>]+>", " ", markup)


def _classification_label(classification: object) -> str:
    return CLASSIFICATION_LABELS.get(str(classification), str(classification).replace("_", " "))


def _classification_markup(classification: str, *, manual: bool, current: bool = False) -> str:
    """Render a classification label: italic when automatic, bold when manual."""
    style = "bold" if manual else "italic"
    current_attr = ' data-current="true"' if current else ""
    return f'<span class="classification-label classification-{escape(classification)} classification-{style}"{current_attr}>{escape(_classification_label(classification))}</span>'


def _classification_button(value: str, state: dict, entry_id: object, context: str) -> str:
    """Render a label action purely from resolved entry state."""
    selected = value == state["classification"]
    style = "manual" if state["manual"] else "auto"
    pressed = ' aria-pressed="true"' if selected else ""
    current = ' aria-current="true"' if selected else ""
    classes = f"classification-{style} classification-selected" if selected else f"classification-{style}"
    return (
        '<form method="post" action="/override" class="inline-form">'
        f'<input type="hidden" name="entry_id" value="{escape(str(entry_id), quote=True)}">'
        f'<input type="hidden" name="classification" value="{escape(value, quote=True)}">'
        f'<input type="hidden" name="folder" value="{context}">'
        f'<button type="submit" class="page-button {classes}"{pressed}{current}>{_classification_label(value)}</button></form>'
    )


def _unclassified_decision(config: Config, entry_id: int) -> dict:
    return {
        "entry_id": entry_id,
        "classification": "possible_interest",
        "confidence": 0.5,
        "reasons": ["awaiting background classification"],
        "manual": False,
        "status": "unread",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "source_entry": {"id": entry_id},
    }


def _done_sort_key(row: dict) -> str:
    """Order history by when the entry was finished (newest first)."""
    return str(row.get("archived_at") or row.get("observed_at") or "")


def _archived_after(row: dict, cutoff: datetime) -> bool:
    raw_value = row.get("archived_at") or row.get("observed_at")
    if not raw_value:
        return False
    try:
        archived_at = datetime.fromisoformat(str(raw_value).replace("Z", "+00:00"))
    except ValueError:
        return False
    if archived_at.tzinfo is None:
        archived_at = archived_at.replace(tzinfo=timezone.utc)
    return archived_at >= cutoff


def _catalog(config: Config, sync: BackgroundSync) -> tuple[list[dict], list[dict]]:
    if getattr(config, "_fixture", False):
        return fixture_catalog()
    catalog = sync.catalog()
    return (
        sorted(catalog["categories"], key=lambda row: str(row["title"])),
        sorted(catalog["feeds"], key=lambda row: str(row["title"])),
    )


def _page(title: str, page_header: str, content: str, *, store=None) -> str:
    if store is None:
        counts = {key: 0 for key in ("uncategorized", "must_read", "possible_interest", "ignore", "history")}
    else:
        rows = store.rows()
        open_rows = [row for row in rows if row.get("status") != "read"]
        counts = {
            "uncategorized": len([
                row for row in open_rows
                if not row.get("manual")
            ]),
            "must_read": sum(1 for row in open_rows if row.get("classification") == "must_read"),
            "possible_interest": sum(1 for row in open_rows if row.get("classification") == "possible_interest"),
            "ignore": sum(1 for row in open_rows if row.get("classification") == "ignore"),
            # Same definition (and cap) as the History page itself, so the nav
            # number always equals what clicking through will show.
            "history": len(_history_rows(store)),
        }
    nav_items = [
        ("/", "Pending", "entries", "uncategorized"),
        ("/category/must_read", "Important", "entries", "must_read"),
        ("/category/possible_interest", "Skim", "entries", "possible_interest"),
        ("/category/ignore", "Ignore", "entries", "ignore"),
        ("/history", "History", "history", "history"),
    ]
    navigation = "".join(
        f'<li><a href="{href}">{label} <span class="unread-counter-wrapper" aria-hidden="true">(<span class="unread-counter">{counts[key]})</span></span></a></li>'
        for href, label, _, key in nav_items
    )
    reload_script = """
<script>
window.addEventListener('pageshow', (event) => {
  if (event.persisted || performance.getEntriesByType('navigation')[0]?.type === 'back_forward') location.reload();
});
</script>"""
    return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{escape(title)} - Skimmer</title><link rel="icon" type="image/svg+xml" href="/favicon.svg"><link rel="stylesheet" href="/stylesheets/skimmer.css"></head>
<body>
<header class="header"><nav><div class="logo"><form method="post" action="/fetch" class="inline-form"><input type="hidden" name="next" value="/"><button type="submit" class="logo-button">Skim<span>mer</span></button></form></div><ul id="header-menu">{navigation}</ul></nav></header>
<section class="page-header" aria-labelledby="page-header-title">{page_header}</section>
<main id="main">{content}</main>
{reload_script}
</body></html>"""


def _uncategorized_html(decisions: list[dict], write_back_enabled: bool, title: str = "Pending", *, store=None) -> str:
    # Pending = every open entry whose label came from the algorithm, not
    # from the user. Manual overrides leave this page immediately.
    pending = [row for row in decisions if not row.get("manual")]
    items = [_item_markup(row, {}, include_source=True, classify=True, next_url="/") for row in pending]
    content = (
        '<div class="items">'
        + ("".join(items) if items else '<p role="alert" class="alert">No pending entries.</p>')
        + "</div>"
    )
    return _page(title, f'<h1 id="page-header-title">{escape(title)} <span aria-hidden="true" class="unread-counter-wrapper">(<span class="unread-counter">{len(pending)})</span></span></h1>', content, store=store)




HISTORY_LIMIT = 300


def _history_rows(store) -> list[dict]:
    """History page contents: handled entries (done), newest first, capped.

    Reading an entry marks it done, so this is simply everything the user
    has finished.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    rows = [row for row in store.rows() if row.get("done") and _archived_after(row, cutoff)]
    return sorted(rows, key=_done_sort_key, reverse=True)[:HISTORY_LIMIT]


def _folder_rows(context: str | None, config: Config, store: StateStore) -> list[dict]:
    """All rows belonging to the folder identified by ``context``, newest first.

    This is the single definition of "what's in this folder" — the list pages
    render from it and reader pagination walks it, so the two can never
    disagree. It is re-computed on every request (dynamic nexting): whatever
    happened since the list was loaded — relabels, reads, dones — is already
    reflected when the prev/next links are generated.
    """
    if not context:
        return []
    parsed = urlparse(context)
    query = parse_qs(parsed.query)

    if parsed.path in {"", "/"}:
        # Pending: open entries whose label came from the algorithm.
        return sorted(
            (row for row in store.unread(limit=None) if not row.get("manual")),
            key=_published_sort_key,
            reverse=True,
        )

    classification = parsed.path.removeprefix("/category/")
    raw_id = query.get("id", [None])[0]

    if parsed.path == "/history":
        return _history_rows(store)

    review_category = (
        classification
        if parsed.path.startswith("/category/") and classification in CLASSIFICATIONS
        else raw_id
        if parsed.path == "/category" and raw_id in CLASSIFICATIONS
        else None
    )
    if review_category:
        # Working category: UNREAD entries with this label (same definition as
        # StateStore.by_category and the nav counter — all three must agree;
        # read entries leave the working list, done or not).
        rows = [
            row for row in store.rows()
            if row.get("classification") == review_category and row.get("status") != "read"
        ]
        return sorted(rows, key=_published_sort_key, reverse=True)

    if parsed.path == "/category":
        category_id = int(query.get("id", ["0"])[0])
        rows = [
            row for row in store.rows()
            if not row.get("done")
            and (
                ((row.get("source_entry") or {}).get("feed") or {}).get("category", {}).get("id") == category_id
                or (row.get("source_entry") or {}).get("category", {}).get("id") == category_id
            )
        ]
        return sorted(rows, key=_published_sort_key, reverse=True)

    if parsed.path == "/feed":
        feed_id = int(query.get("id", ["0"])[0])
        rows = [
            row for row in store.rows()
            if not row.get("done")
            and (row.get("source_entry") or {}).get("feed", {}).get("id") == feed_id
        ]
        return sorted(rows, key=_published_sort_key, reverse=True)

    return []


def _published_sort_key(row: dict) -> str:
    return str(row.get("published_at") or row.get("observed_at"))


def _entry_neighbors(context: str | None, entry_id: int, config: Config, store: StateStore) -> tuple[dict | None, dict | None]:
    """Closest matching entry on either side of ``entry_id`` in the folder.

    The current article may no longer be a member of the folder (it was just
    read, done, or relabeled). Rather than falling back to some other list,
    we walk the folder's own current membership by published date and pick
    the nearest older entry as Previous and nearest newer as Next — so both
    links always land inside the folder you came from.
    """
    ordered = _folder_rows(context, config, store)
    if not ordered:
        return None, None
    current = store.entry(entry_id)
    current_key = _published_sort_key(current) if current else ""
    if not current_key:
        return None, None

    position = next(
        (index for index, row in enumerate(ordered) if int(row["entry_id"]) == int(entry_id)),
        None,
    )
    if position is not None:
        # Reading order follows the list (newest-first): Next goes to the
        # next entry down (older), Previous goes back up (newer).
        return (
            ordered[position - 1] if position > 0 else None,
            ordered[position + 1] if position + 1 < len(ordered) else None,
        )

    # Current item left the folder: walk its remaining members by date.
    # Previous = nearest newer, Next = nearest older.
    older = [row for row in ordered if _published_sort_key(row) < current_key]
    newer = [row for row in ordered if _published_sort_key(row) > current_key]
    previous = min(newer, key=_published_sort_key) if newer else None
    following = max(older, key=_published_sort_key) if older else None
    return previous, following



def _entry_list_html(
    entries: list[dict],
    overrides: dict,
    title: str,
    *,
    archive: bool = False,
    classify: bool = False,
    bulk_next: str | None = None,
    next_url: str | None = None,
    store=None,
    config=None,
) -> str:
    # Manual/auto styling must consult the live overrides log; decision rows
    # alone can lag behind (e.g. an override written without a new decision).
    if config is not None:
        overrides = read_overrides(config.overrides_path)
    items = [
        _item_markup(row, overrides, include_source=True, archive=archive, classify=classify, next_url=next_url, done_next=next_url)
        for row in entries
    ]
    bulk = ""
    if bulk_next:
        open_rows = [row for row in entries if not row.get("done")]
        hidden = "".join(
            f'<input type="hidden" name="entry_id" value="{row.get("entry_id")}">' for row in open_rows
        )
        bulk = (
            '<div class="pagination-top"><div class="pagination"><div class="pagination-forward">'
            f'<form method="post" action="/done-many" class="inline-form">{hidden}'
            f'<input type="hidden" name="next" value="{escape(bulk_next, quote=True)}">'
            f'<button class="page-button">Mark all done</button></form></div></div></div>'
        )
    content = bulk + '<div class="items">' + ("".join(items) if items else '<p role="alert" class="alert">No entries.</p>') + "</div>"
    return _page(title, f'<h1 id="page-header-title">{escape(title)} <span aria-hidden="true" class="unread-counter-wrapper">(<span class="unread-counter">{len(entries)})</span></span></h1>', content, store=store)


def _item_markup(
    row: dict,
    overrides: dict,
    include_source: bool = False,
    *,
    simple: bool = True,
    archive: bool = False,
    classify: bool = False,
    show_label: bool = True,
    next_url: str | None = None,
    done_next: str | None = None,
) -> str:
    entry_id = row.get("entry_id")
    state = _entry_state(row, overrides.get(entry_id))
    manual = bool(state["manual"])
    classification = str(state["classification"])
    title = str(row.get("title") or "Untitled")
    source = row.get("source_entry") or row.get("_source_entry") or {}
    url = source.get("url") or row.get("url")
    title_markup = escape(title)
    if url:
        folder = next_url or (f"/category/{escape(str(classification), quote=True)}" if archive else None)
        entry_link = f"/entry?id={escape(str(entry_id), quote=True)}"
        if folder:
            entry_link += f"&folder={escape(folder, quote=True)}"
        title_markup = f'<a href="{entry_link}">{title_markup}</a>'
    if include_source:
        entry = source
        feed = entry.get("feed")
        category = entry.get("category")
        feed_title = _nested_title(feed) or str(row.get("feed") or "Unknown feed")
        category_title = _nested_title(category) or str(row.get("category_source") or "")
        feed_id = feed.get("id") if isinstance(feed, dict) else None
        category_id = category.get("id") if isinstance(category, dict) else None
        feed_link = f'<a href="/feed?id={escape(str(feed_id), quote=True)}">{escape(feed_title)}</a>' if feed_id else escape(feed_title)
        category_link = f'<a href="/category?id={category_id}">{escape(category_title)}</a>' if category_id else escape(category_title)
        meta_feed = f'<li>{_feed_icon_markup(feed_id)}{feed_link}</li>'
        if category_title:
            meta_feed += f'<li>{escape(str(category_title))}</li>'
    else:
        category_markup = ""
        meta_feed = '<li><a href="#"><span class="feed-icon"></span>{}</a></li>'.format(escape(str(row.get("feed") or "Unknown feed")))
    published = _relative_time(row.get("published_at") or row.get("observed_at"))
    reading_time = _reading_time(str(source.get("content") or row.get("content") or ""))
    if reading_time:
        published = f"{published} · {reading_time}"
    actions = ""
    if archive or done_next:
        return_after_done = next_url or (f"/category/{classification}" if archive else str(done_next or "/"))
        # List toggle: label reflects the READ flag (Miniflux convention).
        is_read = bool(state["miniflux_read"])
        read_label = "Unread" if is_read else "Read"
        read_status = "unread" if is_read else "read"
        actions = (
            f'<form method="post" action="/status" class="inline-form"><input type="hidden" name="entry_id" value="{entry_id}">'
            f'<input type="hidden" name="status" value="{read_status}"><input type="hidden" name="folder" value="{escape(return_after_done, quote=True)}">'
            f'<button class="page-button">{read_label}</button></form>'
        )
    if classify:
        buttons = []
        for value in CLASSIFICATIONS:
            label = _classification_label(value)
            current = ' aria-current="true"' if value == classification else ""
            override_next = next_url or f"/category/{escape(str(classification), quote=True)}"
            style = "manual" if manual else "auto"
            buttons.append(
                '<form method="post" action="/override" class="inline-form">'
                f'<input type="hidden" name="entry_id" value="{entry_id}">'
                f'<input type="hidden" name="classification" value="{value}">'
                f'<input type="hidden" name="next" value="{override_next}">'
                f'<button type="submit" class="classification-button {style}"{current}>{label}</button></form>'
            )
        actions = f'<div class="classification-actions">{" ".join(buttons)}</div>{actions}'
    if simple:
        meta = f'<ul class="item-meta-info">{meta_feed}<li><time>{escape(published)}</time></li></ul>'
        label = _classification_markup(classification, manual=manual) if show_label and not classify else ""
        return f"""<article class="item entry-item item-status-{escape(classification)}">
<header class="item-header"><h2 id="entry-title-{escape(str(entry_id))}" class="item-title" dir="auto">{title_markup}</h2>{label}</header>
<div class="item-meta"><ul class="item-meta-info">{meta_feed}<li><time>{escape(published)}</time></li></ul><ul class="item-meta-icons">{f'<li>{actions}</li>' if actions else ''}</ul></div>
</article>"""
    label = _classification_markup(classification, manual=manual) if show_label and not classify else ""
    return f"""<article class="item entry-item item-status-{escape(classification)}">
<header class="item-header"><h2 class="item-title">{title_markup}</h2>{label}</header>
<div class="item-meta"><ul class="item-meta-info">{meta_feed}<li><time>{escape(published)}</time></li><li>{float(row.get('confidence') or 0):.0%}</li></ul><ul class="item-meta-icons">{f'<li>{actions}</li>' if actions else ''}</ul></div>
</article>"""


def _history_html(rows: list[dict], *, next_url: str | None = None, store=None) -> str:
    items = [_item_markup(row, {}, include_source=True, archive=True, classify=True, show_label=False, next_url=next_url) for row in rows]
    content = '<div class="items">' + ("".join(items) if items else '<p role="alert" class="alert">No history yet.</p>') + "</div>"
    return _page("History", f'<h1 id="page-header-title">History <span aria-hidden="true" class="unread-counter-wrapper">(<span class="unread-counter">{len(rows)})</span></span></h1>', content, store=store)


def _categories_html(categories: list[dict]) -> str:
    items = []
    for row in categories:
        items.append(f"""<article class="item category-item category-has-unread">
<header class="item-header"><h2 class="item-title"><a href="/category?id={row['id']}">{escape(str(row['title']))} <span class="category-item-total">({row.get('entry_count', 0)})</span></a></h2></header>
<div class="item-meta"><ul class="item-meta-info"><li>{row.get('feed_count', 0)} feeds</li></ul><ul class="item-meta-icons"><li><a href="/category?id={row['id']}"><span class="icon-label">Entries</span></a></li><li><a href="/feeds"><span class="icon-label">Feeds</span></a></li></ul></div>
</article>""")
    content = '<div class="items">' + ("".join(items) if items else '<p role="alert" class="alert">No categories.</p>') + "</div>"
    return _page("Categories", f'<h1 id="page-header-title">Categories <span aria-hidden="true" class="unread-counter-wrapper">(<span class="unread-counter">{len(categories)})</span></span></h1>', content)


def _feeds_html(feeds: list[dict]) -> str:
    items = []
    for row in feeds:
        site_url = row.get("site_url") or "#"
        domain = urlparse(str(site_url)).netloc or site_url
        items.append(f"""<article class="item feed-item feed-has-unread">
<header class="item-header"><h2 class="item-title"><a href="/feed?id={row['id']}">{escape(str(row['title']))} <span class="feed-entries-counter">({row.get('entry_count', 0)})</span></a></h2><span class="category">{escape(str(row.get('category') or ''))}</span></header>
<div class="item-meta"><ul class="item-meta-info"><li><a href="{escape(str(site_url), quote=True)}" rel="noopener">{escape(str(domain))}</a></li></ul><ul class="item-meta-icons"><li><a href="/feed?id={row['id']}"><span class="icon-label">Entries</span></a></li></ul></div>
</article>""")
    content = '<div class="items">' + ("".join(items) if items else '<p role="alert" class="alert">No feeds.</p>') + "</div>"
    return _page("Feeds", f'<h1 id="page-header-title">Feeds <span aria-hidden="true" class="unread-counter-wrapper">(<span class="unread-counter">{len(feeds)})</span></span></h1>', content)


def _fixture_feed_icon(feed_id: int) -> dict:
    if feed_id == 11:
        mimetype = "image/svg+xml"
        svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><rect width="16" height="16" fill="#b3261e"/></svg>'
    elif feed_id == 12:
        mimetype = "image/svg+xml"
        svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><rect width="16" height="16" fill="#1e88e5"/></svg>'
    elif feed_id == 13:
        mimetype = "image/svg+xml"
        svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><rect width="16" height="16" fill="#f9ab00"/></svg>'
    else:
        return {}
    return {"mimetype": mimetype, "data": base64.b64encode(svg.encode()).decode()}


def _reader_html(
    decision: dict,
    override: dict | None,
    write_back_enabled: bool,
    *,
    live_entry: dict | None = None,
    next_url: str | None = None,
    folder: str | None = None,
    previous: dict | None = None,
    following: dict | None = None,
    store=None,
) -> str:
    entry = live_entry or decision.get("source_entry") or decision.get("_source_entry") or {}
    if live_entry:
        entry = {
            **entry,
            "feed": entry.get("feed") or (decision.get("source_entry") or {}).get("feed"),
            "category": entry.get("category") or (decision.get("source_entry") or {}).get("category"),
        }
    content = str(entry.get("content") or decision.get("content") or "<p>No content available.</p>")
    author = entry.get("author") or decision.get("author")
    state = _entry_state(decision, override)
    current = state["classification"]
    manual = state["manual"]
    published = _relative_time(entry.get("published_at") or decision.get("published_at") or decision.get("observed_at"))
    reading_time = _reading_time(content)
    meta_bits = f"<span class=\"entry-date\"><time>{escape(published)}</time></span>"
    if reading_time:
        meta_bits += f" · {escape(reading_time)}"
    folder = _safe_next(folder or next_url or "/")
    context = escape(str(folder or "/"), quote=True)
    buttons = []
    for value in CLASSIFICATIONS:
        buttons.append(_classification_button(value, state, decision["entry_id"], context))
    feed = entry.get("feed") or {}
    category = entry.get("category") or {}
    feed_title = _nested_title(feed) or decision.get("feed") or ""
    category_title = _nested_title(category) or decision.get("category_source") or ""
    feed_id = feed.get("id") if isinstance(feed, dict) else None
    external_url = entry.get("url") or decision.get("url") or "#"
    icon_markup = _feed_icon_markup(feed_id)
    # Reader toggle: same Miniflux convention. Read entries offer "Unread";
    # unread entries offer "Read". Done state never changes the label.
    is_read = bool(state["miniflux_read"])
    read_label = "Unread" if is_read else "Read"
    read_status = "unread" if is_read else "read"
    pagination = _reader_pagination(previous, following, context, "top")
    bottom_pagination = _reader_pagination(previous, following, context, "bottom")
    header = f"""
<section class="entry reader-view" data-folder="{context}">
<header class="entry-header">
<h1><a href="{escape(str(external_url), quote=True)}" rel="noopener">{escape(str(decision.get('title') or 'Untitled'))}</a></h1>
<div class="reader-actions">
<div class="entry-actions"><ul>
<li>{''.join(buttons)}</li>
<li><form method="post" action="/status" class="inline-form"><input type="hidden" name="entry_id" value="{decision['entry_id']}"><input type="hidden" name="status" value="{read_status}"><input type="hidden" name="from" value="reader"><input type="hidden" name="folder" value="{context}"><button type="submit" class="page-button">{read_label}</button></form></li>
<li><a class="page-button" href="{escape(str(external_url), quote=True)}" target="_blank" rel="noopener">External</a></li>
</ul></div>
<div class="entry-meta"><span class="entry-website">{f'<a href="/feed?id={escape(str(feed_id), quote=True)}">' if feed_id else ''}{icon_markup}{escape(str(feed_title))}{'</a>' if feed_id else ''}</span>{f' – <em>{escape(str(author))}</em>' if author else ''}{f' <span class="category">{escape(str(category_title))}</span>' if category_title else ''} {meta_bits}</div>
</header>
</section>
{pagination}
<article class="entry-content">{content}</article>
{bottom_pagination}"""
    return _page(str(decision.get("title") or "Entry"), escape(str(decision.get("title") or "Entry")), header, store=store)


def _reader_pagination(previous: dict | None, following: dict | None, context: str, position: str) -> str:
    if previous is None and following is None:
        return ""
    backward = (
        f'<div class="pagination-backward"><div><a class="pagination-prev" href="/entry?id={escape(str(previous["entry_id"]), quote=True)}'
        f'&amp;folder={context}">Previous</a></div></div>'
        if previous is not None
        else '<div class="pagination-backward"><div></div></div>'
    )
    forward = (
        f'<div class="pagination-forward"><div><a class="pagination-next" href="/entry?id={escape(str(following["entry_id"]), quote=True)}'
        f'&amp;folder={context}">Next</a></div></div>'
        if following is not None
        else '<div class="pagination-forward"><div></div></div>'
    )
    css_class = "pagination-entry-top" if position == "top" else "pagination-entry-bottom"
    return (
        f'<nav class="pagination {css_class}" aria-label="Entry pagination">'
        f'{backward}{forward}</nav>'
    )


def _entry_state(decision: dict, override: dict | None) -> dict:
    """Derive one authoritative display/action state for an entry.

    Read-state model (single source of truth):

    - ``miniflux_read`` is THE read flag. An entry is read iff it has been
      marked read in Miniflux — whether by skimmer actions or directly in
      Miniflux. It is never inferred from anything else.
    - ``done`` is independent: it only means the user finished with the entry
      in skimmer. History shows done entries; read-but-not-done entries stay
      in their working lists.
    - ``manual`` means the user set the classification (bold labels). It can
      only be set through an explicit override, never by sync or reads.
    """
    classification = str((override or {}).get("classification") or decision.get("classification") or "possible_interest")
    manual = bool(override) or bool(decision.get("manual"))
    miniflux_read = bool(decision.get("miniflux_read"))
    status = "read" if miniflux_read else "unread"
    return {
        "classification": classification,
        "manual": manual,
        "done": bool(decision.get("done")),
        "miniflux_read": miniflux_read,
        "status": status,
    }


def _feed_icon_markup(feed_id: object) -> str:
    if not isinstance(feed_id, int):
        return '<span class="feed-icon" aria-hidden="true"></span>'
    return f'<img class="feed-icon" src="/feed-icon?id={feed_id}" alt="" loading="lazy">'


def _nested_title(value: object) -> str | None:
    if isinstance(value, dict):
        title = value.get("title")
        return str(title) if title else None
    return None


def _safe_next(value: str | None) -> str | None:
    """Only allow same-site redirect targets (no open redirect)."""
    if not value or not value.startswith("/") or value.startswith("//"):
        return None
    return value


def _login_html(error: str | None, next_url: str | None) -> str:
    error_markup = '<p role="alert" class="alert">Incorrect password.</p>' if error else ""
    next_markup = f'<input type="hidden" name="next" value="{escape(_safe_next(next_url) or "/", quote=True)}">'
    content = f"""
<section class="entry login-entry">
<header class="entry-header"><h1>Sign in to Skimmer</h1></header>
{error_markup}
<form method="post" action="/login" class="login-form">
{next_markup}
<label for="password">Password</label>
<input type="password" id="password" name="password" required autofocus autocomplete="current-password">
<button type="submit" class="page-button">Sign in</button>
</form>
</section>"""
    return _page("Sign in", "Sign in", content)
