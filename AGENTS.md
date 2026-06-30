# Undercover Agent

Undercover Agent acts as a model provider implementing the OpenAI **chat completions**
API. It is designed to log everything that a coding harness (such as GitHub
Copilot, OpenCode, or any other coding agent) sends to the model, and it logs the
responses.

A harness points its "OpenAI base URL" at Undercover Agent, and from that moment every
request and response flows through (and is recorded by) Undercover Agent.

It has two modes:

- **"act dumb"** (the default): it pretends to be an LLM but is secretly just
  logging everything and returning dumb, boring responses. It never contacts a
  real model.
- **"man in the middle" (MITM)**: it forwards requests to a real upstream LLM,
  relays the answer back to the harness, and logs everything in between.

## Goals & non-goals

- **Goal:** faithfully capture the full request/response traffic between a harness
  and a model, grouped into human-readable sessions.
- **Goal:** be a drop-in OpenAI-compatible endpoint so no harness changes are
  needed beyond pointing at our base URL.
- **Non-goal:** being a smart model. In dumb mode the responses are intentionally
  trivial.
- **Non-goal:** driving tool-call loops. Tool calls are rarely required, so dumb
  mode normally just answers with text (see "Act dumb" mode below).

## Tech stack

- **Python 3.9** minimum code-compatibility, **3.14** used during development.
- **Flask** for the HTTP server (familiar, simple, and streaming via generator
  responses is straightforward).
- **httpx** — used to forward requests upstream in MITM mode via raw HTTP. We
  deliberately avoid the OpenAI SDK so nothing is reshaped or dropped on the way
  through: Undercover Agent relays exactly what the upstream sent.
- Dependencies are pinned in `requirements.txt`; project metadata lives in
  `pyproject.toml`.
- A local virtual environment is created at `.venv`.
- Entrypoint: `python -m undercover_agent`.

## HTTP API

Undercover Agent implements the subset of the OpenAI REST API that coding harnesses
actually use:

- `POST /v1/chat/completions` — the main endpoint. Supports both non-streaming
  (single JSON body) and streaming (`stream: true`, Server-Sent Events) requests,
  honoring whatever the request asks for.
- `GET /v1/models` — most harnesses probe this on startup; returns a small static
  list advertising the Undercover Agent model id(s).
- `GET /health` — simple liveness check.
- Human-facing report routes (`GET /`, `GET /sessions/<id>`, …) are described under
  **Human interface** below.

### Authentication

The `Authorization` header from the harness is **logged but otherwise ignored** in
dumb mode. In MITM mode the harness's key is **not** forwarded; Undercover Agent
authenticates to the upstream using its own configured credentials (see below).

## Modes

### "Act dumb" mode (default)

- Returns a valid OpenAI chat-completion response with a short, boring, canned
  text message (e.g. an acknowledgement that the request was received).
- Responses are well-formed: correct `choices`, `finish_reason: "stop"`, and a
  plausible (faked) `usage` block, so harnesses don't choke.
- Honors `stream`: when streaming is requested, the canned text is emitted as SSE
  chunks followed by `[DONE]`.
- It does **not** try to call the harness's tools. Tool calls are rarely required
  to keep a harness functioning, so plain text is the default. If a future need
  arises to emit tool calls, only safe, read-only-looking tools
  (`get*`/`list*`/`read*`/`search*`) may ever be selected.

### MITM mode

- Forwards the incoming chat-completion request to a real upstream LLM and relays
  the response back to the harness unchanged.
- Streaming is passed through transparently (SSE in → SSE out).
- Both the outgoing request and the upstream response are logged, including the
  upstream per-call response `id` and `usage`.
- The `model` is **passed through unchanged**: run with `--model` set to the real
  upstream model id (it's advertised via `/v1/models`, the harness sends it, and
  Undercover Agent forwards it).
- **Errors are relayed**: if the upstream returns a non-2xx response (or the
  request times out / fails at the network level), Undercover Agent relays the
  upstream status code together with an OpenAI-style `{"error": {…}}` body, and
  records the failed turn in the session log.

MITM forwarding is implemented with **httpx** (raw HTTP), using a client
configured with the base URL and API key passed on the command line. Streaming
responses are read line by line and each upstream `data:` chunk is relayed as-is.

#### Upstream configuration (CLI flags)

- `--base-url` — base URL of the real OpenAI-compatible provider
  (e.g. `https://api.openai.com/v1`), used as the httpx client base URL.
- `--api-key` — credential Undercover Agent uses to authenticate upstream, sent as a
  `Bearer` token on the forwarded request.

If `--mode mitm` is given without both `--base-url` and `--api-key`, Undercover Agent
exits at startup with a clear error.

## Sessions

