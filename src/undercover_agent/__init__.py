"""Undercover Agent: an OpenAI-compatible chat-completions server that logs harness
traffic, either in "dumb" mode (canned responses) or "mitm" mode (forwarding to a
real upstream LLM).

The public surface intentionally stays small; see :mod:`undercover_agent.server` for
the Flask app and request/response dataflow.
"""

from .config import Config

__all__ = ["Config", "__version__"]

__version__ = "0.1.0"
