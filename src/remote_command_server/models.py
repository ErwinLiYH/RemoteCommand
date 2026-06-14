from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


CommandStatus = Literal[
    "queued",
    "running",
    "terminating",
    "exited",
    "failed",
    "cancelled",
    "lost",
]
TerminalStatus = Literal["exited", "failed", "cancelled", "lost"]
EventType = Literal[
    "started",
    "stdout",
    "stderr",
    "output",
    "exit",
    "error",
    "heartbeat",
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RunCommandRequest(BaseModel):
    command: str = Field(min_length=1)
    command_id: str | None = None
    working_directory: str | None = None
    pty: bool = False


class CommandRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: str
    command: str
    working_directory: str
    pty: bool
    status: CommandStatus
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    pid: int | None = None
    return_code: int | None = None
    last_seq: int = 0


class CommandEvent(BaseModel):
    model_config = ConfigDict(exclude_none=True)

    command_id: str
    seq: int
    type: EventType
    timestamp: datetime
    data: str | None = None
    message: str | None = None
    status: CommandStatus | None = None
    pid: int | None = None
    return_code: int | None = None
    working_directory: str | None = None
    pty: bool | None = None
    transient: bool | None = None


class CommandListResponse(BaseModel):
    commands: list[CommandRecord]


class CleanupResponse(BaseModel):
    cleaned_commands: list[str]
    count: int
