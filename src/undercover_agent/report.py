"""Human-readable HTML rendering for the session index and per-session reports.

Two entry points:

* :func:`render_index` — the front door: connection/usage help plus a table of
  every recorded session.
* :func:`render_session` — one conversation rendered like a normal agent chat UI.

Both are pure functions that take already-loaded :class:`undercover_agent.session_log.Session`
objects (and a small ``connection`` dict) and return a complete HTML document, so
the same code backs both the live server routes and the static export.

All harness-supplied text is escaped with :func:`html.escape`; nothing from a log
is ever emitted as raw HTML.
"""

from __future__ import annotations

import html
import json
import os
from typing import Any, Callable, Iterable

# A callable that maps a session id to the URL/href for its report page.
LinkFn = Callable[[str], str]


# --------------------------------------------------------------------------- #
# Shared chrome
# --------------------------------------------------------------------------- #

_CSS = """
:root {
  --bg: #0f1115; --panel: #171a21; --panel2: #1e222b; --border: #2a2f3a;
  --text: #e6e8ec; --muted: #9aa3b2; --accent: #6ea8fe; --accent2: #7ee787;
  --user: #2b3b55; --assistant: #243024; --system: #3a2f1c; --tool: #2d2438;
  --code: #0b0d11;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
.wrap { max-width: 980px; margin: 0 auto; padding: 28px 20px 80px; }
header.top { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; margin-bottom: 6px; }
header.top h1 { font-size: 22px; margin: 0; }
.sub { color: var(--muted); font-size: 13px; }
.card { background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
  padding: 16px 18px; margin: 18px 0; }
.card h2 { margin: 0 0 10px; font-size: 15px; letter-spacing: .02em; text-transform: uppercase; color: var(--muted); }
.kv { display: grid; grid-template-columns: max-content 1fr; gap: 6px 14px; align-items: center; }
.kv .k { color: var(--muted); }
code, pre, .mono { font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace; }
pre { background: var(--code); border: 1px solid var(--border); border-radius: 8px;
  padding: 12px 14px; overflow: auto; margin: 8px 0; font-size: 13px; }
code.inline { background: var(--code); border: 1px solid var(--border); border-radius: 6px;
  padding: 1px 6px; font-size: 13px; }
.badge { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 12px;
  border: 1px solid var(--border); color: var(--muted); }
.badge.dumb { color: var(--accent2); border-color: #2c4a2c; }
.badge.mitm { color: #ffb454; border-color: #4a3a1c; }
.badge.err { color: #ff6b6b; border-color: #5a2424; }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: 9px 10px; border-bottom: 1px solid var(--border); font-size: 14px; }
th { color: var(--muted); font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: .03em; }
tr:hover td { background: var(--panel2); }
.right { text-align: right; }
.empty { color: var(--muted); padding: 24px; text-align: center; }
.msg { border: 1px solid var(--border); border-radius: 10px; margin: 12px 0; overflow: hidden; }
.msg > .role { padding: 6px 12px; font-size: 12px; text-transform: uppercase; letter-spacing: .04em;
  color: var(--muted); border-bottom: 1px solid var(--border); background: var(--panel2);
  display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.msg > .body { padding: 12px 14px; white-space: pre-wrap; word-wrap: break-word; }
.copybtn { font: inherit; font-size: 11px; line-height: 1; letter-spacing: .04em; text-transform: uppercase;
  color: var(--muted); background: var(--panel); border: 1px solid var(--border); border-radius: 6px;
  padding: 3px 9px; cursor: pointer; flex: none; }
.copybtn:hover { color: var(--text); border-color: var(--accent); }
.copybtn.copied { color: var(--accent2); border-color: #2c4a2c; }
.msg.user { background: var(--user); }
.msg.assistant { background: var(--assistant); }
.msg.system { background: var(--system); }
.msg.tool { background: var(--tool); }
.msg.error { background: #2a1414; border-color: #5a2424; }
.msg.error > .role { color: #ff6b6b; }
.msg.error { background: #2a1414; border-color: #5a2424; }
.msg.error > .role { color: #ff6b6b; }
.turn { margin: 26px 0; }
.turn .turnhead { display: flex; gap: 12px; flex-wrap: wrap; align-items: baseline;
  color: var(--muted); font-size: 12px; border-bottom: 1px dashed var(--border); padding-bottom: 6px; }
.turn .turnhead .n { color: var(--text); font-weight: 600; }
.toolcall { border: 1px dashed var(--border); border-radius: 8px; margin: 8px 0; padding: 8px 10px; }
.toolcall .name { color: var(--accent); }
details { margin: 6px 0; }
summary { cursor: pointer; color: var(--muted); }
.nav { margin-bottom: 14px; font-size: 13px; }
"""


