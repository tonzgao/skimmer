from __future__ import annotations

import argparse
import json
from pathlib import Path
from tempfile import gettempdir

from .config import Config
from .http_api import serve
from .state import StateStore
from .sync import BackgroundSync
from .worker import run_once


def main() -> int:
    parser = argparse.ArgumentParser(prog="skimmer-server")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run-once", help="Fetch unread entries and classify them.")
    run_parser.add_argument("--limit", type=int, default=None)
    run_parser.add_argument("--fixture", action="store_true", help="Classify built-in sample entries without calling Miniflux.")

    serve_parser = subparsers.add_parser("serve", help="Serve the read-only local API.")
    serve_parser.add_argument("--fixture", action="store_true", help="Serve decisions from a temporary sample run.")

    show_parser = subparsers.add_parser("show-decisions", help="Print recent local decisions.")
    show_parser.add_argument("--limit", type=int, default=25)
    show_parser.add_argument("--fixture", action="store_true", help="Show the latest temporary fixture decisions.")

    compact_parser = subparsers.add_parser("compact", help="Rewrite decisions.jsonl keeping only the latest row per entry and stripping stored article content.")

    train_parser = subparsers.add_parser("train-model", help="Retrain the classifier from manual labels for cron use.")

    args = parser.parse_args()
    config = Config.load()

    if getattr(args, "fixture", False):
        config = _fixture_config(config, _fixture_data_dir())
    if args.command == "run-once":
        summary = run_once(config, limit=args.limit, fixture=args.fixture)
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0

    if args.command == "serve":
        if args.fixture:
            config.decisions_path.unlink(missing_ok=True)
        if args.fixture:
            run_once(config, fixture=True)
        serve(config)
        return 0

    if args.command == "show-decisions":
        if args.fixture:
            config = _fixture_config(config, _fixture_data_dir())
        path = config.decisions_path
        for row in _tail_jsonl(path, args.limit):
            print(json.dumps(row, sort_keys=True))
        return 0

    if args.command == "compact":
        size_before = config.decisions_path.stat().st_size if config.decisions_path.exists() else 0
        history_before = config.history_path.stat().st_size if config.history_path.exists() else 0
        store = StateStore(config.decisions_path, config.history_path)
        stats = store.compact()
        size_after = config.decisions_path.stat().st_size
        history_after = config.history_path.stat().st_size
        print(json.dumps({
            **stats,
            "decisions_size_mb": round(size_after / 1024 / 1024, 2),
            "history_size_mb": round(history_after / 1024 / 1024, 2),
            "freed_mb": round((size_before + history_before - size_after - history_after) / 1024 / 1024, 2),
        }, indent=2))
        return 0

    if args.command == "train-model":
        store = StateStore(config.decisions_path, config.history_path)
        sync = BackgroundSync(config, store)
        used = sync.train_model()
        print(json.dumps({"trained_examples": used, "model_path": str(config.data_dir / "model.json")}, indent=2))
        return 0

    return 1


def _fixture_data_dir() -> Path:
    return Path(gettempdir()) / "skimmer-fixture"


def _fixture_config(config: Config, data_dir: Path) -> Config:
    return Config(
        miniflux_url=config.miniflux_url,
        miniflux_username=config.miniflux_username,
        miniflux_password=config.miniflux_password,
        miniflux_token=config.miniflux_token,
        fetch_limit=config.fetch_limit,
        data_dir=data_dir,
        overrides_path=data_dir / "overrides.jsonl",
        history_path=data_dir / "history.jsonl",
        write_back=False,
        host=config.host,
        port=config.port,
        must_read_keywords=config.must_read_keywords,
        possible_interest_keywords=config.possible_interest_keywords,
        ignore_keywords=config.ignore_keywords,
        auth_password=config.auth_password,
        _fixture=True,
    )


def _tail_jsonl(path: Path, limit: int) -> list[dict]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    rows = []
    for line in lines:
        if line.strip():
            rows.append(json.loads(line))
    return rows
