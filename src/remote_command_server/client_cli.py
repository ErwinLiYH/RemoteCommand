from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Any

import httpx

from remote_command_server.client import RemoteCommandClient

OUTPUT_EVENT_TYPES = {"stdout", "stderr", "output"}
COMMAND_STATUSES = (
    "queued",
    "running",
    "terminating",
    "exited",
    "failed",
    "cancelled",
    "lost",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="remote-command",
        description="Run and manage commands on a RemoteCommand Server.",
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument(
        "--command",
        help='Command to run, for example: --command "ls -la"',
    )
    action.add_argument(
        "--reconnect",
        metavar="COMMAND_ID",
        help="Replay and follow an existing command",
    )
    action.add_argument(
        "--list",
        action="store_true",
        help="List retained commands",
    )
    action.add_argument(
        "--get",
        metavar="COMMAND_ID",
        help="Show one command's details",
    )
    action.add_argument(
        "--cancel",
        metavar="COMMAND_ID",
        help="Cancel a running command",
    )
    action.add_argument(
        "--cleanup",
        action="store_true",
        help="Delete all completed command records and event logs",
    )
    action.add_argument(
        "--health",
        action="store_true",
        help="Check server health; a token is not required",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get(
            "REMOTE_COMMAND_URL", "http://127.0.0.1:8000"
        ),
        help=(
            "RemoteCommand Server URL "
            "(default: REMOTE_COMMAND_URL or http://127.0.0.1:8000)"
        ),
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("REMOTE_COMMAND_TOKEN"),
        help="Bearer token (default: REMOTE_COMMAND_TOKEN)",
    )
    parser.add_argument(
        "--working-directory",
        help="Override the server's default working directory",
    )
    parser.add_argument(
        "--command-id",
        help="Choose the ID for a newly submitted command",
    )
    parser.add_argument(
        "--pty",
        action="store_true",
        help="Run the new command in PTY mode",
    )
    parser.add_argument(
        "--after-seq",
        type=int,
        default=0,
        help="Reconnect after this event sequence number (default: 0)",
    )
    parser.add_argument(
        "--no-follow",
        action="store_true",
        help="On reconnect, print existing output and return immediately",
    )
    parser.add_argument(
        "--status",
        choices=COMMAND_STATUSES,
        help="With --list, only show commands in this status",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON; streams use one JSON event per line",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Hide local command ID, PID and status messages for streams",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _validate_arguments(parser, args)

    client = RemoteCommandClient(args.url, args.token)
    try:
        if args.health:
            return _show_health(client, as_json=args.json)
        if args.list:
            return _list_commands(
                client, status_filter=args.status, as_json=args.json
            )
        if args.get:
            return _show_command(client, args.get, as_json=args.json)
        if args.cancel:
            return _cancel_command(client, args.cancel, as_json=args.json)
        if args.cleanup:
            return _cleanup_commands(client, as_json=args.json)
        return _stream_command(client, args)
    except (httpx.HTTPError, ValueError) as exc:
        print(f"[remote-command] request failed: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()


def _validate_arguments(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> None:
    if not args.health and not args.token:
        parser.error(
            "a token is required; use --token or set REMOTE_COMMAND_TOKEN"
        )
    if args.after_seq < 0:
        parser.error("--after-seq must be zero or greater")

    is_command = args.command is not None
    is_reconnect = args.reconnect is not None
    is_stream = is_command or is_reconnect

    if not is_command and (
        args.working_directory or args.command_id or args.pty
    ):
        parser.error(
            "--working-directory, --command-id and --pty are only valid "
            "with --command"
        )
    if not is_reconnect and (args.after_seq != 0 or args.no_follow):
        parser.error(
            "--after-seq and --no-follow are only valid with --reconnect"
        )
    if not args.list and args.status:
        parser.error("--status is only valid with --list")
    if not is_stream and args.quiet:
        parser.error("--quiet is only valid with --command or --reconnect")


def _stream_command(
    client: RemoteCommandClient, args: argparse.Namespace
) -> int:
    command_id = args.reconnect or args.command_id
    last_seq = args.after_seq
    return_code: int | None = None

    try:
        if args.command is not None:
            events = client.run_command(
                args.command,
                command_id=args.command_id,
                working_directory=args.working_directory,
                pty=args.pty,
            )
        else:
            events = client.events(
                args.reconnect,
                after_seq=args.after_seq,
                follow=not args.no_follow,
            )

        for event in events:
            command_id = event.get("command_id", command_id)
            last_seq = max(last_seq, int(event.get("seq", last_seq)))
            event_type = event.get("type")

            if args.json:
                _print_json(event, compact=True)
            elif event_type == "started" and not args.quiet:
                pid = event.get("pid")
                print(
                    f"[remote-command] command_id={command_id} pid={pid}",
                    file=sys.stderr,
                    flush=True,
                )
            elif event_type in OUTPUT_EVENT_TYPES:
                _write_output_event(event)
            elif event_type == "error":
                message = event.get("message", "unknown remote error")
                print(
                    f"[remote-command] error: {message}",
                    file=sys.stderr,
                    flush=True,
                )
            elif event_type == "exit":
                if not args.quiet:
                    status = event.get("status", "unknown")
                    print(
                        f"[remote-command] status={status} "
                        f"return_code={event.get('return_code')}",
                        file=sys.stderr,
                        flush=True,
                    )

            if event_type == "exit":
                return_code = event.get("return_code")
    except KeyboardInterrupt:
        if not args.quiet:
            print(
                "\n[remote-command] stream disconnected; "
                "the remote command continues running.",
                file=sys.stderr,
            )
            _print_reconnect_hint(
                command_id, last_seq, args.url, file=sys.stderr
            )
        return 130
    except (httpx.HTTPError, ValueError):
        if command_id and not args.quiet:
            _print_reconnect_hint(
                command_id, last_seq, args.url, file=sys.stderr
            )
        raise

    if return_code is None:
        return 0
    return _shell_exit_code(return_code)


def _show_health(
    client: RemoteCommandClient, *, as_json: bool
) -> int:
    health = client.health()
    if as_json:
        _print_json(health)
    else:
        print(f"status: {health.get('status', 'unknown')}")
        print(
            "working_directory: "
            f"{health.get('working_directory', '-')}"
        )
    return 0


def _list_commands(
    client: RemoteCommandClient,
    *,
    status_filter: str | None,
    as_json: bool,
) -> int:
    commands = client.list_commands()
    if status_filter:
        commands = [
            command
            for command in commands
            if command.get("status") == status_filter
        ]

    if as_json:
        _print_json(commands)
    else:
        _print_command_table(commands)
    return 0


def _show_command(
    client: RemoteCommandClient,
    command_id: str,
    *,
    as_json: bool,
) -> int:
    command = client.get_command(command_id)
    if as_json:
        _print_json(command)
    else:
        _print_command_details(command)
    return 0


def _cancel_command(
    client: RemoteCommandClient,
    command_id: str,
    *,
    as_json: bool,
) -> int:
    command = client.cancel_command(command_id)
    if as_json:
        _print_json(command)
    else:
        print(
            f"command_id={command.get('command_id', command_id)} "
            f"status={command.get('status', 'unknown')} "
            f"return_code={_display_value(command.get('return_code'))}"
        )
    return 0


def _cleanup_commands(
    client: RemoteCommandClient, *, as_json: bool
) -> int:
    result = client.cleanup_commands()
    if as_json:
        _print_json(result)
    else:
        cleaned_commands = result.get("cleaned_commands", [])
        print(f"cleaned: {result.get('count', len(cleaned_commands))}")
        for command_id in cleaned_commands:
            print(f"- {command_id}")
    return 0


def _print_command_table(commands: list[dict[str, Any]]) -> None:
    if not commands:
        print("No commands found.")
        return

    headers = ("COMMAND ID", "STATUS", "PID", "RETURN", "STARTED", "COMMAND")
    rows = [
        (
            str(command.get("command_id", "-")),
            str(command.get("status", "-")),
            _display_value(command.get("pid")),
            _display_value(command.get("return_code")),
            _format_timestamp(command.get("started_at")),
            _single_line(str(command.get("command", "")), limit=60),
        )
        for command in commands
    ]
    widths = [
        min(
            max(len(headers[index]), *(len(row[index]) for row in rows)),
            60 if index == 5 else 36,
        )
        for index in range(len(headers))
    ]
    print(
        "  ".join(
            header.ljust(widths[index])
            for index, header in enumerate(headers)
        )
    )
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print(
            "  ".join(
                _single_line(value, limit=widths[index]).ljust(widths[index])
                for index, value in enumerate(row)
            ).rstrip()
        )


def _print_command_details(command: dict[str, Any]) -> None:
    fields = (
        ("command_id", "command_id"),
        ("status", "status"),
        ("pid", "pid"),
        ("return_code", "return_code"),
        ("pty", "pty"),
        ("working_directory", "working_directory"),
        ("created_at", "created_at"),
        ("started_at", "started_at"),
        ("finished_at", "finished_at"),
        ("last_seq", "last_seq"),
        ("command", "command"),
    )
    for label, key in fields:
        print(f"{label}: {_display_value(command.get(key))}")


def _write_output_event(event: dict[str, Any]) -> None:
    stream = (
        sys.stderr if event.get("type") == "stderr" else sys.stdout
    )
    stream.write(str(event.get("data", "")))
    stream.flush()


def _print_json(value: Any, *, compact: bool = False) -> None:
    if compact:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        text = json.dumps(value, ensure_ascii=False, indent=2)
    print(text, flush=True)


def _print_reconnect_hint(
    command_id: str | None,
    last_seq: int,
    url: str,
    *,
    file: Any,
) -> None:
    if not command_id:
        return
    print(
        "[remote-command] reconnect with: "
        f"python remote_command.py --url {url!r} "
        f"--reconnect {command_id!r} --after-seq {last_seq}",
        file=file,
        flush=True,
    )


def _display_value(value: Any) -> str:
    return "-" if value is None else str(value)


def _format_timestamp(value: Any) -> str:
    if not value:
        return "-"
    try:
        timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    return timestamp.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _single_line(value: str, *, limit: int) -> str:
    normalized = " ".join(value.splitlines())
    if len(normalized) <= limit:
        return normalized
    if limit <= 3:
        return normalized[:limit]
    return normalized[: limit - 3] + "..."


def _shell_exit_code(return_code: int) -> int:
    if return_code < 0:
        return min(255, 128 + abs(return_code))
    return min(255, return_code)


if __name__ == "__main__":
    raise SystemExit(main())
