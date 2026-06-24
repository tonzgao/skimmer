FROM python:3.13-slim

WORKDIR /app

# Only runtime dependency is the Miniflux client (plus its HTTP layer).
COPY pyproject.toml ./
RUN pip install --no-cache-dir "miniflux>=1.1.6"

COPY server/skimmer_server ./server/skimmer_server
COPY shared ./shared
COPY client ./client

ENV SKIMMER_HOST=0.0.0.0 \
    SKIMMER_PORT=8765 \
    SKIMMER_DATA_DIR=/data \
    PYTHONUNBUFFERED=1

VOLUME ["/data"]
EXPOSE 8765

WORKDIR /app/server
CMD ["python", "-m", "skimmer_server", "serve"]