_JS = """
document.addEventListener('click', function (e) {
  var btn = e.target.closest('.copybtn');
  if (!btn) return;
  var text = btn.getAttribute('data-copy') || '';
  var done = function () {
    var original = btn.textContent;
    btn.textContent = 'Copied';
    btn.classList.add('copied');
    setTimeout(function () {
      btn.textContent = original;
      btn.classList.remove('copied');
    }, 1200);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done, function () {});
  } else {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); done(); } catch (err) {}
    document.body.removeChild(ta);
  }
});
"""


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html>\n<html lang=\"en\"><head>"
        "<meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{html.escape(title)}</title>"
        f"<style>{_CSS}</style>"
        "</head><body><div class=\"wrap\">"
        f"{body}"
        "</div>"
        f"<script>{_JS}</script>"
        "</body></html>"
    )


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _badge_mode(mode: str) -> str:
    cls = "mitm" if mode == "mitm" else "dumb"
    return f'<span class="badge {cls}">{_esc(mode)}</span>'


# --------------------------------------------------------------------------- #
# Index
# --------------------------------------------------------------------------- #

def render_index(
    sessions: Iterable[Any],
    *,
    connection: dict[str, Any],
    url_for_session: LinkFn,
    json_for_session: LinkFn,
) -> str:
    sessions = list(sessions)
    body = [
        '<header class="top"><h1>🕵️ Undercover Agent</h1>'
        f'<span class="sub">{_badge_mode(connection.get("mode", "dumb"))} '
        f'· model <code class="inline">{_esc(connection.get("model"))}</code></span></header>',
        '<p class="sub">An OpenAI-compatible chat-completions endpoint that records '
        'everything your harness sends and returns.</p>',
        _connection_card(connection),
        _sessions_card(sessions, url_for_session, json_for_session),
    ]
    return _page("Undercover Agent", "\n".join(body))


def _connection_card(conn: dict[str, Any]) -> str:
    base = conn.get("base_url", "")
    chat = conn.get("chat_url", "")
    models = conn.get("models_url", "")
    health = conn.get("health_url", "")
    model = conn.get("model", "")
    mode = conn.get("mode", "dumb")

    curl = (
        f"curl {chat} \\\n"
        f"  -H 'Content-Type: application/json' \\\n"
        f"  -H 'Authorization: Bearer any-key-works' \\\n"
        f"  -d '{{\n"
        f"    \"model\": \"{model}\",\n"
        f"    \"messages\": [{{\"role\": \"user\", \"content\": \"hello\"}}],\n"
        f"    \"stream\": true\n"
        f"  }}'"
    )

    auth_note = (
        "The <code class=\"inline\">Authorization</code> header is logged but "
        "ignored — any key works."
        if mode == "dumb"
        else "Your harness key is logged but not forwarded; Undercover Agent uses its "
        "own upstream credentials."
    )

    return (
        '<div class="card"><h2>Point your harness here</h2>'
        '<div class="kv">'
        f'<div class="k">Base URL</div><div><code class="inline">{_esc(base)}</code></div>'
        f'<div class="k">Model id</div><div><code class="inline">{_esc(model)}</code></div>'
        f'<div class="k">Mode</div><div>{_badge_mode(mode)}</div>'
        f'<div class="k">Chat endpoint</div><div><code class="inline">POST {_esc(chat)}</code></div>'
        f'<div class="k">Models</div><div><a href="{_esc(models)}"><code class="inline">GET {_esc(models)}</code></a></div>'
        f'<div class="k">Health</div><div><a href="{_esc(health)}"><code class="inline">GET {_esc(health)}</code></a></div>'
        '</div>'
        f'<p class="sub" style="margin-top:12px">{auth_note}</p>'
        '<p class="sub" style="margin-bottom:4px">Quick test:</p>'
        f'<pre>{_esc(curl)}</pre>'
        '</div>'
    )


