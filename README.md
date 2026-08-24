# Skimmer

Skimmer is a standalone companion for a large Miniflux installation. It fetches unread articles, classifies them cheaply and locally, learns from your feedback, and presents review queues:

- `must_read`
- `possible_interest`
- `ignore`

The server is the source of truth for decisions. The local client only inspects server output.

## Highlights

- **Review UI:** Miniflux-style uncategorized, category, feed, entry, and history views with manual labels, Done, bulk Done, and Fetch now.
- **Feedback learning:** Manual labels and implicit done/read behavior train a small softmax classifier. Keyword rules remain hard overrides.
- **Background sync:** The HTTP service reconciles Miniflux state, discovers new entries, rescores open items, refreshes catalogs, and flushes queued writebacks every 5 minutes. Pending Skimmer clicks are flushed to Miniflux before each reconciliation pass, so Miniflux is never treated as authoritative over newer local actions.
- **Single read state:** Read = done. Opening an article marks it read in Miniflux and finishes it in Skimmer; marking it unread anywhere reopens it back onto its category list. Category pages, reader pagination, and the nav counters all share this one definition, so counts always match what a page shows.
- **Safe defaults:** No writeback occurs unless explicitly enabled. With writeback enabled, automatic `ignore` decisions are marked read; other categories remain unread.
- **Optional authentication:** A single UI password protects browser and API routes.
- **AI-written signal:** Common AI-writing phrases are recorded as a decision signal without sending content to any LLM.

## Layout

```text
server/   worker, Miniflux API access, learning, storage, HTTP API/UI
client/   local inspection CLI
shared/   shared schemas/contracts
docs/     deployment notes
```

## Local Development

Run against Miniflux using settings from `server/.env`, the repository `.env`, or your shell environment:

```sh
cd server
cp ../.env.example .env
uv run python -m skimmer_server run-once --limit 25
uv run python -m skimmer_server serve
```

Open <http://127.0.0.1:8765>. If `SKIMMER_PASSWORD` is set, sign in first.

For fixture mode without contacting Miniflux:

```sh
cd server
uv run python -m skimmer_server run-once --fixture
uv run python -m skimmer_server serve --fixture
```

Inspect results from another terminal:

```sh
PYTHONPATH=./client uv run --project ./server python -m skimmer_client list \
  --server http://127.0.0.1:8765
```

The client expects a reachable Skimmer server; it does not classify articles.

## Configuration

Configuration is read in this order: `server/.env`, repository `.env`, then process environment. Later values win.

### Miniflux connection

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `MINIFLUX_URL` | Yes | none | Miniflux base URL. Legacy alias: `URL`. |
| `MINIFLUX_TOKEN` | Yes* | none | Miniflux API token. Legacy alias: `TOKEN`. |
| `MINIFLUX_USERNAME` | Yes* | none | Username alternative to a token. Legacy alias: `USERNAME`. |
| `MINIFLUX_PASSWORD` | Yes* | none | Password used with username auth. Legacy alias: `PASSWORD`. |

\*A token is required unless both username and password are supplied.

Do not confuse `MINIFLUX_PASSWORD` (Miniflux login) with `SKIMMER_PASSWORD` (Skimmer UI login).

### Worker, storage, and UI

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `SKIMMER_FETCH_LIMIT` | No | `100` | Maximum unread entries fetched per sync/run. |
| `SKIMMER_DATA_DIR` | No | `server/data`; `/data` in Docker | Directory for decisions, overrides, feedback history, model, checkpoints, and session secret. Relative paths resolve under `server/`. |
| `SKIMMER_OVERRIDES_PATH` | No | `<data dir>/overrides.jsonl` | Override log path. Relative paths resolve under `server/`. |
| `SKIMMER_HOST` | No | `127.0.0.1`; `0.0.0.0` in Docker | HTTP bind address. |
| `SKIMMER_PORT` | No | `8765` | HTTP port. |
| `SKIMMER_PASSWORD` | No | unset | Enables signed-cookie password protection when nonempty. |

### Classification and writeback

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `SKIMMER_MUST_READ_KEYWORDS` | No | empty | Comma-separated case-insensitive substring overrides. |
| `SKIMMER_POSSIBLE_INTEREST_KEYWORDS` | No | empty | Comma-separated possible-interest overrides. |
| `SKIMMER_IGNORE_KEYWORDS` | No | empty | Comma-separated ignore overrides. Must-read keywords win if multiple lists match. |
| `SKIMMER_WRITE_BACK` | No | `false` | Enable Miniflux writeback. Accepts `true`, `1`, `yes`, or `on`. |

Keyword matching uses title, URL, author, feed name, and source category. It does not scan article body text.

The background interval is currently built into the HTTP service at five minutes; it has no environment variable. Use the UI's Fetch now action or restart the service for an immediate cycle.

### Data files

By default Skimmer creates:

