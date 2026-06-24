from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


SERVER_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_ROOT.parent


@dataclass(frozen=True)
class Config:
    miniflux_url: str
    miniflux_username: str | None
    miniflux_password: str | None
    miniflux_token: str | None
    fetch_limit: int
    data_dir: Path
    overrides_path: Path
    history_path: Path
    write_back: bool
    host: str
    port: int
    must_read_keywords: list[str]
    possible_interest_keywords: list[str]
    ignore_keywords: list[str]
    auth_password: str | None = None
    _fixture: bool = False

    @property
    def decisions_path(self) -> Path:
        return self.data_dir / "decisions.jsonl"

    @classmethod
    def load(cls) -> "Config":
        values = {}
        values.update(_read_env_file(SERVER_ROOT / ".env"))
        values.update(_read_env_file(REPO_ROOT / ".env"))
        values.update(os.environ)

        miniflux_url = _first(values, "MINIFLUX_URL", "URL")
        if not miniflux_url:
            raise ValueError("Set MINIFLUX_URL or URL.")

        username = _first(values, "MINIFLUX_USERNAME", "USERNAME")
        password = _first(values, "MINIFLUX_PASSWORD", "PASSWORD")
        token = _first(values, "MINIFLUX_TOKEN", "TOKEN")

        if not token and not (username and password):
            raise ValueError("Set MINIFLUX_TOKEN or username/password credentials.")

        data_dir = Path(values.get("SKIMMER_DATA_DIR", str(SERVER_ROOT / "data"))).expanduser()
        if not data_dir.is_absolute():
            data_dir = SERVER_ROOT / data_dir
        overrides_path = Path(values.get("SKIMMER_OVERRIDES_PATH", str(data_dir / "overrides.jsonl"))).expanduser()
        if not overrides_path.is_absolute():
            overrides_path = SERVER_ROOT / overrides_path
        history_path = data_dir / "history.jsonl"

        return cls(
            miniflux_url=miniflux_url.rstrip("/"),
            miniflux_username=username,
            miniflux_password=password,
            miniflux_token=token,
            fetch_limit=_int(values.get("SKIMMER_FETCH_LIMIT"), 100),
            data_dir=data_dir,
            overrides_path=overrides_path,
            history_path=history_path,
            write_back=_bool(values.get("SKIMMER_WRITE_BACK"), False),
            host=values.get("SKIMMER_HOST", "127.0.0.1"),
            port=_int(values.get("SKIMMER_PORT"), 8765),
            must_read_keywords=_csv(values.get("SKIMMER_MUST_READ_KEYWORDS")),
            possible_interest_keywords=_csv(values.get("SKIMMER_POSSIBLE_INTEREST_KEYWORDS")),
            ignore_keywords=_csv(values.get("SKIMMER_IGNORE_KEYWORDS")),
            auth_password=values.get("SKIMMER_PASSWORD") or None,
        )


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _first(values: dict[str, str], *keys: str) -> str | None:
    for key in keys:
        value = values.get(key)
        if value:
            return value
    return None


def _int(value: str | None, default: int) -> int:
    if not value:
        return default
    return int(value)


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]
