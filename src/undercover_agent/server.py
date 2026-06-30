"""The Undercover Agent provider server.

This is the heart of the project: a Flask app that speaks the subset of the
OpenAI REST API that coding harnesses use, routes each request through the
configured mode backend (dumb or mitm), and records every turn via the logging
module.

Special care is taken with **streaming** (``stream: true``): the chosen backend
yields OpenAI ``chat.completion.chunk`` dicts, and :func:`_sse_response` both
forwards them to the client as Server-Sent Events *and* accumulates them so the
turn can be logged once the stream finishes.
"""

from __future__ import annotations

import json
import time
from typing import Any, Iterator

from flask import Flask, Response, abort, jsonify, request, stream_with_context

from . import report
from . import session_log
from .config import Config
from .dumb import DumbBackend
from .mitm import MitmBackend, UpstreamError


def create_app(config: Config) -> Flask:
    app = Flask(__name__)

    store = session_log.SessionStore(
        config.logs_dir, mode=config.mode, model=config.model
    )
    backend = _make_backend(config)

    # Stash for routes / tests / introspection.
    app.config["UNDERCOVER_AGENT"] = {"config": config, "store": store, "backend": backend}

    _register_api_routes(app, config, store, backend)
    _register_browse_routes(app, config, store)
    return app


def _make_backend(config: Config):
    if config.mode == "mitm":
        return MitmBackend(config)
    return DumbBackend(config)


# --------------------------------------------------------------------------- #
# OpenAI-compatible API
# --------------------------------------------------------------------------- #

def _register_api_routes(app: Flask, config: Config, store, backend) -> None:

    @app.get("/health")
    def health():
        return jsonify({"status": "ok", "mode": config.mode, "model": config.model})

    @app.get("/v1/models")
    def list_models():
        return jsonify(
            {
                "object": "list",
                "data": [
                    {
                        "id": config.model,
                        "object": "model",
                        "created": 0,
                        "owned_by": "undercover-agent",
                    }
                ],
            }
        )

    @app.post("/v1/chat/completions")
    def chat_completions():
        body = request.get_json(force=True, silent=True)
        if not isinstance(body, dict):
            abort(400, description="request body must be a JSON object")

        messages = body.get("messages") or []
        stream = bool(body.get("stream", False))

        # Reconstruct (or start) the session this request belongs to.
        session = store.find_or_create(messages)

        if stream:
            return _sse_response(config, store, backend, session, body)
        return _json_response(config, store, backend, session, body)


def _json_response(config: Config, store, backend, session, body: dict[str, Any]):
    started = time.monotonic()
    try:
        response = backend.complete(body)
    except UpstreamError as exc:
        elapsed = time.monotonic() - started
        return _record_error(store, session, body, exc, elapsed)
    elapsed = time.monotonic() - started

    store.record_turn(
        session,
        request_body=body,
        response=response,
        timing={"seconds": round(elapsed, 4)},
        upstream_id=response.get("_upstream_id"),
    )
    response.pop("_upstream_id", None)
    return jsonify(response)


def _record_error(store, session, body: dict[str, Any], exc: UpstreamError, elapsed: float):
    """Log a failed upstream turn and relay the error to the harness.

    The error body is recorded as the turn's response (so the failure is captured
    faithfully) and returned with the upstream status code.
    """
    store.record_turn(
        session,
        request_body=body,
        response={},
        timing={"seconds": round(elapsed, 4)},
        error=exc.body,
    )
    return jsonify(exc.body), exc.status


def _sse_response(config: Config, store, backend, session, body: dict[str, Any]):
    """Stream the backend's chunks as SSE while accumulating them for logging."""

    started = time.monotonic()

    # Prime the generator so that an upstream connection/status error surfaces
    # *before* we commit to a 200 text/event-stream response — once SSE headers
    # are sent we can no longer relay the upstream status code.
    stream = backend.stream(body)
    try:
        first_chunk: dict[str, Any] | None = next(stream)
    except StopIteration:
        first_chunk = None
    except UpstreamError as exc:
        elapsed = time.monotonic() - started
        return _record_error(store, session, body, exc, elapsed)

    def generate() -> Iterator[str]:
        chunks: list[dict[str, Any]] = []
        upstream_id: str | None = None
        try:
            for chunk in _prepend(first_chunk, stream):
                # Backends may attach a private upstream id on the first chunk.
                if upstream_id is None and chunk.get("_upstream_id"):
                    upstream_id = chunk.pop("_upstream_id")
                chunks.append(chunk)
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        finally:
            elapsed = time.monotonic() - started
            assembled = session_log.assemble_stream(chunks)
            store.record_turn(
                session,
                request_body=body,
                response=assembled,
                raw_chunks=chunks,
                timing={"seconds": round(elapsed, 4)},
                upstream_id=upstream_id,
            )

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _prepend(first: dict[str, Any] | None, rest: Iterator[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    if first is not None:
        yield first
    yield from rest


# --------------------------------------------------------------------------- #
# Human browsing UI
# --------------------------------------------------------------------------- #

def _connection_info(config: Config) -> dict[str, Any]:
    """Build the connection/usage details shown on the index, using the URL the
    browser actually reached us on (so it's copy-pasteable)."""
    base = request.host_url.rstrip("/")  # e.g. http://localhost:8000
    return {
        "mode": config.mode,
        "model": config.model,
        "base_url": f"{base}/v1",
        "chat_url": f"{base}/v1/chat/completions",
        "models_url": f"{base}/v1/models",
        "health_url": f"{base}/health",
    }


def _register_browse_routes(app: Flask, config: Config, store) -> None:

    @app.get("/")
    def index():
        html = report.render_index(
            store.list_sessions(),
            connection=_connection_info(config),
            url_for_session=lambda sid: f"/sessions/{sid}",
            json_for_session=lambda sid: f"/sessions/{sid}.json",
        )
        return Response(html, mimetype="text/html")

    @app.get("/sessions/<session_id>")
    def session_html(session_id: str):
        session = store.get(session_id)
        if session is None:
            abort(404)
        html = report.render_session(
            session,
            connection=_connection_info(config),
            index_url="/",
            json_url=f"/sessions/{session_id}.json",
        )
        return Response(html, mimetype="text/html")

    @app.get("/sessions/<session_id>.json")
    def session_json(session_id: str):
        session = store.get(session_id)
        if session is None:
            abort(404)
        return jsonify(session.to_dict())


__all__ = ["create_app"]
