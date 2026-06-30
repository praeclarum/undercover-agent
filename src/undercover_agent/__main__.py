"""``python -m undercover_agent`` entrypoint: parse CLI flags and run the server, or
export static HTML reports from the logs."""

from __future__ import annotations

import argparse
import sys

from .config import (
    DEFAULT_HOST,
    DEFAULT_LOGS_DIR,
    DEFAULT_MODEL,
    DEFAULT_PORT,
    DEFAULT_REPORTS_DIR,
    Config,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="undercover_agent", description=__doc__)
    sub = parser.add_subparsers(dest="command")

    # Default command: run the server.
    parser.add_argument("--mode", choices=["dumb", "mitm"], default="dumb")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--logs-dir", default=DEFAULT_LOGS_DIR)
    parser.add_argument("--base-url", default=None, help="upstream base URL (mitm)")
    parser.add_argument("--api-key", default=None, help="upstream API key (mitm)")

    # `report` subcommand: static HTML export from the logs.
    report_cmd = sub.add_parser(
        "report", help="generate static HTML reports from the logs directory"
    )
    report_cmd.add_argument("--logs-dir", default=DEFAULT_LOGS_DIR)
    report_cmd.add_argument("--out-dir", default=DEFAULT_REPORTS_DIR)
    report_cmd.add_argument("--model", default=DEFAULT_MODEL)
    report_cmd.add_argument("--mode", choices=["dumb", "mitm"], default="dumb")
    return parser


def config_from_args(args: argparse.Namespace) -> Config:
    return Config(
        mode=args.mode,
        host=args.host,
        port=args.port,
        model=args.model,
        logs_dir=args.logs_dir,
        base_url=args.base_url,
        api_key=args.api_key,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "report":
        return _run_report(args)

    from .server import create_app

    config = config_from_args(args)

    if config.mode == "mitm":
        missing = [
            flag
            for flag, value in (("--base-url", config.base_url), ("--api-key", config.api_key))
            if not value
        ]
        if missing:
            parser_prog = "undercover_agent"
            print(
                f"{parser_prog}: error: --mode mitm requires "
                f"{' and '.join(missing)}",
                file=sys.stderr,
            )
            return 2

    app = create_app(config)
    print(
        f"Undercover Agent [{config.mode}] listening on "
        f"http://{config.host}:{config.port}  (model={config.model})"
    )
    app.run(host=config.host, port=config.port, threaded=True)
    return 0


def _run_report(args: argparse.Namespace) -> int:
    from . import report
    from . import session_log

    store = session_log.SessionStore(args.logs_dir, mode=args.mode, model=args.model)
    sessions = store.list_sessions()
    connection = {
        "mode": args.mode,
        "model": args.model,
        "base_url": "http://localhost:8000/v1",
        "chat_url": "http://localhost:8000/v1/chat/completions",
        "models_url": "http://localhost:8000/v1/models",
        "health_url": "http://localhost:8000/health",
    }
    written = report.export_static(sessions, args.out_dir, connection)
    print(
        f"Wrote {len(written)} files for {len(sessions)} session(s) to "
        f"{args.out_dir}/  (open {args.out_dir}/index.html)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
