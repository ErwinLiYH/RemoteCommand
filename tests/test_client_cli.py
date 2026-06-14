from __future__ import annotations

import pytest

from remote_command_server import client_cli


class FakeClient:
    instances: list["FakeClient"] = []

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url
        self.token = token
        self.run_arguments = None
        self.event_arguments = None
        self.closed = False
        self.instances.append(self)

    def run_command(self, command: str, **kwargs):
        self.run_arguments = (command, kwargs)
        yield {
            "command_id": "new-command",
            "seq": 1,
            "type": "started",
            "pid": 42,
        }
        yield {
            "command_id": "new-command",
            "seq": 2,
            "type": "stdout",
            "data": "normal output\n",
        }
        yield {
            "command_id": "new-command",
            "seq": 3,
            "type": "stderr",
            "data": "warning\n",
        }
        yield {
            "command_id": "new-command",
            "seq": 4,
            "type": "exit",
            "status": "exited",
            "return_code": 7,
        }

    def events(self, command_id: str, **kwargs):
        self.event_arguments = (command_id, kwargs)
        yield {
            "command_id": command_id,
            "seq": 13,
            "type": "output",
            "data": "resumed\routput",
        }
        yield {
            "command_id": command_id,
            "seq": 14,
            "type": "exit",
            "status": "exited",
            "return_code": 0,
        }

    def health(self):
        return {
            "status": "ok",
            "working_directory": "/srv/workspace",
        }

    def list_commands(self):
        return [
            {
                "command_id": "running-task",
                "command": "sleep 10",
                "working_directory": "/srv/workspace",
                "pty": False,
                "status": "running",
                "created_at": "2026-06-13T12:00:00Z",
                "started_at": "2026-06-13T12:00:01Z",
                "finished_at": None,
                "pid": 100,
                "return_code": None,
                "last_seq": 2,
            },
            {
                "command_id": "finished-task",
                "command": "echo done",
                "working_directory": "/tmp",
                "pty": False,
                "status": "exited",
                "created_at": "2026-06-13T11:00:00Z",
                "started_at": "2026-06-13T11:00:01Z",
                "finished_at": "2026-06-13T11:00:02Z",
                "pid": 99,
                "return_code": 0,
                "last_seq": 3,
            },
        ]

    def get_command(self, command_id: str):
        return {
            "command_id": command_id,
            "command": "sleep 10",
            "working_directory": "/srv/workspace",
            "pty": False,
            "status": "running",
            "created_at": "2026-06-13T12:00:00Z",
            "started_at": "2026-06-13T12:00:01Z",
            "finished_at": None,
            "pid": 100,
            "return_code": None,
            "last_seq": 2,
        }

    def cancel_command(self, command_id: str):
        return {
            "command_id": command_id,
            "status": "cancelled",
            "return_code": -15,
        }

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def fake_client(monkeypatch):
    FakeClient.instances.clear()
    monkeypatch.setattr(client_cli, "RemoteCommandClient", FakeClient)


def test_run_prints_remote_output_and_returns_remote_code(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = client_cli.main(
        [
            "--url",
            "http://server",
            "--token",
            "secret",
            "--command",
            "ls -la",
            "--working-directory",
            "/tmp",
            "--pty",
        ]
    )

    captured = capsys.readouterr()
    assert result == 7
    assert captured.out == "normal output\n"
    assert "warning\n" in captured.err
    assert "command_id=new-command pid=42" in captured.err
    assert "return_code=7" in captured.err
    client = FakeClient.instances[0]
    assert client.run_arguments == (
        "ls -la",
        {
            "command_id": None,
            "working_directory": "/tmp",
            "pty": True,
        },
    )
    assert client.closed


def test_reconnect_uses_sequence_and_can_disable_follow(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = client_cli.main(
        [
            "--token",
            "secret",
            "--reconnect",
            "existing-command",
            "--after-seq",
            "12",
            "--no-follow",
            "--quiet",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert captured.out == "resumed\routput"
    assert captured.err == ""
    client = FakeClient.instances[0]
    assert client.event_arguments == (
        "existing-command",
        {"after_seq": 12, "follow": False},
    )


def test_list_can_filter_running_commands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = client_cli.main(
        ["--token", "secret", "--list", "--status", "running"]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert "COMMAND ID" in captured.out
    assert "running-task" in captured.out
    assert "finished-task" not in captured.out


def test_get_command_as_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = client_cli.main(
        ["--token", "secret", "--get", "running-task", "--json"]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert '"command_id": "running-task"' in captured.out
    assert '"status": "running"' in captured.out


def test_cancel_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = client_cli.main(
        ["--token", "secret", "--cancel", "running-task"]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert (
        captured.out
        == "command_id=running-task status=cancelled return_code=-15\n"
    )


def test_health_does_not_require_token(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = client_cli.main(["--health"])

    captured = capsys.readouterr()
    assert result == 0
    assert "status: ok" in captured.out
    assert "working_directory: /srv/workspace" in captured.out
    assert FakeClient.instances[0].token is None


def test_stream_json_outputs_one_event_per_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = client_cli.main(
        ["--token", "secret", "--command", "ls", "--json"]
    )

    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert result == 7
    assert len(lines) == 4
    assert all(line.startswith("{") and line.endswith("}") for line in lines)
    assert captured.err == ""


def test_signal_exit_code_mapping() -> None:
    assert client_cli._shell_exit_code(-15) == 143
    assert client_cli._shell_exit_code(300) == 255
