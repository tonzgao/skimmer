"""One-time migration: read entries that predate the read=done model.

Under the old schema an entry could be miniflux_read=True with done unset or
False. Under the new single-state model, handled = done. This appends one
corrected row per such entry.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1] if len(sys.argv) > 1 else "server/data/decisions.jsonl")

latest: dict[int, dict] = {}
for line in path.read_text(encoding="utf-8").splitlines():
    if not line.strip():
        continue
    row = json.loads(line)
    latest[int(row["entry_id"])] = row

now = datetime.now(timezone.utc).isoformat()
fixes = []
for entry_id, row in sorted(latest.items()):
    if row.get("miniflux_read") and not row.get("done"):
        fixed = dict(row)
        fixed["done"] = True
        fixed.setdefault("archived_at", now)
        fixed["observed_at"] = now
        fixes.append(fixed)

print(f"{len(fixes)} read-but-not-done entries to finish")
with path.open("a", encoding="utf-8") as fh:
    for row in fixes:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
print("appended")
