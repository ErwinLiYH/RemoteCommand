from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

import uvicorn

from remote_command_server.app import create_app
from remote_command_server.config import Settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="remote-command-server",
        description=(
            "Run local commands through an authenticated HTTP API with "
            "reconnectable NDJSON output."
        ),
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--working-directory",
        type=Path,
        default=Path.cwd(),
        help="Default directory used to run commands (default: current directory)",
    )
    parser.add_argument(
        "--state-directory",
        type=Path,
        default=Path.home() / ".remote-command",
        help="Directory used for command metadata and event logs",
    )
    parser.add_argument(
        "--shell",
        default="/bin/sh",
        help="Shell used as '<shell> -c <command>'",
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        help=(
            "Read the bearer token from this file. Otherwise "
            "REMOTE_COMMAND_TOKEN is used."
        ),
    )
    parser.add_argument(
        "--retention-hours",
        type=float,
        default=168,
        help="Hours to retain completed command logs (default: 168)",
    )
    parser.add_argument(
        "--cleanup-interval-seconds",
        type=float,
        default=3600,
    )
    parser.add_argument(
        "--terminate-grace-seconds",
        type=float,
        default=5,
    )
    parser.add_argument(
        "--heartbeat-seconds",
        type=float,
        default=15,
        help="Interval for transient stream heartbeat events",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug"],
    )
    return parser


def load_token(token_file: Path | None) -> str:
    if token_file is not None:
        try:
            token = token_file.expanduser().read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError(f"Unable to read token file: {exc}") from exc
    else:
        token = os.environ.get("REMOTE_COMMAND_TOKEN", "").strip()
    if not token:
        raise ValueError(
            "A bearer token is required. Set REMOTE_COMMAND_TOKEN or "
            "provide --token-file."
        )
    return token


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        token = load_token(args.token_file)
        settings = Settings(
            token=token,
            working_directory=args.working_directory,
            state_directory=args.state_directory,
            shell=args.shell,
            retention_seconds=args.retention_hours * 60 * 60,
            cleanup_interval_seconds=args.cleanup_interval_seconds,
            terminate_grace_seconds=args.terminate_grace_seconds,
            stream_heartbeat_seconds=args.heartbeat_seconds,
        ).normalized()
    except ValueError as exc:
        parser.error(str(exc))

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run(
        create_app(settings),
        host=args.host,
        port=args.port,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()

