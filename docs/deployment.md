# Deployment Notes

## Docker (recommended for a VPS)

Build and run alongside Miniflux on the same server:

```sh
cp .env.example .env   # fill in MINIFLUX_URL and MINIFLUX_TOKEN
docker compose up -d --build
```

All configuration is via environment variables — nothing is hardcoded in the
image. The container binds to `0.0.0.0:8765` inside its network; compose
publishes it on `127.0.0.1:8765` on the host so only nginx (or an SSH tunnel)
can reach it from outside. Decisions persist in `./data` on the host.

If Miniflux runs as another container on the same host, point
`MINIFLUX_URL` at `http://miniflux:8080` (service name on a shared Docker
network) instead of the public URL.

## Server Worker (bare metal)

Clone the repository on the Miniflux server and configure `server/.env`.

The browser UI uses CSS files copied from the official
[Miniflux v2](https://github.com/miniflux/v2) repository. It intentionally uses
the same item layout and system theme variables, while showing Skimmer's review
decisions rather than proxying or replacing the live Miniflux UI.

For a no-network interface test, use fixture mode:

```sh
cd server
uv run python -m skimmer_server run-once --fixture
uv run python -m skimmer_server show-decisions
```

```sh
cd server
cp .env.example .env
uv run python -m skimmer_server run-once
```

The worker writes decisions to `server/data/decisions.jsonl` by default.

## Cron

Run every 30 minutes:

```cron
*/30 * * * * cd /path/to/skimmer/server && python -m skimmer_server run-once >> /path/to/skimmer/server/data/cron.log 2>&1
```

Change `*/30` to whatever interval you prefer.

## systemd Timer

Use cron first unless you specifically want systemd logging and service controls.

## Optional API

The server includes a small read-only API for the local client:

```sh
cd server
uv run python -m skimmer_server serve
```

Endpoints:

- `GET /` — small local decision browser;
- `GET /health`
- `GET /meta`
- `GET /decisions`

Bind to `127.0.0.1` by default. If exposing through nginx, keep it private or add authentication at nginx.

The API is intended for a local consumption UI or CLI. Classification and any
Miniflux writeback stay on the server worker.

## Optional Miniflux Writeback

Leave writeback disabled while tuning rules:

```sh
SKIMMER_WRITE_BACK=false
```

When enabled, the worker marks only `ignore` entries as read in Miniflux:

```sh
SKIMMER_WRITE_BACK=true
```

`must_read` and `possible_interest` entries remain unread for consumption in
Skimmer or Miniflux.

## Local Client

From your local machine:

```sh
cd client
PYTHONPATH=. uv run --project ../server python -m skimmer_client list \
  --server https://your-nginx-host/skimmer
```

If using SSH forwarding instead of nginx:

```sh
ssh -L 8765:127.0.0.1:8765 your-server
cd client
PYTHONPATH=. uv run --project ../server python -m skimmer_client list \
  --server http://127.0.0.1:8765
```
