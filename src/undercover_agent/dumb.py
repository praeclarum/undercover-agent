"""Dumb mode: pretend to be an LLM while never contacting one.

Returns well-formed OpenAI chat-completion responses with a short canned message
and a plausible (faked) ``usage`` block. Honors streaming by emitting the canned
text as ``chat.completion.chunk`` deltas.

This module is intentionally simple but *fully functional*, because it is what
exercises the server's streaming dataflow end to end.
"""

from __future__ import annotations

import time
from typing import Any, Iterator

from .config import Config

CANNED_PREFIX = "Acknowledged by Undercover Agent (dumb mode). Your request was logged; no real model was contacted."

MAX_ECHO_CHARS = 512


class DumbBackend:
    """A backend that answers every request with the same boring text."""

    def __init__(self, config: Config):
        self.config = config

    # -- non-streaming ----------------------------------------------------- #

    def complete(self, body: dict[str, Any]) -> dict[str, Any]:
        text = _canned_reply(body)
        usage = _fake_usage(body, text)
        return {
            "id": _response_id(),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": _model_for(body, self.config),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": usage,
        }

    # -- streaming --------------------------------------------------------- #

    def stream(self, body: dict[str, Any]) -> Iterator[dict[str, Any]]:
        response_id = _response_id()
        created = int(time.time())
        text = _canned_reply(body)
        base = {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": _model_for(body, self.config),
        }

        # First chunk announces the assistant role.
        yield {**base, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}

        # Stream the canned text word by word.
        for token in _tokenize(text):
            yield {**base, "choices": [{"index": 0, "delta": {"content": token}, "finish_reason": None}]}

        # Final chunk: stop + (optional) usage.
        yield {
            **base,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": _fake_usage(body, text),
        }


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _canned_reply(body: dict[str, Any]) -> str:
    """A boring but slightly informative reply, so a tester can see the round-trip.

    Echoes back only a short prefix of what the harness said (up to 256 chars),
    wrapped in a Markdown code fence so it renders cleanly and never lets the
    echoed content break out into the surrounding message.
    """
    last_user = _last_user_text(body)
    if not last_user:
        return CANNED_PREFIX

    snippet = last_user.strip()
    if len(snippet) > MAX_ECHO_CHARS:
        snippet = snippet[:MAX_ECHO_CHARS].rstrip() + "\u2026"
    fence = _safe_fence(snippet)
    return f"{CANNED_PREFIX} You said:\n\n{fence}\n{snippet}\n{fence}"


def _safe_fence(text: str) -> str:
    """Pick a backtick fence long enough to safely wrap ``text``."""
    longest = 0
    run = 0
    for ch in text:
        if ch == "`":
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    return "`" * max(3, longest + 1)


def _last_user_text(body: dict[str, Any]) -> str:
    for msg in reversed(body.get("messages") or []):
        if msg.get("role") == "user":
            return _text_of(msg.get("content"))
    return ""


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            p.get("text", "")
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        ]
        return " ".join(t for t in parts if t)
    return ""


def _model_for(body: dict[str, Any], config: Config) -> str:
    """Echo back the model the harness asked for (falling back to ours)."""
    requested = body.get("model")
    return requested if isinstance(requested, str) and requested else config.model


def _tokenize(text: str) -> list[str]:
    """Split into whitespace-preserving fragments so the reassembled stream equals
    the original text."""
    parts: list[str] = []
    for i, word in enumerate(text.split(" ")):
        parts.append(word if i == 0 else " " + word)
    return parts


def _response_id() -> str:
    return f"chatcmpl-dumb-{int(time.time() * 1000)}"


def _fake_usage(body: dict[str, Any], completion: str) -> dict[str, int]:
    prompt_tokens = sum(
        _approx_tokens(m.get("content")) for m in (body.get("messages") or [])
    )
    completion_tokens = _approx_tokens(completion)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def _approx_tokens(content: Any) -> int:
    if isinstance(content, list):
        return sum(_approx_tokens(p.get("text")) for p in content if isinstance(p, dict))
    if not isinstance(content, str):
        return 0
    return max(1, len(content) // 4)


__all__ = ["DumbBackend"]
