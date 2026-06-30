"""MITM mode: forward to a real upstream LLM and relay the response back.

This backend is a *byte-faithful passthrough*: it forwards the incoming chat
completion request to a real OpenAI-compatible upstream using raw HTTP (``httpx``)
and relays the answer back to the harness unchanged, logging everything in
between. We deliberately avoid the OpenAI SDK here so that nothing is reshaped or
dropped on the way through — the whole point of Undercover Agent is to capture exactly
what the upstream sent.

It mirrors :class:`undercover_agent.dumb.DumbBackend`'s interface:

* ``complete``: POST a non-streaming request and return the upstream
  ``chat.completion`` dict verbatim (carrying the upstream response ``id`` on a
  private ``_upstream_id`` key for logging).
* ``stream``: POST a streaming request and yield each upstream
  ``chat.completion.chunk`` dict, passing SSE through transparently.

Upstream failures (non-2xx responses, timeouts, network errors) are surfaced as
:class:`UpstreamError`, which the server relays to the harness as an OpenAI-style
error body carrying the upstream status code.
"""

from __future__ import annotations

import json
from typing import Any, Iterator

import httpx

from .config import Config


class UpstreamError(Exception):
    """An upstream call failed; carries the status code and an OpenAI-style body.

    ``body`` is the JSON object that should be relayed to the harness, e.g.
    ``{"error": {"message": ..., "type": ..., "code": ...}}``. When the upstream
    itself returned an error body we relay it verbatim; for transport-level
    failures (timeouts, connection errors) we synthesize one.
    """

    def __init__(self, status: int, body: dict[str, Any]):
        super().__init__(_error_message(body))
        self.status = status
        self.body = body


class MitmBackend:
    """Forwards chat completions to a configured upstream provider via raw HTTP."""

    def __init__(self, config: Config):
        self.config = config
        base_url = (config.base_url or "").rstrip("/")
        headers = {"Content-Type": "application/json"}
        if config.api_key:
            # We authenticate upstream with *our* configured key; the harness's
            # Authorization header is never forwarded (we only receive `body`).
            headers["Authorization"] = f"Bearer {config.api_key}"
        # Generous read timeout for slow model responses; short connect timeout.
        timeout = httpx.Timeout(600.0, connect=10.0)
        self._client = httpx.Client(base_url=base_url, headers=headers, timeout=timeout)

    # -- non-streaming ----------------------------------------------------- #

    def complete(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = self._client.post("/chat/completions", json=body)
        except httpx.HTTPError as exc:
            raise UpstreamError(502, _transport_error(exc)) from exc

        if resp.status_code >= 400:
            raise UpstreamError(resp.status_code, _error_body(resp))

        data = _parse_json(resp)
        if isinstance(data, dict):
            data["_upstream_id"] = data.get("id")
            return data
        # Upstream returned 2xx but not a JSON object: wrap it as an error so the
        # harness isn't handed something malformed.
        raise UpstreamError(502, _synthetic_error("upstream returned a non-object response"))

    # -- streaming --------------------------------------------------------- #

    def stream(self, body: dict[str, Any]) -> Iterator[dict[str, Any]]:
        try:
            with self._client.stream("POST", "/chat/completions", json=body) as resp:
                if resp.status_code >= 400:
                    resp.read()
                    raise UpstreamError(resp.status_code, _error_body(resp))

                first = True
                for line in resp.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(chunk, dict):
                        continue
                    if first:
                        chunk["_upstream_id"] = chunk.get("id")
                        first = False
                    yield chunk
        except httpx.HTTPError as exc:
            raise UpstreamError(502, _transport_error(exc)) from exc


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _parse_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except (json.JSONDecodeError, ValueError):
        return None


def _error_body(resp: httpx.Response) -> dict[str, Any]:
    """Relay the upstream error body verbatim when it's a JSON object with an
    ``error`` key; otherwise synthesize an OpenAI-style error from the text."""
    data = _parse_json(resp)
    if isinstance(data, dict) and "error" in data:
        return data
    if isinstance(data, dict):
        return {"error": data}
    text = (resp.text or "").strip()
    return _synthetic_error(text or f"upstream returned HTTP {resp.status_code}")


def _transport_error(exc: httpx.HTTPError) -> dict[str, Any]:
    return _synthetic_error(f"upstream request failed: {exc}", type_="upstream_error")


def _synthetic_error(message: str, type_: str = "upstream_error") -> dict[str, Any]:
    return {"error": {"message": message, "type": type_, "code": None}}


def _error_message(body: dict[str, Any]) -> str:
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        msg = err.get("message")
        if isinstance(msg, str):
            return msg
    return "upstream error"


__all__ = ["MitmBackend", "UpstreamError"]
