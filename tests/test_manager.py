from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from remote_command_server.config import Settings
from remote_command_server.manager import CommandManager
from remote_command_server.models import CommandRecord, utc_now


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        token="test-token",
        working_directory=tmp_path,
        state_directory=tmp_path / "state",
        retention_seconds=3600,
        cleanup_interval_seconds=3600,
        terminate_grace_seconds=0.25,
        stream_heartbeat_seconds=0.05,
    )


async def collect_events(
    manager: CommandManager,
    command_id: str,
    *,
    after_seq: int = 0,
    follow: bool = True,
) -> list[dict]:
    events = []
    async for chunk in manager.iter_events(
        command_id, after_seq=after_seq, follow=follow
    ):
        event = json.loads(chunk)
        if event["type"] != "heartbeat":
            events.append(event)
    return events


@pytest.mark.asyncio
async def test_streams_stdout_stderr_and_exit_code(tmp_path: Path) -> None:
    manager = CommandManager(make_settings(tmp_path))
    await manager.start()
    try:
        record = await manager.create_command(
            "printf 'out'; printf 'err' >&2; exit 7"
        )
        events = await collect_events(manager, record.command_id)

        assert events[0]["type"] == "started"
        assert "".join(
            event.get("data", "")
            for event in events
            if event["type"] == "stdout"
        ) == "out"
        assert "".join(
            event.get("data", "")
            for event in events
            if event["type"] == "stderr"
        ) == "err"
        assert events[-1]["type"] == "exit"
        assert events[-1]["return_code"] == 7
        assert manager.get_command(record.command_id).status == "exited"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_working_directory_override(tmp_path: Path) -> None:
    override = tmp_path / "nested"
    override.mkdir()
    manager = CommandManager(make_settings(tmp_path))
    await manager.start()
    try:
        record = await manager.create_command(
            "pwd", working_directory="nested"
        )
        events = await collect_events(manager, record.command_id)
        output = "".join(
            event.get("data", "")
            for event in events
            if event["type"] == "stdout"
        )
        assert output.strip() == str(override)
        assert manager.get_command(record.command_id).working_directory == str(
            override
        )
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_stream_disconnect_does_not_stop_command(tmp_path: Path) -> None:
    manager = CommandManager(make_settings(tmp_path))
    await manager.start()
    try:
        record = await manager.create_command(
            "printf 'before'; sleep 0.15; printf 'after'"
        )
        stream = manager.iter_events(record.command_id)
        first = json.loads(await anext(stream))
        assert first["type"] == "started"
        await stream.aclose()

        for _ in range(100):
            current = manager.get_command(record.command_id)
            if current.status == "exited":
                break
            await asyncio.sleep(0.01)

        events = await collect_events(
            manager, record.command_id, follow=False
        )
        output = "".join(
            event.get("data", "")
            for event in events
            if event["type"] == "stdout"
        )
        assert output == "beforeafter"
        assert events[-1]["type"] == "exit"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_resume_after_sequence(tmp_path: Path) -> None:
    manager = CommandManager(make_settings(tmp_path))
    await manager.start()
    try:
        record = await manager.create_command("printf 'hello'")
        all_events = await collect_events(manager, record.command_id)
        resumed = await collect_events(
            manager,
            record.command_id,
            after_seq=all_events[0]["seq"],
            follow=False,
        )
        assert resumed
        assert all(
            event["seq"] > all_events[0]["seq"] for event in resumed
        )
        assert resumed[-1]["type"] == "exit"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_pty_preserves_terminal_output(tmp_path: Path) -> None:
    manager = CommandManager(make_settings(tmp_path))
    await manager.start()
    try:
        record = await manager.create_command(
            "printf 'one\\rtwo'", use_pty=True
        )
        events = await collect_events(manager, record.command_id)
        assert not any(
            event["type"] in {"stdout", "stderr"} for event in events
        )
        output = "".join(
            event.get("data", "")
            for event in events
            if event["type"] == "output"
        )
        assert output == "one\rtwo"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_cancel_terminates_command(tmp_path: Path) -> None:
    manager = CommandManager(make_settings(tmp_path))
    await manager.start()
    try:
        record = await manager.create_command("sleep 10")
        for _ in range(100):
            if manager.get_command(record.command_id).status == "running":
                break
            await asyncio.sleep(0.01)

        cancelled = await manager.cancel_command(record.command_id)
        assert cancelled.status == "cancelled"
        assert cancelled.return_code is not None
        assert cancelled.return_code < 0
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_restart_marks_incomplete_command_lost(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    command_directory = settings.state_directory / "commands" / "recovered"
    command_directory.mkdir(parents=True)
    record = CommandRecord(
        command_id="recovered",
        command="sleep 10",
        working_directory=str(tmp_path),
        pty=False,
        status="running",
        created_at=utc_now(),
        started_at=utc_now(),
        pid=12345,
        last_seq=1,
    )
    (command_directory / "metadata.json").write_text(
        record.model_dump_json(indent=2), encoding="utf-8"
    )
    (command_directory / "events.ndjson").write_text(
        (
            '{"command_id":"recovered","seq":1,"type":"started",'
            f'"timestamp":"{utc_now().isoformat()}","status":"running",'
            '"pid":12345}\n'
        ),
        encoding="utf-8",
    )

    manager = CommandManager(settings)
    await manager.start()
    try:
        recovered = manager.get_command("recovered")
        assert recovered.status == "lost"
        assert recovered.pid is None
        events = await collect_events(manager, "recovered", follow=False)
        assert events[-1]["type"] == "error"
        assert events[-1]["status"] == "lost"
        assert events[-1]["seq"] == 2
    finally:
        await manager.stop()
