from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from remote_command_server.app import create_app
from remote_command_server.config import Settings


def make_app(tmp_path: Path):
    return create_app(
        Settings(
            token="secret",
            working_directory=tmp_path,
            state_directory=tmp_path / "state",
            cleanup_interval_seconds=3600,
            stream_heartbeat_seconds=0.05,
        )
    )


@pytest.mark.asyncio
async def test_authentication_and_health(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            health = await client.get("/healthz")
            assert health.status_code == 200

            unauthorized = await client.get("/commands")
            assert unauthorized.status_code == 401

            authorized = await client.get(
                "/commands",
                headers={"Authorization": "Bearer secret"},
            )
            assert authorized.status_code == 200
            assert authorized.json() == {"commands": []}


@pytest.mark.asyncio
async def test_run_query_and_replay_command(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    headers = {"Authorization": "Bearer secret"}
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.post(
                "/commands",
                headers=headers,
                json={
                    "command": "printf 'hello'",
                    "command_id": "example",
                },
            )
            assert response.status_code == 200
            assert response.headers["x-command-id"] == "example"
            events = [
                json.loads(line)
                for line in response.text.splitlines()
                if line
            ]
            assert events[0]["type"] == "started"
            assert events[-1]["type"] == "exit"

            details = await client.get("/commands/example", headers=headers)
            assert details.json()["status"] == "exited"

            replay = await client.get(
                "/commands/example/events",
                headers=headers,
                params={"after_seq": events[0]["seq"], "follow": "false"},
            )
            replay_events = [
                json.loads(line)
                for line in replay.text.splitlines()
                if line
            ]
            assert all(
                event["seq"] > events[0]["seq"] for event in replay_events
            )
            assert replay_events[-1]["type"] == "exit"


@pytest.mark.asyncio
async def test_rejects_invalid_working_directory_and_duplicate_id(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    headers = {"Authorization": "Bearer secret"}
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            invalid = await client.post(
                "/commands",
                headers=headers,
                json={
                    "command": "pwd",
                    "working_directory": "missing-directory",
                },
            )
            assert invalid.status_code == 400

            first = await client.post(
                "/commands",
                headers=headers,
                json={"command": "true", "command_id": "duplicate"},
            )
            assert first.status_code == 200

            duplicate = await client.post(
                "/commands",
                headers=headers,
                json={"command": "true", "command_id": "duplicate"},
            )
            assert duplicate.status_code == 409


@pytest.mark.asyncio
async def test_cleanup_removes_completed_commands(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    headers = {"Authorization": "Bearer secret"}
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            completed = await client.post(
                "/commands",
                headers=headers,
                json={"command": "true", "command_id": "completed"},
            )
            assert completed.status_code == 200

            cleanup = await client.post("/cleanup", headers=headers)
            assert cleanup.status_code == 200
            assert cleanup.json() == {
                "cleaned_commands": ["completed"],
                "count": 1,
            }

            missing = await client.get(
                "/commands/completed", headers=headers
            )
            assert missing.status_code == 404

            unauthorized = await client.post("/cleanup")
            assert unauthorized.status_code == 401
