"""Session reconstruction, turn recording, and JSON persistence.

The OpenAI chat-completions API is stateless: every request carries the full
message history. Undercover Agent reconstructs long-running "sessions" by prefix
matching the incoming ``messages`` against the messages it has already recorded
(see :meth:`SessionStore.find_or_create`).

This module also owns the logic for turning a streamed sequence of OpenAI chunk
dicts back into a single assembled response (:func:`assemble_stream`), which is
what makes streaming requests loggable in the same shape as non-streaming ones.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable


# --------------------------------------------------------------------------- #
# Session ids
# --------------------------------------------------------------------------- #

def new_session_id(now: datetime | None = None) -> str:
    """Return a timestamped, sortable session id like ``20260630T101345Z-a1b2c3d4``."""
    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    suffix = secrets.token_hex(4)
    return f"{stamp}-{suffix}"


# --------------------------------------------------------------------------- #
# In-memory session model
# --------------------------------------------------------------------------- #

@dataclass
class Session:
    """One reconstructed conversation between a harness and the provider."""

    id: str
    mode: str
    model: str
    created: str
    updated: str
    # The full message history as recorded so far (incoming messages + each
    # assistant reply we returned). This is what we prefix-match against.
    messages: list[dict[str, Any]] = field(default_factory=list)
    turns: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "metadata": {
                "id": self.id,
                "mode": self.mode,
                "model": self.model,
                "created": self.created,
                "updated": self.updated,
                "turns": len(self.turns),
                "usage": self.total_usage(),
            },
            "messages": self.messages,
            "turns": self.turns,
        }

    def total_usage(self) -> dict[str, int]:
        total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        for turn in self.turns:
            usage = (turn.get("response") or {}).get("usage") or {}
            for key in total:
                total[key] += int(usage.get(key) or 0)
        return total

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Session":
        meta = data.get("metadata", {})
        return cls(
            id=meta.get("id", ""),
            mode=meta.get("mode", "dumb"),
            model=meta.get("model", ""),
            created=meta.get("created", ""),
            updated=meta.get("updated", ""),
            messages=data.get("messages", []),
            turns=data.get("turns", []),
        )


# --------------------------------------------------------------------------- #
# Streaming assembly
# --------------------------------------------------------------------------- #

def assemble_stream(chunks: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Collapse a sequence of OpenAI ``chat.completion.chunk`` dicts into a single
    ``chat.completion``-shaped response dict, so a streamed turn can be logged the
    same way as a non-streamed one.
    """
    content_parts: list[str] = []
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    resp_id: str | None = None
    model: str | None = None
    # tool_call fragments keyed by index -> assembled tool call
    tool_calls: dict[int, dict[str, Any]] = {}

    for chunk in chunks:
        resp_id = chunk.get("id", resp_id)
        model = chunk.get("model", model)
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices", []):
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content_parts.append(delta["content"])
            for tc in delta.get("tool_calls", []) or []:
                idx = tc.get("index", 0)
                slot = tool_calls.setdefault(
                    idx,
                    {"id": tc.get("id"), "type": tc.get("type", "function"),
                     "function": {"name": "", "arguments": ""}},
                )
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["function"]["arguments"] += fn["arguments"]
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]

    message: dict[str, Any] = {"role": "assistant", "content": "".join(content_parts) or None}
    if tool_calls:
        message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]

    return {
        "id": resp_id,
        "object": "chat.completion",
        "model": model,
        "choices": [
            {"index": 0, "message": message, "finish_reason": finish_reason or "stop"}
        ],
        "usage": usage,
    }


def _assistant_message(response: dict[str, Any]) -> dict[str, Any]:
    """Pull the assistant message out of a (possibly assembled) response dict."""
    choices = response.get("choices") or []
    if not choices:
        return {"role": "assistant", "content": None}
    return choices[0].get("message") or {"role": "assistant", "content": None}


# --------------------------------------------------------------------------- #
# Persistence + session detection
# --------------------------------------------------------------------------- #

def _messages_match(prefix: list[dict[str, Any]], whole: list[dict[str, Any]]) -> bool:
    """True if ``prefix`` is a (role, content) prefix of ``whole``."""
    if len(prefix) > len(whole):
        return False
    for a, b in zip(prefix, whole):
        if a.get("role") != b.get("role"):
            return False
        if a.get("content") != b.get("content"):
            return False
    return True


