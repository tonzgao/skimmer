# Skimmer Agent Guidance

## Chosen Approach

Skimmer should be built as a standalone tool with two clear operating modes:

1. A server-side worker that runs on the same machine as Miniflux.
2. A local client that can inspect the worker's output from another machine.

This combines the useful parts of the original options:

- It keeps the implementation independent from Miniflux, avoiding the maintenance cost of a fork.
- It can run periodically throughout the day at intervals selected by the user.
- It can be cloned and run directly on the server without confusing local client concerns with server operations.
- It keeps local debugging possible, because the same worker can run manually against Miniflux from a terminal.

Do not implement Skimmer as a Miniflux fork unless the API becomes a proven blocker.

## Operating Constraints

- The Miniflux instance has around 1500 feed subscriptions.
- AI usage is limited, so Skimmer must not send every article to an LLM.
- Classify cheaply first using feed metadata, title, URL, author, category, reading history, and explicit rules.
- Spend LLM calls only on uncertain or high-value articles after cheap filtering.
- Default behavior must avoid destructive write-back to Miniflux.
- The first useful version should focus on:
  - separating unread articles into `must_read`, `possible_interest`, and `ignore`;
  - recording enough decision data to improve future classification.

## Architecture

- `server/` contains the worker, Miniflux API client, decision storage, and optional read-only HTTP API.
- `client/` contains local tools that query the server API or read exported decision files.
- `shared/` contains contracts or schemas used by both sides.
- `docs/` contains setup notes for deployment, cron/systemd, and nginx.

The server worker is the source of truth for classification. The client should not classify articles itself.

## Feedback Model

User behavior should eventually influence classification:

- immediately marking an item as read suggests `ignore`;
- opening and later dismissing an item suggests lower priority;
- opening and marking important suggests `must_read`;
- repeated behavior by feed, author, topic, or URL pattern should become explicit rules.

Until direct feedback integration exists, preference adjustments should be captured as explicit rules in this file or server config.

## Preference Rules

Add durable preferences here as they become clear. Keep them specific enough that the worker can translate them into cheap filters.

### Must Read Indicators

- 

### Possible Interest Indicators

- 

### Ignore Indicators

more context: https://chatgpt.com/c/6a887e3e-c214-83e8-8955-e55fe68eaa5d- 
