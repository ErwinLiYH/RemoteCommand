from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx


class RemoteCommandClient:
    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        *,
        connect_timeout: float = 5.0,
    ):
        timeout = httpx.Timeout(
            connect=connect_timeout,
            read=None,
            write=None,
            pool=connect_timeout,
        )
        headers = (
            {"Authorization": f"Bearer {token}"}
            if token
            else {}
        )
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"), headers=headers, timeout=timeout
        )

    def health(self) -> dict[str, Any]:
        response = self._client.get("/healthz")
        response.raise_for_status()
        return response.json()

    def run_command(
        self,
        command: str,
        *,
        command_id: str | None = None,
        working_directory: str | None = None,
        pty: bool = False,
    ) -> Iterator[dict[str, Any]]:
        payload = {
            "command": command,
            "command_id": command_id,
            "working_directory": working_directory,
            "pty": pty,
        }
        with self._client.stream("POST", "/commands", json=payload) as response:
            response.raise_for_status()
            yield from self._iter_response_events(response)

    def events(
        self,
        command_id: str,
        *,
        after_seq: int = 0,
        follow: bool = True,
    ) -> Iterator[dict[str, Any]]:
        params = {"after_seq": after_seq, "follow": str(follow).lower()}
        with self._client.stream(
            "GET", f"/commands/{command_id}/events", params=params
        ) as response:
            response.raise_for_status()
            yield from self._iter_response_events(response)

    def get_command(self, command_id: str) -> dict[str, Any]:
        response = self._client.get(f"/commands/{command_id}")
        response.raise_for_status()
        return response.json()

    def list_commands(self) -> list[dict[str, Any]]:
        response = self._client.get("/commands")
        response.raise_for_status()
        return response.json()["commands"]

    def cleanup_commands(self) -> dict[str, Any]:
        response = self._client.post("/cleanup")
        response.raise_for_status()
        return response.json()

    def cancel_command(self, command_id: str) -> dict[str, Any]:
        response = self._client.delete(f"/commands/{command_id}")
        response.raise_for_status()
        return response.json()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "RemoteCommandClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    @staticmethod
    def _iter_response_events(
        response: httpx.Response,
    ) -> Iterator[dict[str, Any]]:
        for line in response.iter_lines():
            if line:
                yield json.loads(line)
