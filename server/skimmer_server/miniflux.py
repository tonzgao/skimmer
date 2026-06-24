from __future__ import annotations

import miniflux

from .config import Config


class MinifluxClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.client = miniflux.Client(
            base_url=config.miniflux_url,
            username=config.miniflux_username,
            password=config.miniflux_password,
            api_key=config.miniflux_token,
            timeout=30.0,
    )

    def categories(self) -> list[dict]:
        return self.client.get_categories()

    def feeds(self) -> list[dict]:
        return self.client.get_feeds()

    def get_feed_icon(self, feed_id: int) -> dict:
        return self.client.get_feed_icon(int(feed_id))

    def entries(self, **filters: object) -> list[dict]:
        payload = self.client.get_entries(**filters)
        return payload.get("entries", [])

    def entry(self, entry_id: int) -> dict:
        return self.client.get_entry(entry_id)

    def set_status(self, entry_id: int, status: str) -> None:
        self.client.update_entries([int(entry_id)], status)

    def unread_entries(self, limit: int) -> list[dict]:
        payload = self.client.get_entries(
            status="unread",
            order="published_at",
            direction="desc",
            limit=limit,
            offset=0,
        )
        entries = payload.get("entries", [])
        if not isinstance(entries, list):
            raise RuntimeError("Miniflux returned an unexpected entries payload.")
        return entries

    def mark_entries_read(self, entry_ids: list[int]) -> None:
        if not entry_ids:
            return
        self.client.update_entries(entry_ids, "read")

    def mark_entries_unread(self, entry_ids: list[int]) -> None:
        if not entry_ids:
            return
        self.client.update_entries(entry_ids, "unread")

    def read_entries(self, limit: int = 500, changed_after: str | None = None) -> list[dict]:
        payload = self.client.get_entries(
            status="read",
            order="changed_at",
            direction="desc",
            limit=limit,
            offset=0,
            **({"changed_after": changed_after} if changed_after else {}),
        )
        entries = payload.get("entries", [])
        return entries if isinstance(entries, list) else []

    def unread_changes(self, limit: int = 500, changed_after: str | None = None) -> list[dict]:
        payload = self.client.get_entries(
            status="unread",
            order="changed_at",
            direction="desc",
            limit=limit,
            offset=0,
            **({"changed_after": changed_after} if changed_after else {}),
        )
        entries = payload.get("entries", [])
        return entries if isinstance(entries, list) else []