```text
decisions.jsonl        latest classification per entry plus append history
history.jsonl          interactions and training feedback
overrides.jsonl        durable manual label overrides
model.json             trained classifier weights
sync-checkpoint.json   Miniflux reconciliation checkpoint
session-secret         HMAC key for session cookies (created when serving)
```

Persist this directory in production.

## Docker

Configure the root `.env`, then build and start the persistent HTTP worker:

```sh
cp .env.example .env
dock compose up -d --build
```

Use `docker compose` instead if that is the command installed on your machine.

### Plain `docker run` (no compose)

If you'd rather skip Compose entirely — for example next to a Miniflux that runs directly on the host:

```sh
docker run -d --name skimmer --network host \
  -v /root/skimmer-data:/data \
  --env-file .env \
  skimmer
```

Notes:

- `--network host` puts the container on the host network, so `MINIFLUX_URL=http://localhost:8080` works when Miniflux runs on the same machine (and Skimmer binds to `SKIMMER_HOST`, which should be `127.0.0.1` here to stay off the public interface).
- Without `--network host`, publish the port instead: `-p 127.0.0.1:8765:8765`, set `SKIMMER_HOST=0.0.0.0`, and use the host gateway or Miniflux's container address as `MINIFLUX_URL`.
- The container shuts down promptly on `docker stop`; no force-remove needed.
- If your Docker command is actually podman emulating it, prefer real `podman` subcommands for lifecycle operations (`podman stop`, `podman rm`) and avoid `container prune`, which can sweep containers you meant to keep.

Use `docker compose` instead of `docker build`/`run` if that is what's installed; both work identically here.

`compose.yaml` builds the local image, applies `restart: unless-stopped`, loads `.env`, and publishes only to localhost:

```yaml
ports:
  - "127.0.0.1:8765:8765"
volumes:
  - ./data:/data
```

Inside the container, `SKIMMER_HOST=0.0.0.0` and `SKIMMER_DATA_DIR=/data` are baked into the image. Keep that bind address unchanged; restrict exposure through Compose's host publish rule, nginx, or SSH.

Compose explicitly forwards:

```text
MINIFLUX_URL
MINIFLUX_TOKEN
MINIFLUX_USERNAME
MINIFLUX_PASSWORD
SKIMMER_FETCH_LIMIT
SKIMMER_WRITE_BACK
```

It also loads the complete optional root `.env`, so other supported variables—including `SKIMMER_PASSWORD`, keyword rules, data paths, bind host, and port—are available without editing `compose.yaml`.

If Miniflux is another container on the same Docker network, use its service DNS name:

```sh
MINIFLUX_URL=http://miniflux:8080
```

Operate the deployment with:

```sh
dock compose logs -f skimmer
dock compose up -d --force-recreate --build
dock compose down
```

### Updating

To pull the latest code and redeploy in one step:

```sh
./scripts_update.sh
```

The script auto-detects docker vs podman, pulls the repo, compacts the data (drops superseded decision rows and stored article content), rebuilds the image, recreates the container, and waits for `/health` to answer — printing recent logs if startup fails. It works identically for compose and plain `docker run` setups.

Data storage is deliberately lean: decision rows hold only classification metadata, not article bodies (the reader fetches content live from Miniflux). Run `uv run python -m skimmer_server compact` from `server/` occasionally to rewrite the log keeping one row per entry; it is safe to run any time the worker is stopped.

## Scheduling

Docker Compose runs the continuous HTTP worker and its background sync. For bare-metal one-shot usage, cron remains available:

```cron
*/30 * * * * cd /path/to/skimmer/server && python -m skimmer_server run-once >> /path/to/skimmer/server/data/cron.log 2>&1
```

Prefer the HTTP service for interactive review because it also reconciles Miniflux state and handles batched writes. See `docs/deployment.md` for reverse-proxy notes.

## Decisions and Writeback

Automatic classifications are stored locally. With `SKIMMER_WRITE_BACK=true`, automatically ignored entries are marked read in Miniflux; automatic `must_read` and `possible_interest` decisions are not written back.

In the UI:

- Setting or changing a label records it locally and keeps the item open; it does not itself send a read action to Miniflux.
- Opening an entry marks it read in Miniflux and done in Skimmer (read = done), moving it from its category to History.
- Marking an entry unread — in Skimmer or Miniflux — reopens it: the read and done flags clear and it returns to its category.
- Done, bulk Done, and archive actions record completion and queue a Miniflux read update.
- Reconciliation treats Miniflux as authoritative only after queued local clicks have been flushed; entries with pending writes are skipped so a fresh click is never reverted.

Manual labels and completion signals feed future classification.

## Current Scope

Implemented:

- Miniflux fetching and catalog browsing;
- local keyword rules and feedback-trained sparse logistic regression;
- JSON Lines decision storage and persisted model weights;
- authenticated browser UI with favicon, and read-only consumption endpoints;
- periodic reconciliation with flush-before-reconcile ordering and opt-in ignore writeback;
- AI-written phrase signal detection.

Not implemented yet:

- LLM escalation;
- direct external feedback integration;
- prompt reconstruction;
- discussion prompt generation.
