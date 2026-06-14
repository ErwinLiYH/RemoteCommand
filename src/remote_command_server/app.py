from __future__ import annotations

import hmac
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from remote_command_server.config import Settings
from remote_command_server.manager import (
    CommandConflictError,
    CommandManager,
    CommandNotFoundError,
    InvalidWorkingDirectoryError,
)
from remote_command_server.models import (
    CommandListResponse,
    CommandRecord,
    RunCommandRequest,
)

bearer_scheme = HTTPBearer(auto_error=False)


def create_app(settings: Settings) -> FastAPI:
    normalized_settings = settings.normalized()
    manager = CommandManager(normalized_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await manager.start()
        app.state.command_manager = manager
        try:
            yield
        finally:
            await manager.stop()

    app = FastAPI(
        title="RemoteCommand Server",
        version="0.1.0",
        description=(
            "Run commands on this server and consume reconnectable NDJSON output."
        ),
        lifespan=lifespan,
    )
    app.state.command_manager = manager

    async def require_token(
        credentials: HTTPAuthorizationCredentials | None = Depends(
            bearer_scheme
        ),
    ) -> None:
        if (
            credentials is None
            or credentials.scheme.lower() != "bearer"
            or not hmac.compare_digest(
                credentials.credentials, normalized_settings.token
            )
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    protected = [Depends(require_token)]

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {
            "status": "ok",
            "working_directory": str(
                normalized_settings.working_directory
            ),
        }

    @app.post(
        "/commands",
        dependencies=protected,
        responses={
            200: {
                "content": {
                    "application/x-ndjson": {
                        "schema": {"type": "string", "format": "binary"}
                    }
                },
                "description": "Reconnectable command event stream",
            }
        },
    )
    async def run_command(request: RunCommandRequest) -> StreamingResponse:
        try:
            record = await manager.create_command(
                request.command,
                command_id=request.command_id,
                working_directory=request.working_directory,
                use_pty=request.pty,
            )
        except CommandConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (InvalidWorkingDirectoryError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return StreamingResponse(
            manager.iter_events(record.command_id),
            media_type="application/x-ndjson",
            headers=_stream_headers(record.command_id),
        )

    @app.get(
        "/commands",
        response_model=CommandListResponse,
        dependencies=protected,
    )
    async def list_commands() -> CommandListResponse:
        return CommandListResponse(commands=manager.list_commands())

    @app.get(
        "/commands/{command_id}",
        response_model=CommandRecord,
        dependencies=protected,
    )
    async def get_command(command_id: str) -> CommandRecord:
        try:
            return manager.get_command(command_id)
        except CommandNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Command not found") from exc

    @app.get(
        "/commands/{command_id}/events",
        dependencies=protected,
        responses={
            200: {
                "content": {
                    "application/x-ndjson": {
                        "schema": {"type": "string", "format": "binary"}
                    }
                }
            }
        },
    )
    async def command_events(
        command_id: str,
        after_seq: int = Query(default=0, ge=0),
        follow: bool = True,
    ) -> StreamingResponse:
        try:
            manager.get_command(command_id)
        except CommandNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Command not found") from exc

        return StreamingResponse(
            manager.iter_events(
                command_id, after_seq=after_seq, follow=follow
            ),
            media_type="application/x-ndjson",
            headers=_stream_headers(command_id),
        )

    @app.delete(
        "/commands/{command_id}",
        response_model=CommandRecord,
        dependencies=protected,
    )
    async def cancel_command(command_id: str) -> CommandRecord:
        try:
            return await manager.cancel_command(command_id)
        except CommandNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Command not found") from exc
        except CommandConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return app


def _stream_headers(command_id: str) -> dict[str, str]:
    return {
        "X-Command-ID": command_id,
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
    }
