#!/usr/bin/env bash
# Update skimmer on the deployment server: pull, rebuild, restart.
# Works with docker or podman (whichever is installed).
set -euo pipefail

cd "$(dirname "$0")"

# Pick a container CLI: real docker, else podman (which emulates the docker CLI).
if command -v docker >/dev/null 2>&1 && ! grep -qs "Emulate Docker CLI" <(docker ps 2>&1); then
  CLI=docker
else
  CLI=podman
fi

echo "==> Using $CLI"

echo "==> Pulling latest code"
git pull --ff-only

echo "==> Compacting data (idempotent; drops superseded rows and stored article content)"
if command -v uv >/dev/null 2>&1; then
  (cd server && uv run python -m skimmer_server compact) || echo "compact skipped"
fi

echo "==> Rebuilding image"
$CLI build -t skimmer .

echo "==> Restarting container"
if $CLI compose version >/dev/null 2>&1; then
  $CLI compose up -d --force-recreate --build
elif [ "$($CLI ps -q --filter name=skimmer)" ]; then
  $CLI rm -f skimmer
fi
if ! $CLI compose version >/dev/null 2>&1; then
  $CLI run -d --name skimmer --network host \
    -v "${SKIMMER_DATA_VOLUME:-/root/skimmer-data}:/data" \
    --env-file .env \
    skimmer
fi

echo "==> Health check"
for i in $(seq 1 15); do
  if curl -fsS http://127.0.0.1:8765/health >/dev/null 2>&1; then
    echo "skimmer is up: http://127.0.0.1:8765"
    exit 0
  fi
  sleep 2
done

echo "WARNING: health check did not pass; recent logs:" >&2
$CLI logs --tail 30 skimmer >&2 || true
exit 1
