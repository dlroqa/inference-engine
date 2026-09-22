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

    bk = sub.add_parser(
        "backup", parents=[common], help="Back up the database + config to a tarball."
    )
    bk.add_argument(
        "--out",
        type=Path,
        default=Path("."),
        help="Output directory or .tar.gz file path (default: current directory).",
    )
    rs = sub.add_parser(
        "restore", parents=[common], help="Restore the database + config from a backup tarball."
    )
    rs.add_argument("--from", dest="archive", type=Path, required=True, help="Backup tarball path.")
    rs.add_argument(
        "--force", action="store_true", help="Overwrite an existing database if present."
    )

    gen = sub.add_parser(
        "generate",
        parents=[common],
        help="Load the configured GGUF model and stream a completion.",
    )
    gen.add_argument("--model-path", type=Path, default=None, help="Path to a GGUF model file.")
    gen.add_argument("--prompt", required=True, help="Prompt text to generate from.")
    gen.add_argument("--max-tokens", type=int, default=256, help="Maximum tokens to generate.")
    gen.add_argument("--temperature", type=float, default=0.8, help="Sampling temperature.")

    keys = sub.add_parser("keys", help="Manage API keys.")
    keys_sub = keys.add_subparsers(dest="keys_command", required=True)
    kc = keys_sub.add_parser("create", parents=[common], help="Create an API key (shown once).")
    kc.add_argument("--label", default=None, help="Human-readable label for the key.")
    keys_sub.add_parser("list", parents=[common], help="List API keys (no secrets).")
    kr = keys_sub.add_parser("revoke", parents=[common], help="Revoke an API key by id.")
    kr.add_argument("key_id", help="The key id to revoke.")
    return parser


def _settings_from_args(args: argparse.Namespace) -> Settings:
    overrides = {
        "host": getattr(args, "host", None),
        "port": getattr(args, "port", None),
        "log_level": getattr(args, "log_level", None),
        "data_dir": getattr(args, "data_dir", None),
        "db_path": getattr(args, "db_path", None),
        "model_path": getattr(args, "model_path", None),
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


def _cmd_backup(settings: Settings, args: argparse.Namespace) -> int:
    from engine.backup import create_backup

    try:
        archive = create_backup(settings, args.out)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps({"archive": str(archive)}))
    return 0


def _cmd_restore(settings: Settings, args: argparse.Namespace) -> int:
    from engine.backup import restore_backup

    try:
        result = restore_backup(args.archive, settings, force=args.force)
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "db_path": str(result.db_path),
                "config_path": str(result.config_path) if result.config_path else None,
                "engine_version": result.manifest.get("engine_version"),
            }
        )
    )
    return 0


def _ensure_migrated(settings: Settings) -> None:
    from engine.store.db import connect
    from engine.store.migrations import apply_migrations

    conn = connect(settings.db_path)  # type: ignore[arg-type]
    try:
        apply_migrations(conn)
    finally:
        conn.close()


def _cmd_keys(settings: Settings, args: argparse.Namespace) -> int:
    from engine.auth.keys import KeyStore

    _ensure_migrated(settings)
    store = KeyStore(settings.db_path)  # type: ignore[arg-type]

    if args.keys_command == "create":
        record, token = store.create(label=args.label)
        print(
            json.dumps(
                {"id": record.id, "prefix": record.prefix, "label": record.label, "token": token}
            )
        )
        print("Save this token now; it will not be shown again.", file=sys.stderr)
        return 0
    if args.keys_command == "list":
        rows = [
            {
                "id": r.id,
                "prefix": r.prefix,
                "label": r.label,
                "created_at": r.created_at,
                "last_used_at": r.last_used_at,
                "revoked": r.revoked,
            }
            for r in store.list()
        ]
        print(json.dumps(rows, indent=2))
        return 0
    if args.keys_command == "revoke":
        ok = store.revoke(args.key_id)
        print(json.dumps({"revoked": ok, "id": args.key_id}))
        return 0 if ok else 1
    return 2  # pragma: no cover


def _cmd_generate(settings: Settings, args: argparse.Namespace) -> int:
    import asyncio

    from engine.inference import GenerationRequest, build_backend
    from engine.inference.types import BackendError

    async def run() -> int:
        backend = build_backend(settings)
        await backend.load()
        try:
            request = GenerationRequest(
                prompt=args.prompt,
                max_tokens=args.max_tokens,
                temperature=args.temperature,
            )
            stream = backend.generate(request)
            try:
                async for chunk in stream:
                    sys.stdout.write(chunk.text)
                    sys.stdout.flush()
            finally:
                await stream.aclose()
            sys.stdout.write("\n")
            result = stream.result
            assert result is not None
            print(
                json.dumps(
                    {
                        "finish_reason": result.finish_reason.value,
                        "prompt_tokens": result.prompt_tokens,
                        "completion_tokens": result.completion_tokens,
                        "ttft_ms": round(result.timings.ttft_ms or 0.0, 1),
                        "total_ms": round(result.timings.total_ms or 0.0, 1),
                    }
                ),
                file=sys.stderr,
            )
        finally:
            await backend.unload()
        return 0

    try:
        return asyncio.run(run())
    except BackendError as exc:
        print(f"Generation error: {exc}", file=sys.stderr)
        return 1


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
    if args.command == "backup":
        return _cmd_backup(settings, args)
    if args.command == "restore":
        return _cmd_restore(settings, args)
    if args.command == "generate":
        return _cmd_generate(settings, args)
    if args.command == "keys":
        return _cmd_keys(settings, args)
    parser.error(f"unknown command: {args.command}")
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
