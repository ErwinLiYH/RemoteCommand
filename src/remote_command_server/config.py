from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    token: str
    working_directory: Path
    state_directory: Path
    shell: str = "/bin/sh"
    retention_seconds: float = 7 * 24 * 60 * 60
    cleanup_interval_seconds: float = 60 * 60
    terminate_grace_seconds: float = 5.0
    stream_heartbeat_seconds: float = 15.0

    def normalized(self) -> "Settings":
        working_directory = self.working_directory.expanduser().resolve()
        state_directory = self.state_directory.expanduser().resolve()
        shell = str(Path(self.shell).expanduser().resolve())

        if not self.token:
            raise ValueError("A non-empty bearer token is required")
        if not working_directory.is_dir():
            raise ValueError(
                f"Working directory does not exist or is not a directory: "
                f"{working_directory}"
            )
        if not Path(shell).is_file():
            raise ValueError(f"Shell does not exist: {shell}")
        if self.retention_seconds < 0:
            raise ValueError("Retention must be non-negative")
        if self.cleanup_interval_seconds <= 0:
            raise ValueError("Cleanup interval must be positive")
        if self.terminate_grace_seconds <= 0:
            raise ValueError("Termination grace period must be positive")
        if self.stream_heartbeat_seconds <= 0:
            raise ValueError("Stream heartbeat interval must be positive")

        return Settings(
            token=self.token,
            working_directory=working_directory,
            state_directory=state_directory,
            shell=shell,
            retention_seconds=self.retention_seconds,
            cleanup_interval_seconds=self.cleanup_interval_seconds,
            terminate_grace_seconds=self.terminate_grace_seconds,
            stream_heartbeat_seconds=self.stream_heartbeat_seconds,
        )