def _sessions_card(sessions: list[Any], url_for_session: LinkFn, json_for_session: LinkFn) -> str:
    if not sessions:
        return (
            '<div class="card"><h2>Sessions</h2>'
            '<div class="empty">No sessions yet. Send a request to '
            '<code class="inline">/v1/chat/completions</code> and refresh.</div></div>'
        )

    rows = []
    for s in sessions:
        usage = s.total_usage()
        rows.append(
            "<tr>"
            f'<td><a href="{_esc(url_for_session(s.id))}">{_esc(format_session_time(s.id))}</a>'
            f'<div class="sub mono">{_esc(s.id)}</div></td>'
            f"<td>{_badge_mode(s.mode)}</td>"
            f'<td><code class="inline">{_esc(s.model)}</code></td>'
            f'<td class="right">{len(s.turns)}</td>'
            f'<td class="right">{usage.get("total_tokens", 0)}</td>'
            f'<td><a href="{_esc(json_for_session(s.id))}">json</a></td>'
            "</tr>"
        )

    return (
        '<div class="card"><h2>Sessions</h2>'
        "<table><thead><tr>"
        "<th>Session</th><th>Mode</th><th>Model</th>"
        '<th class="right">Turns</th><th class="right">Tokens</th><th>Raw</th>'
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


# --------------------------------------------------------------------------- #
# Per-session report
# --------------------------------------------------------------------------- #

def render_session(
    session: Any,
    *,
    connection: dict[str, Any],
    index_url: str,
    json_url: str,
) -> str:
    usage = session.total_usage()
    head = [
        f'<div class="nav"><a href="{_esc(index_url)}">&larr; all sessions</a></div>',
        '<header class="top">'
        f'<h1>Session</h1>{_badge_mode(session.mode)}'
        f'<span class="sub">model <code class="inline">{_esc(session.model)}</code></span>'
        "</header>",
        f'<p class="sub mono">{_esc(session.id)}</p>',
        '<div class="card"><div class="kv">'
        f'<div class="k">Started</div><div>{_esc(format_session_time(session.id))}</div>'
        f'<div class="k">Created</div><div>{_esc(session.created)}</div>'
        f'<div class="k">Updated</div><div>{_esc(session.updated)}</div>'
        f'<div class="k">Turns</div><div>{len(session.turns)}</div>'
        f'<div class="k">Tokens</div><div>{usage.get("prompt_tokens", 0)} prompt · '
        f'{usage.get("completion_tokens", 0)} completion · {usage.get("total_tokens", 0)} total</div>'
        f'<div class="k">Raw log</div><div><a href="{_esc(json_url)}">{_esc(json_url)}</a></div>'
        "</div></div>",
    ]

    turns_html = [_render_turn(i, t) for i, t in enumerate(session.turns, start=1)]
    if not turns_html:
        turns_html = ['<div class="empty">No turns recorded.</div>']

    return _page(f"Session {session.id}", "\n".join(head + turns_html))


def _render_turn(n: int, turn: dict[str, Any]) -> str:
    req = turn.get("request", {}) or {}
    resp = turn.get("response", {}) or {}
    usage = resp.get("usage") or {}
    timing = resp.get("timing") or {}

    parts = [f'<div class="turn"><div class="turnhead"><span class="n">Turn {n}</span>']
    if req.get("stream"):
        parts.append('<span class="badge">stream</span>')
    if resp.get("error"):
        parts.append('<span class="badge err">error</span>')
    if resp.get("finish_reason"):
        parts.append(f'<span>finish: {_esc(resp["finish_reason"])}</span>')
    if usage.get("total_tokens"):
        parts.append(f'<span>{_esc(usage.get("total_tokens"))} tok</span>')
    if timing.get("seconds") is not None:
        parts.append(f'<span>{_esc(timing.get("seconds"))}s</span>')
    if resp.get("upstream_id"):
        parts.append(f'<span class="mono">upstream {_esc(resp["upstream_id"])}</span>')
    parts.append("</div>")

    # Incoming messages added this turn.
    for msg in req.get("new_messages", []) or []:
        parts.append(_render_message(msg))

    # The assistant reply, or the upstream error if this turn failed.
    if resp.get("error"):
        parts.append(_render_error(resp["error"]))
    else:
        parts.append(_render_message(resp.get("message") or {"role": "assistant", "content": None}))

    # Request params / tools (collapsed).
    if req.get("params"):
        parts.append(_details("request params", _pretty(req["params"])))
    if req.get("tools"):
        names = _tool_names(req["tools"])
        label = f"tools ({len(names)})" if names else "tools"
        parts.append(_details(label, _pretty(req["tools"])))

    parts.append("</div>")
    return "".join(parts)


def _render_message(msg: dict[str, Any]) -> str:
    role = msg.get("role", "?")
    cls = role if role in ("user", "assistant", "system", "tool") else "system"

    body_bits: list[str] = []
    raw_bits: list[str] = []
    content = _content_to_text(msg.get("content"))
    if content:
        body_bits.append(f'<div class="body">{_esc(content)}</div>')
        raw_bits.append(content)

    for tc in msg.get("tool_calls", []) or []:
        fn = (tc.get("function") or {})
        name = fn.get("name", "")
        args = _maybe_pretty_json(fn.get("arguments", ""))
        body_bits.append(
            '<div class="toolcall">'
            f'<span class="name">⚙ {_esc(name)}</span>'
            f"<pre>{_esc(args)}</pre>"
            "</div>"
        )
        raw_bits.append(f"{name}({args})" if name else args)

    if msg.get("tool_call_id"):
        body_bits.insert(0, f'<div class="sub">tool_call_id: {_esc(msg["tool_call_id"])}</div>')

    if not body_bits:
        body_bits.append('<div class="body sub">(empty)</div>')

    label = role
    if msg.get("name"):
        label = f"{role} · {msg['name']}"

    raw = "\n\n".join(raw_bits)
    copy_btn = f'<button class="copybtn" data-copy="{_esc(raw)}">Copy</button>'

    return (
        f'<div class="msg {cls}"><div class="role">'
        f'<span class="rolelabel">{_esc(label)}</span>{copy_btn}'
        "</div>"
        + "".join(body_bits)
        + "</div>"
    )


def _render_error(error: Any) -> str:
    """Render an upstream error body as a distinct, highlighted message block."""
    message = ""
    if isinstance(error, dict):
        inner = error.get("error") if isinstance(error.get("error"), dict) else error
        if isinstance(inner, dict):
            message = inner.get("message") or ""
    if not message:
        message = _content_to_text(error) or "upstream error"

    detail = _details("error detail", _pretty(error))
    return (
        '<div class="msg error"><div class="role">'
        '<span class="rolelabel">upstream error</span>'
        "</div>"
        f'<div class="body">{_esc(message)}</div>'
        f"{detail}"
        "</div>"
    )


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def format_session_time(session_id: str) -> str:
    """Turn ``20260630T173117Z-3a86cdc0`` into ``2026-06-30 17:31:17 UTC``."""
    stamp = session_id.split("-", 1)[0]
    try:
        date, t = stamp.split("T")
        t = t.rstrip("Z")
        return f"{date[:4]}-{date[4:6]}-{date[6:8]} {t[:2]}:{t[2:4]}:{t[4:6]} UTC"
    except (ValueError, IndexError):
        return session_id


def _content_to_text(content: Any) -> str:
    """OpenAI message content may be a string or a list of typed parts."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text" and "text" in part:
                    out.append(part["text"])
                else:
                    out.append(f"[{part.get('type', 'part')}]")
            else:
                out.append(str(part))
        return "\n".join(out)
    return str(content)


def _tool_names(tools: Any) -> list[str]:
    names = []
    for t in tools or []:
        fn = (t or {}).get("function") or {}
        if fn.get("name"):
            names.append(fn["name"])
    return names


def _details(summary: str, pre_text: str) -> str:
    return f"<details><summary>{_esc(summary)}</summary><pre>{_esc(pre_text)}</pre></details>"


def _pretty(value: Any) -> str:
    try:
        return json.dumps(value, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _maybe_pretty_json(text: Any) -> str:
    if not isinstance(text, str):
        return _pretty(text)
    try:
        return json.dumps(json.loads(text), indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        return text


# --------------------------------------------------------------------------- #
# Static export
# --------------------------------------------------------------------------- #

def export_static(sessions: Iterable[Any], out_dir: str, connection: dict[str, Any]) -> list[str]:
    """Write ``index.html`` plus one ``<id>.html`` and ``<id>.json`` per session
    into ``out_dir``. Returns the list of written paths.

    Links between pages are relative, so the directory can be opened straight from
    disk or served by any static file host.
    """
    sessions = list(sessions)
    os.makedirs(out_dir, exist_ok=True)
    written: list[str] = []

    index_html = render_index(
        sessions,
        connection=connection,
        url_for_session=lambda sid: f"{sid}.html",
        json_for_session=lambda sid: f"{sid}.json",
    )
    index_path = os.path.join(out_dir, "index.html")
    _write(index_path, index_html)
    written.append(index_path)

    for s in sessions:
        page = render_session(
            s,
            connection=connection,
            index_url="index.html",
            json_url=f"{s.id}.json",
        )
        html_path = os.path.join(out_dir, f"{s.id}.html")
        _write(html_path, page)
        written.append(html_path)

        json_path = os.path.join(out_dir, f"{s.id}.json")
        _write(json_path, json.dumps(s.to_dict(), indent=2, ensure_ascii=False))
        written.append(json_path)

    return written


def _write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


__all__ = ["render_index", "render_session", "format_session_time", "export_static"]
