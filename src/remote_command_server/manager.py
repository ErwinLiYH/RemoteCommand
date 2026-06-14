from __future__ import annotations

import asyncio
import codecs
import contextlib
import errno
import fcntl
import json
import logging
import os
import pty
import re
import shutil
import signal
import struct
import termios
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import AsyncIterator, TextIO

from remote_command_server.config import Settings
from remote_command_server.models import CommandEvent, CommandRecord, utc_now

logger = logging.getLogger(__name__)

COMMAND_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
TERMINAL_STATUSES = {"exited", "failed", "cancelled", "lost"}


class CommandNotFoundError(KeyError):
    pass


class CommandConflictError(ValueError):
    pass


class InvalidWorkingDirectoryError(ValueError):
    pass


@dataclass(slots=True)
class CommandRuntime:
    record: CommandRecord
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    version: int = 0
    process: asyncio.subprocess.Process | None = None
    task: asyncio.Task[None] | None = None
    cancel_requested: bool = False
    master_fd: int | None = None
    event_file: TextIO | None = None


class CommandManager:
    def __init__(self, settings: Settings):
        self.settings = settings.normalized()
        self.commands_directory = self.settings.state_directory / "commands"
        self._records: dict[str, CommandRecord] = {}
        self._runtimes: dict[str, CommandRuntime] = {}
        self._lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task[None] | None = None
        self._stopping = False

    async def start(self) -> None:
        if os.name != "posix":
            raise RuntimeError("RemoteCommand Server currently requires a POSIX system")
        self.commands_directory.mkdir(parents=True, exist_ok=True)
        await self._load_existing_records()
        await self.cleanup_once()
        self._cleanup_task = asyncio.create_task(
            self._cleanup_loop(), name="remote-command-cleanup"
        )

    async def stop(self) -> None:
        self._stopping = True
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cleanup_task

        active_ids = [
            command_id
            for command_id, runtime in self._runtimes.items()
            if not runtime.done.is_set()
        ]
        if active_ids:
            await asyncio.gather(
                *(self.cancel_command(command_id) for command_id in active_ids),
                return_exceptions=True,
            )

    async def create_command(
        self,
        command: str,
        *,
        command_id: str | None = None,
        working_directory: str | None = None,
        use_pty: bool = False,
    ) -> CommandRecord:
        if self._stopping:
            raise RuntimeError("Server is shutting down")

        resolved_id = command_id or uuid.uuid4().hex
        if not COMMAND_ID_PATTERN.fullmatch(resolved_id):
            raise ValueError(
                "command_id must start with an alphanumeric character and contain "
                "only letters, digits, '.', '_' or '-' (maximum 128 characters)"
            )
        cwd = self._resolve_working_directory(working_directory)

        async with self._lock:
            if resolved_id in self._records:
                raise CommandConflictError(
                    f"Command ID already exists: {resolved_id}"
                )
            command_directory = self._command_directory(resolved_id)
            command_directory.mkdir(parents=True, exist_ok=False)
            record = CommandRecord(
                command_id=resolved_id,
                command=command,
                working_directory=str(cwd),
                pty=use_pty,
                status="queued",
                created_at=utc_now(),
            )
            self._records[resolved_id] = record
            runtime = CommandRuntime(record=record)
            self._runtimes[resolved_id] = runtime
            self._write_metadata(record)
            runtime.task = asyncio.create_task(
                self._execute(runtime), name=f"remote-command-{resolved_id}"
            )

        return record.model_copy(deep=True)

    def get_command(self, command_id: str) -> CommandRecord:
        record = self._records.get(command_id)
        if record is None:
            raise CommandNotFoundError(command_id)
        return record.model_copy(deep=True)

    def list_commands(self) -> list[CommandRecord]:
        return [
            record.model_copy(deep=True)
            for record in sorted(
                self._records.values(),
                key=lambda item: item.created_at,
                reverse=True,
            )
        ]

    async def cancel_command(self, command_id: str) -> CommandRecord:
        record = self._records.get(command_id)
        if record is None:
            raise CommandNotFoundError(command_id)
        if record.status in TERMINAL_STATUSES:
            return record.model_copy(deep=True)

        runtime = self._runtimes.get(command_id)
        if runtime is None:
            raise CommandConflictError(
                f"Command {command_id} is not attached to this server process"
            )

        runtime.cancel_requested = True
        record.status = "terminating"
        self._write_metadata(record)

        process = runtime.process
        if process is not None and process.returncode is None:
            self._signal_process_group(process.pid, signal.SIGTERM)

        try:
            await asyncio.wait_for(
                runtime.done.wait(), timeout=self.settings.terminate_grace_seconds
            )
        except TimeoutError:
            process = runtime.process
            if process is not None and process.returncode is None:
                self._signal_process_group(process.pid, signal.SIGKILL)
            await runtime.done.wait()

        return self.get_command(command_id)

    async def iter_events(
        self,
        command_id: str,
        *,
        after_seq: int = 0,
        follow: bool = True,
    ) -> AsyncIterator[bytes]:
        if command_id not in self._records:
            raise CommandNotFoundError(command_id)

        current_seq = max(0, after_seq)
        file_offset = 0
        while True:
            runtime = self._runtimes.get(command_id)
            observed_version = runtime.version if runtime is not None else 0
            events, file_offset = await asyncio.to_thread(
                self._read_events_from_offset,
                command_id,
                file_offset,
                current_seq,
            )
            for event in events:
                current_seq = max(current_seq, event.seq)
                yield self._serialize_event(event)

            record = self._records[command_id]
            if (
                record.status in TERMINAL_STATUSES
                and current_seq >= record.last_seq
            ):
                return
            if not follow:
                return

            runtime = self._runtimes.get(command_id)
            if runtime is None:
                await asyncio.sleep(0.1)
                continue

            async with runtime.condition:
                if runtime.version != observed_version:
                    continue
                try:
                    await asyncio.wait_for(
                        runtime.condition.wait(),
                        timeout=self.settings.stream_heartbeat_seconds,
                    )
                except TimeoutError:
                    heartbeat = CommandEvent(
                        command_id=command_id,
                        seq=current_seq,
                        type="heartbeat",
                        timestamp=utc_now(),
                        transient=True,
                    )
                    yield self._serialize_event(heartbeat)

    async def cleanup_once(self) -> list[str]:
        if self.settings.retention_seconds == 0:
            cutoff = utc_now()
        else:
            cutoff = utc_now() - timedelta(
                seconds=self.settings.retention_seconds
            )

        removed: list[str] = []
        async with self._lock:
            for command_id, record in list(self._records.items()):
                if record.status not in TERMINAL_STATUSES:
                    continue
                finished_at = record.finished_at or record.created_at
                if finished_at > cutoff:
                    continue
                self._records.pop(command_id, None)
                self._runtimes.pop(command_id, None)
                removed.append(command_id)

        for command_id in removed:
            await asyncio.to_thread(
                shutil.rmtree,
                self._command_directory(command_id),
                True,
            )
        return removed

    async def _execute(self, runtime: CommandRuntime) -> None:
        record = runtime.record
        event_path = self._events_path(record.command_id)
        event_path.parent.mkdir(parents=True, exist_ok=True)
        runtime.event_file = event_path.open("a", encoding="utf-8", buffering=1)

        try:
            if record.pty:
                process, master_fd = await self._spawn_pty(record)
                runtime.master_fd = master_fd
            else:
                process = await self._spawn_pipes(record)

            runtime.process = process
            record.status = "running"
            record.pid = process.pid
            record.started_at = utc_now()
            self._write_metadata(record)
            await self._emit(
                runtime,
                "started",
                pid=process.pid,
                status="running",
                working_directory=record.working_directory,
                pty=record.pty,
            )
            if runtime.cancel_requested and process.returncode is None:
                self._signal_process_group(process.pid, signal.SIGTERM)

            queue: asyncio.Queue[tuple[str, str | None]] = asyncio.Queue(
                maxsize=256
            )
            if record.pty:
                assert runtime.master_fd is not None
                pumps = [
                    asyncio.create_task(
                        self._pump_pty(runtime.master_fd, queue),
                        name=f"pty-reader-{record.command_id}",
                    )
                ]
            else:
                assert process.stdout is not None
                assert process.stderr is not None
                pumps = [
                    asyncio.create_task(
                        self._pump_stream(process.stdout, "stdout", queue),
                        name=f"stdout-reader-{record.command_id}",
                    ),
                    asyncio.create_task(
                        self._pump_stream(process.stderr, "stderr", queue),
                        name=f"stderr-reader-{record.command_id}",
                    ),
                ]

            open_streams = len(pumps)
            while open_streams:
                event_type, data = await queue.get()
                if data is None:
                    open_streams -= 1
                elif data:
                    await self._emit(runtime, event_type, data=data)

            await asyncio.gather(*pumps)
            return_code = await process.wait()
            record.return_code = return_code
            record.finished_at = utc_now()
            terminal_status = (
                "cancelled" if runtime.cancel_requested else "exited"
            )
            await self._emit(
                runtime,
                "exit",
                status=terminal_status,
                return_code=return_code,
                final_status=terminal_status,
            )
        except Exception as exc:
            logger.exception("Command %s failed", record.command_id)
            if (
                runtime.process is not None
                and runtime.process.returncode is None
            ):
                self._signal_process_group(runtime.process.pid, signal.SIGKILL)
                with contextlib.suppress(Exception):
                    await runtime.process.wait()
            terminal_status = (
                "cancelled" if runtime.cancel_requested else "failed"
            )
            record.finished_at = utc_now()
            if runtime.process is not None:
                record.return_code = runtime.process.returncode
            await self._emit(runtime, "error", message=str(exc))
            await self._emit(
                runtime,
                "exit",
                status=terminal_status,
                return_code=record.return_code,
                final_status=terminal_status,
            )
        finally:
            if runtime.master_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(runtime.master_fd)
                runtime.master_fd = None
            if runtime.event_file is not None:
                runtime.event_file.close()
                runtime.event_file = None
            runtime.done.set()
            async with runtime.condition:
                runtime.version += 1
                runtime.condition.notify_all()

    async def _spawn_pipes(
        self, record: CommandRecord
    ) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            self.settings.shell,
            "-c",
            record.command,
            cwd=record.working_directory,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

    async def _spawn_pty(
        self, record: CommandRecord
    ) -> tuple[asyncio.subprocess.Process, int]:
        master_fd, slave_fd = pty.openpty()
        try:
            fcntl.ioctl(
                slave_fd,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", 24, 80, 0, 0),
            )
            environment = os.environ.copy()
            environment.setdefault("TERM", "xterm-256color")
            process = await asyncio.create_subprocess_exec(
                self.settings.shell,
                "-c",
                record.command,
                cwd=record.working_directory,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env=environment,
                start_new_session=True,
            )
        except Exception:
            os.close(master_fd)
            raise
        finally:
            os.close(slave_fd)
        return process, master_fd

    async def _pump_stream(
        self,
        stream: asyncio.StreamReader,
        event_type: str,
        queue: asyncio.Queue[tuple[str, str | None]],
    ) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while chunk := await stream.read(4096):
                decoded = decoder.decode(chunk)
                if decoded:
                    await queue.put((event_type, decoded))
            final = decoder.decode(b"", final=True)
            if final:
                await queue.put((event_type, final))
        finally:
            await queue.put((event_type, None))

    async def _pump_pty(
        self,
        master_fd: int,
        queue: asyncio.Queue[tuple[str, str | None]],
    ) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while True:
                try:
                    chunk = await asyncio.to_thread(os.read, master_fd, 4096)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise
                if not chunk:
                    break
                decoded = decoder.decode(chunk)
                if decoded:
                    await queue.put(("output", decoded))
            final = decoder.decode(b"", final=True)
            if final:
                await queue.put(("output", final))
        finally:
            await queue.put(("output", None))

    async def _emit(
        self,
        runtime: CommandRuntime,
        event_type: str,
        *,
        data: str | None = None,
        message: str | None = None,
        status: str | None = None,
        pid: int | None = None,
        return_code: int | None = None,
        working_directory: str | None = None,
        pty: bool | None = None,
        final_status: str | None = None,
    ) -> CommandEvent:
        record = runtime.record
        record.last_seq += 1
        event = CommandEvent(
            command_id=record.command_id,
            seq=record.last_seq,
            type=event_type,
            timestamp=utc_now(),
            data=data,
            message=message,
            status=status,
            pid=pid,
            return_code=return_code,
            working_directory=working_directory,
            pty=pty,
        )
        if runtime.event_file is None:
            raise RuntimeError("Event file is not open")
        runtime.event_file.write(
            event.model_dump_json(exclude_none=True) + "\n"
        )
        runtime.event_file.flush()
        if final_status is not None:
            record.status = final_status
            self._write_metadata(record)
        async with runtime.condition:
            runtime.version += 1
            runtime.condition.notify_all()
        return event

    def _resolve_working_directory(self, requested: str | None) -> Path:
        if requested is None:
            candidate = self.settings.working_directory
        else:
            candidate_path = Path(requested).expanduser()
            candidate = (
                candidate_path
                if candidate_path.is_absolute()
                else self.settings.working_directory / candidate_path
            )
        resolved = candidate.resolve()
        if not resolved.is_dir():
            raise InvalidWorkingDirectoryError(
                f"Working directory does not exist or is not a directory: {resolved}"
            )
        return resolved

    async def _load_existing_records(self) -> None:
        for metadata_path in self.commands_directory.glob("*/metadata.json"):
            try:
                record = CommandRecord.model_validate_json(
                    metadata_path.read_text(encoding="utf-8")
                )
            except Exception:
                logger.exception("Unable to load command metadata: %s", metadata_path)
                continue

            self._records[record.command_id] = record
            if record.status not in TERMINAL_STATUSES:
                record.status = "lost"
                record.finished_at = utc_now()
                record.pid = None
                record.last_seq = max(
                    record.last_seq, self._read_last_sequence(record.command_id)
                )
                record.last_seq += 1
                event = CommandEvent(
                    command_id=record.command_id,
                    seq=record.last_seq,
                    type="error",
                    timestamp=utc_now(),
                    message="Server restarted while the command was still active",
                    status="lost",
                )
                with self._events_path(record.command_id).open(
                    "a", encoding="utf-8"
                ) as event_file:
                    event_file.write(
                        event.model_dump_json(exclude_none=True) + "\n"
                    )
                self._write_metadata(record)

    def _read_events_after(
        self, command_id: str, after_seq: int
    ) -> list[CommandEvent]:
        events, _ = self._read_events_from_offset(
            command_id, 0, after_seq
        )
        return events

    def _read_events_from_offset(
        self,
        command_id: str,
        file_offset: int,
        after_seq: int,
    ) -> tuple[list[CommandEvent], int]:
        event_path = self._events_path(command_id)
        if not event_path.exists():
            return [], file_offset
        events: list[CommandEvent] = []
        with event_path.open("rb") as event_file:
            event_file.seek(file_offset)
            for line in event_file:
                if not line.strip():
                    continue
                try:
                    event = CommandEvent.model_validate_json(line)
                except (ValueError, json.JSONDecodeError):
                    logger.warning(
                        "Skipping malformed event in %s", event_path
                    )
                    continue
                if event.seq > after_seq:
                    events.append(event)
            new_offset = event_file.tell()
        return events, new_offset

    def _read_last_sequence(self, command_id: str) -> int:
        events = self._read_events_after(command_id, 0)
        return events[-1].seq if events else 0

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.cleanup_interval_seconds)
            try:
                await self.cleanup_once()
            except Exception:
                logger.exception("Command history cleanup failed")

    @staticmethod
    def _signal_process_group(pid: int, sig: signal.Signals) -> None:
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            return

    @staticmethod
    def _serialize_event(event: CommandEvent) -> bytes:
        return (event.model_dump_json(exclude_none=True) + "\n").encode("utf-8")

    def _command_directory(self, command_id: str) -> Path:
        return self.commands_directory / command_id

    def _metadata_path(self, command_id: str) -> Path:
        return self._command_directory(command_id) / "metadata.json"

    def _events_path(self, command_id: str) -> Path:
        return self._command_directory(command_id) / "events.ndjson"

    def _write_metadata(self, record: CommandRecord) -> None:
        metadata_path = self._metadata_path(record.command_id)
        temporary_path = metadata_path.with_suffix(".json.tmp")
        temporary_path.write_text(
            record.model_dump_json(indent=2), encoding="utf-8"
        )
        os.replace(temporary_path, metadata_path)
