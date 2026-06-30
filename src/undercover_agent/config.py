"""Runtime configuration for Undercover Agent.

Kept in its own module so that both the server and the mode backends can import
it without creating an import cycle (the backends need the config, and the server
imports the backends).
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_MODEL = "undercover-agent"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000
DEFAULT_LOGS_DIR = "logs"
DEFAULT_REPORTS_DIR = "reports"


@dataclass
class Config:
    """All knobs that control a running Undercover Agent instance."""

    mode: str = "dumb"  # "dumb" | "mitm"
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT

    # MITM upstream (passed straight to the OpenAI SDK client).
    base_url: str | None = None
    api_key: str | None = None

    # Identity advertised to the harness via /v1/models and in responses.
    model: str = DEFAULT_MODEL

    # Where per-session JSON logs are written.
    logs_dir: str = DEFAULT_LOGS_DIR
    reports_dir: str = DEFAULT_REPORTS_DIR
