"""HTTP boundary of the chat: sessions and the SSE stream of a turn.

TODO(контракты): POST /api/cards/{pending_id}/decision — кнопка карточки:
  «да» → orchestrator.execute_pending, «нет» → отмена.
"""

from __future__ import annotations

import asyncio
import json
import traceback
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from app import messages, orchestrator, sessions
from app.sessions import Session
from core import config
from core.logging import get_logger

logger = get_logger(__name__)

LOGGED_STACK_FRAMES = 5

# The event loop keeps only weak references to tasks, and a turn must outlive
# the stream that started it.
RUNNING_TURNS: set[asyncio.Task[None]] = set()


class SessionRequest(BaseModel):
    user: str


class ChatRequest(BaseModel):
    session_id: str
    message: str


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    config.setup_model()
    yield


app = FastAPI(lifespan=lifespan)
# The web chat is served by the stand, on another port.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def format_sse_event(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def extract_stack_frames(error: Exception) -> list[str]:
    """Return where the error happened, without the exception's text."""
    frames = traceback.extract_tb(error.__traceback__)[-LOGGED_STACK_FRAMES:]
    return [
        f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
        for frame in frames
    ]


async def drive_turn(
    session: Session, message: str, queue: asyncio.Queue[dict]
) -> None:
    """Run one turn. A failed turn is an event, the session keeps working."""
    try:
        await orchestrator.run_turn(session, message, queue.put)
    except Exception as error:  # noqa: BLE001 -- any failure must end as an event
        await queue.put({"type": "error", "detail": messages.TURN_FAILED})
        # No exception text: a plan validation error quotes the model's output.
        logger.error(
            "turn_failed",
            error_type=type(error).__name__,
            frames=extract_stack_frames(error),
        )
    finally:
        await queue.put({"type": "done"})


async def stream_turn(session: Session, message: str) -> AsyncGenerator[str]:
    """Yield the events of one turn as SSE."""
    queue: asyncio.Queue[dict] = asyncio.Queue()
    # A separate task, so a client disconnect stops this stream and not the
    # turn: cancelled between the billing call and the audit record, an action
    # would be done and unrecorded.
    turn = asyncio.create_task(drive_turn(session, message, queue))
    RUNNING_TURNS.add(turn)
    turn.add_done_callback(RUNNING_TURNS.discard)
    while True:
        event = await queue.get()
        yield format_sse_event(event)
        if event["type"] == "done":
            return


async def stream_unknown_session() -> AsyncGenerator[str]:
    # An event and not a 404: the web chat reads only the stream.
    yield format_sse_event(
        {"type": "error", "detail": messages.UNKNOWN_SESSION}
    )
    yield format_sse_event({"type": "done"})


@app.get("/api/health")
async def check_health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/session")
async def open_session(request: SessionRequest) -> JSONResponse:
    try:
        session = await sessions.open_session(request.user)
    except KeyError:
        # POST-QA: return 422, the body is valid JSON with an unknown value.
        return JSONResponse({"error": messages.UNKNOWN_USER}, status_code=400)
    return JSONResponse(
        {
            "session_id": session.session_id,
            "name": session.user.name,
            "role": session.user.role,
            "visible_tools": session.pool.tool_names(),
        }
    )


@app.post("/api/chat")
async def chat(request: ChatRequest) -> StreamingResponse:
    session = sessions.get(request.session_id)
    events = (
        stream_turn(session, request.message)
        if session is not None
        else stream_unknown_session()
    )
    return StreamingResponse(events, media_type="text/event-stream")
