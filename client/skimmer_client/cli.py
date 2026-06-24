from __future__ import annotations

import argparse
import json
from urllib.request import urlopen


def main() -> int:
    parser = argparse.ArgumentParser(prog="skimmer-client")
    parser.add_argument("--server", default="http://127.0.0.1:8765")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("health", help="Check server health.")
    subparsers.add_parser("meta", help="Show server mode and writeback policy.")

    list_parser = subparsers.add_parser("list", help="List recent decisions.")
    list_parser.add_argument("--category", choices=["must_read", "possible_interest", "ignore"])
    list_parser.add_argument("--limit", type=int, default=50)
    list_parser.add_argument("--json", action="store_true")

    args = parser.parse_args()
    server = args.server.rstrip("/")

    if args.command == "health":
        payload = _get_json(f"{server}/health")
        print(json.dumps(payload, sort_keys=True))
        return 0

    if args.command == "meta":
        payload = _get_json(f"{server}/meta")
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if args.command == "list":
        payload = _get_json(f"{server}/decisions")
        decisions = payload.get("decisions", [])
        if args.category:
            decisions = [row for row in decisions if row.get("classification") == args.category]
        decisions = decisions[-args.limit :]
        if args.json:
            print(json.dumps(decisions, indent=2, sort_keys=True))
        else:
            _print_table(decisions)
        return 0

    return 1


def _get_json(url: str) -> dict:
    with urlopen(url, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _print_table(decisions: list[dict]) -> None:
    for row in decisions:
        category = str(row.get("classification", ""))
        confidence = row.get("confidence", 0)
        title = str(row.get("title") or "").replace("\n", " ")
        feed = str(row.get("feed") or "")
        print(f"{category:18} {confidence:.2f}  {feed[:24]:24}  {title[:100]}")