class SessionStore:
    """Loads, matches, and persists sessions under a logs directory."""

    def __init__(self, logs_dir: str, mode: str = "dumb", model: str = "undercover-agent"):
        self.logs_dir = logs_dir
        self.mode = mode
        self.model = model
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}
        os.makedirs(self.logs_dir, exist_ok=True)
        self._load_all()

    # -- loading ----------------------------------------------------------- #

    def _load_all(self) -> None:
        for name in os.listdir(self.logs_dir):
            if not name.endswith(".json"):
                continue
            path = os.path.join(self.logs_dir, name)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    session = Session.from_dict(json.load(fh))
                if session.id:
                    self._sessions[session.id] = session
            except (OSError, json.JSONDecodeError):
                continue

    def list_sessions(self) -> list[Session]:
        return sorted(self._sessions.values(), key=lambda s: s.id, reverse=True)

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    # -- detection --------------------------------------------------------- #

    def find_or_create(self, messages: list[dict[str, Any]]) -> Session:
        """Return the existing session whose recorded messages are the longest
        prefix of ``messages``, or mint a new one if nothing matches.
        """
        with self._lock:
            best: Session | None = None
            for session in self._sessions.values():
                if _messages_match(session.messages, messages):
                    if best is None or len(session.messages) > len(best.messages):
                        best = session
            if best is not None:
                return best

            now = _now_iso()
            session = Session(
                id=new_session_id(),
                mode=self.mode,
                model=self.model,
                created=now,
                updated=now,
            )
            self._sessions[session.id] = session
            return session

    # -- recording --------------------------------------------------------- #

    def record_turn(
        self,
        session: Session,
        *,
        request_body: dict[str, Any],
        response: dict[str, Any],
        raw_chunks: list[dict[str, Any]] | None = None,
        timing: dict[str, Any] | None = None,
        upstream_id: str | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        """Append one request/response turn to ``session`` and persist it.

        When ``error`` is given (an OpenAI-style ``{"error": {...}}`` body from a
        failed upstream call), the failure is recorded faithfully and no assistant
        message is appended to the session history \u2014 so a harness retry of the same
        messages continues this session instead of branching into a new one.
        """
        with self._lock:
            incoming = request_body.get("messages", []) or []
            new_messages = incoming[len(session.messages):]

            response_record: dict[str, Any] = {
                "id": response.get("id"),
                "upstream_id": upstream_id,
                "timing": timing or {},
            }
            if error is not None:
                response_record["error"] = error.get("error", error)
                response_record["message"] = None
                response_record["finish_reason"] = "error"
                response_record["usage"] = None
            else:
                response_record["message"] = _assistant_message(response)
                response_record["finish_reason"] = _finish_reason(response)
                response_record["usage"] = response.get("usage")

            turn = {
                "request": {
                    "new_messages": new_messages,
                    "params": _sampling_params(request_body),
                    "tools": request_body.get("tools"),
                    "stream": bool(request_body.get("stream", False)),
                },
                "response": response_record,
            }
            if raw_chunks is not None:
                turn["response"]["raw_chunks"] = raw_chunks

            session.turns.append(turn)
            if error is None:
                session.messages = list(incoming) + [response_record["message"]]
            else:
                session.messages = list(incoming)
            session.updated = _now_iso()
            self._persist(session)

    def _persist(self, session: Session) -> None:
        path = os.path.join(self.logs_dir, f"{session.id}.json")
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(session.to_dict(), fh, indent=2, ensure_ascii=False)
        os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

_SAMPLING_KEYS = (
    "temperature", "top_p", "max_tokens", "max_completion_tokens",
    "presence_penalty", "frequency_penalty", "stop", "seed", "n",
    "tool_choice", "response_format",
)


def _sampling_params(body: dict[str, Any]) -> dict[str, Any]:
    return {k: body[k] for k in _SAMPLING_KEYS if k in body}


def _finish_reason(response: dict[str, Any]) -> str | None:
    choices = response.get("choices") or []
    if not choices:
        return None
    return choices[0].get("finish_reason")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