A session is **not** an invocation of this provider. A session is a single
conversation between the harness and the model provider. A session can be
long-running and resumed later.

Each session id **encodes its creation date and time** (UTC) plus a short random
suffix for uniqueness, e.g. `20260630T101345Z-a1b2c3d4`. Timestamped ids sort
chronologically on disk and let a human find or open a specific session by time
without consulting the index.

### The stateless-API problem

The OpenAI chat completions API is **stateless**: every request carries the entire
message history and there is no session id in the protocol. Undercover Agent therefore
**reconstructs** sessions from the message contents using prefix matching.

### Session detection (prefix matching)

When a harness continues a conversation, the next request's `messages` array
contains everything from the previous turn (including the assistant reply Undercover
Agent returned) plus the newly added user/tool messages. So the recorded
conversation-so-far of an existing session will be a **prefix** of the incoming
request's `messages`.

Algorithm on each `POST /v1/chat/completions`:

1. Compare the incoming `messages` against the stored message log of every known
   session.
2. If an existing session's stored messages are a prefix of the incoming
   `messages`, treat the request as a **continuation** of that session (longest
   matching prefix wins). Append the newly added messages and the new response as
   a new "turn."
3. If no session matches, mint a **new session** (a fresh timestamped id, see
   above) rooted at this request.

This same logic is used in both dumb and MITM mode. Because the upstream API is
also stateless, there is **no real upstream "session id" to map to** — Undercover
Agent simply records the upstream per-call response `id` on each turn within its
own reconstructed session.

### Edge cases

- **Branching / regeneration / edited history:** if a request doesn't prefix-match
  any known session, it starts a new session. This is acceptable and intentional.
- Matching is on message role + content; tool-call/tool-result messages are part
  of the compared history.

## Storage / logging

- Logs live under `logs/`, one JSON file per session: `logs/<session-id>.json`.
- Each session file contains:
  - **metadata**: session id, mode, created/updated timestamps, model id.
  - **turns**: an ordered list where each turn records the request (newly added
    messages, sampling params, declared `tools`, `stream` flag) and the response
    (text and/or tool calls, `finish_reason`, `usage`, timing, and—when
    streaming—the assembled stream).
- This per-session JSON **is** the machine-readable report.

## The Report

The report is the most important artifact of Undercover Agent, so it exists in two
versions:

1. **Machine-readable (JSON):** the per-session `logs/<session-id>.json` files
   described above, intended for a harness/tooling to consume.
2. **Human-readable (HTML):** nicely formatted, generated **on demand** from the
   JSON logs. It renders like a chat log from a normal agent UI (user / assistant
   / tool messages, tool calls, token usage, timings).

## Human interface

Undercover Agent is meant to be browsed by a human; the HTML report is the front door.

- **Index page** — lists every session, newest first, showing its timestamp (read
  from the session id), mode (dumb/mitm), model id, number of turns, and total
  token usage. Each row links to that session's report.
- **Per-session report** — renders the full conversation like a normal agent chat
  UI. Because the filename/URL is the timestamped session id, any session can be
  **opened directly** (bookmarked, linked, or typed by hand) without going through
  the index.
- **Raw log** — the underlying `logs/<session-id>.json` is always available next
  to the report for anyone who wants the machine-readable version.

There are two ways to get the HTML:

1. **Static export (CLI):** `python -m undercover_agent report` reads `logs/` and
   writes `reports/index.html` plus one `reports/<session-id>.html` per session.
   Open `reports/index.html` in a browser; click a row to open that session, or
   open `reports/<session-id>.html` directly.
2. **Live (while the server is running):** the server also exposes a small
   read-only browsing UI so logs can be viewed without a separate build step:
   - `GET /` → the session index.
   - `GET /sessions/<session-id>` → that session's human-readable HTML.
   - `GET /sessions/<session-id>.json` → that session's raw JSON log.

Both paths satisfy "open the log directly": from the index by clicking, or straight
to `/sessions/<session-id>` (live) / `reports/<session-id>.html` (static).

## Configuration summary

- **CLI flags:** `--mode {dumb,mitm}` (default `dumb`), `--host` (default
  `0.0.0.0`), `--port` (default `8000`).
- **MITM CLI flags:** `--base-url`, `--api-key` (used by the httpx client;
  required together when `--mode mitm`).
- **Defaults:** dumb mode, listening on `0.0.0.0:8000`.

## Running

```bash
# one-time setup (use whatever Python 3.12 interpreter is on your system)
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# dumb mode (default)
.venv/bin/python -m undercover_agent

# MITM mode (forwards upstream via raw HTTP / httpx)
.venv/bin/python -m undercover_agent --mode mitm --base-url https://api.openai.com/v1 --api-key sk-...

# generate HTML reports from the logs
.venv/bin/python -m undercover_agent report
```
