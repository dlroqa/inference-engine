"""Command-line interface: ``inference-engine``.

Subcommands:
    serve     Start the HTTP server (Uvicorn).
    migrate   Apply database migrations and exit.
    config    Print the resolved configuration and exit.
    version   Print the version and exit.

CLI flags are the highest-precedence configuration layer; unset flags fall
through to IE_* environment variables, then the config file, then defaults.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from engine import __version__
from engine.config import ConfigError, Settings, load_config


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="inference-engine",
        description="Self-hosted inference engine (Block 0 foundation).",
    )
    parser.add_argument("--version", action="version", version=f"inference-engine {__version__}")

    # Shared config flags applied to every subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=None, help="Path to a TOML config file.")
    common.add_argument("--host", default=None, help="Bind host (default 127.0.0.1).")
    common.add_argument("--port", type=int, default=None, help="Bind port (default 8000).")
    common.add_argument(
        "--log-level",
        default=None,
        choices=["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"],
        help="Logging level.",
    )
    common.add_argument("--data-dir", type=Path, default=None, help="Base data directory.")
    common.add_argument("--db-path", type=Path, default=None, help="SQLite database file path.")

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", parents=[common], help="Start the HTTP server.")
    sub.add_parser("migrate", parents=[common], help="Apply database migrations and exit.")
    sub.add_parser("config", parents=[common], help="Print resolved configuration and exit.")
    sub.add_parser("version", help="Print the version and exit.")
    return parser


def _settings_from_args(args: argparse.Namespace) -> Settings:
    overrides = {
        "host": getattr(args, "host", None),
        "port": getattr(args, "port", None),
        "log_level": getattr(args, "log_level", None),
        "data_dir": getattr(args, "data_dir", None),
        "db_path": getattr(args, "db_path", None),
    }
    return load_config(cli_overrides=overrides, config_file=getattr(args, "config", None))


def _cmd_serve(settings: Settings) -> int:
    import uvicorn

    from engine.main import create_app

    app = create_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, log_config=None)
    return 0


def _cmd_migrate(settings: Settings) -> int:
    from engine.store.db import connect
    from engine.store.migrations import apply_migrations

    conn = connect(settings.db_path)  # type: ignore[arg-type]
    try:
        applied = apply_migrations(conn)
    finally:
        conn.close()
    print(json.dumps({"newly_applied": applied, "db_path": str(settings.db_path)}))
    return 0


def _cmd_config(settings: Settings) -> int:
    print(settings.model_dump_json(indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "version":
        print(__version__)
        return 0

    try:
        settings = _settings_from_args(args)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.command == "serve":
        return _cmd_serve(settings)
    if args.command == "migrate":
        return _cmd_migrate(settings)
    if args.command == "config":
        return _cmd_config(settings)
    parser.error(f"unknown command: {args.command}")
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
