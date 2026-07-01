# Undercover Agent

[![PyPI](https://img.shields.io/pypi/v/undercover-agent.svg)](https://pypi.org/project/undercover-agent/)

A drop-in **OpenAI-compatible chat completions server** that sits between a coding
harness (GitHub Copilot, OpenCode, or anything that speaks the OpenAI API) and a
real model — and **logs everything** that flows through it.

Point your harness's "OpenAI base URL" at Undercover Agent, and every request and
response is recorded into clean, browsable, per-conversation logs.

## What it's for

When you're building or debugging an agent, the most useful thing you can have is
the *exact* traffic the harness sends to the model: the full message history,
system prompts, declared tools, sampling params, and the responses. Undercover Agent
captures all of it without requiring any changes to the harness beyond a base-URL
swap.

## Two modes

- **Dumb mode (default)** — never contacts a real model. It returns well-formed,
  intentionally boring canned responses (correct `choices`, `finish_reason`, a
  plausible faked `usage` block) so the harness keeps working while you inspect
  what it's sending.
- **MITM mode** — forwards each request to a real upstream LLM via the official
  OpenAI SDK, relays the answer back unchanged, and logs everything in between.

Both modes stream when the request asks for it (`stream: true`), passing SSE
through transparently.

## Sessions

The OpenAI chat API is stateless — every request resends the whole conversation
and carries no session id. Undercover Agent **reconstructs** sessions by prefix
matching: if a stored conversation is a prefix of an incoming request's
`messages`, the request is treated as a continuation of that session (longest
match wins); otherwise a new session is born.

Each session gets a timestamped id like `20260630T101345Z-a1b2c3d4`, so logs sort
chronologically and any session can be opened directly by its id.

## Install & run

Requires Python 3.9+.

```bash
pip install undercover-agent
```

Or, from a checkout of this repository:

```bash
pip install -r requirements.txt
```

### Dumb mode (default)

```bash
python -m undercover_agent
```

Then point your harness at `http://localhost:8000/v1`.

A GUI is provided at `http://localhost:8000/` to view the logs live.

### MITM mode

```bash
python -m undercover_agent --mode mitm \
  --base-url https://api.openai.com/v1 \
  --api-key sk-...
```

The harness's own `Authorization` header is logged but never forwarded — Undercover
Agent authenticates upstream with the `--api-key` you pass here.

### CLI flags

| Flag | Default | Description |
| --- | --- | --- |
| `--mode {dumb,mitm}` | `dumb` | Logging-only or forward-upstream. |
| `--host` | `0.0.0.0` | Bind address. |
| `--port` | `8000` | Bind port. |
| `--model` | `undercover-agent` | Model id advertised to the harness. |
| `--logs-dir` | `logs` | Where per-session JSON logs are written. |
| `--base-url` | — | Upstream base URL (MITM only). |
| `--api-key` | — | Upstream credential (MITM only). |

## HTTP API

Undercover Agent implements the slice of the OpenAI REST API that harnesses actually
use:

- `POST /v1/chat/completions` — the main endpoint (streaming and non-streaming).
- `GET /v1/models` — returns the advertised model id(s).
- `GET /health` — liveness check.

## Viewing the logs

Logs are written as one JSON file per session under `logs/<session-id>.json`. That
JSON **is** the machine-readable report. There's also a human-readable HTML view,
available two ways:

### Live, while the server runs

- `GET /` — index of all sessions, newest first (timestamp, mode, model, turn
  count, token usage).
- `GET /sessions/<session-id>` — the full conversation rendered like a normal chat
  UI.
- `GET /sessions/<session-id>.json` — the raw JSON log.

### Static export

```bash
python -m undercover_agent report
```

Writes `reports/index.html` plus one `reports/<session-id>.html` per session. Open
`reports/index.html` and click through, or open a session file directly.

## How it's built

- **Flask** for the HTTP server (streaming via generator responses).
- **httpx** for the HTTP client (MITM mode).
