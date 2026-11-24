"""Сессии чата: identity, соединения с MCP, история планировщика.

Одна сессия = один пользователь чата. При открытии сессии backend —
доверенный клиент — выпускает у издателя токены для всех MCP-серверов
(по одному на resource, RFC 8707) и держит открытые соединения.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agents import Agent, TResponseInputItem

from app import permissions
from app.pep import Pep
from core import config, db
from core.logging import get_logger
from core.mcpclient import McpPool
from core.tokens import Tokens

if TYPE_CHECKING:
    from app.planner import DroppedStep

logger = get_logger(__name__)

# An even number: every turn adds a message and a reply, so the trimmed history
# still starts with the subscriber.
PLANNER_HISTORY_LIMIT = 20


@dataclass
class Session:
    login: str
    user: config.User
    tokens: Tokens
    mcp: McpPool
    # The operations the token's claims allow, fixed for the session's life.
    allowlist: permissions.Allowlist
    # The dialogue as the subscriber saw it, for resolving follow-up questions.
    planner_messages: list[TResponseInputItem] = field(default_factory=list)
    used_tools: list[str] = field(default_factory=list)
    dropped_steps: list[DroppedStep] = field(default_factory=list)
    trace_id: str = ""  # сквозной id текущего хода
    planner: Agent | None = None  # built on the first turn
    # Asks the policy service before each stage of a card; see pep_of.
    pep: Pep | None = None
    # Whether the guardrail answered on this turn: every stage of a card asks
    # the policy with it. A turn sets it before its first policy request;
    # True is for code that asks without a turn.
    guard_ok: bool = True
    # One turn or card decision at a time: two "да" at once must not both
    # pass the idempotency check before either records its key.
    turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    session_id: str = field(
        default_factory=lambda: f"sess_{uuid.uuid4().hex[:8]}"
    )

    def list_visible_tools(self) -> list[str]:
        """Return the tools of the session's servers its models may see."""
        return permissions.visible_tools(self.allowlist, self.mcp.tool_names())

    def record_tool_use(self, tool: str) -> None:
        self.used_tools.append(tool)

    def remember_turn(self, message: str, reply: str) -> None:
        self.planner_messages.extend(
            [
                {"role": "user", "content": message},
                {"role": "assistant", "content": reply},
            ]
        )
        del self.planner_messages[:-PLANNER_HISTORY_LIMIT]


SESSIONS: dict[str, Session] = {}


async def open_session(login: str) -> Session:
    user = config.USERS.get(login)
    if user is None:
        raise KeyError(f"неизвестный пользователь: {login}")

    tokens = Tokens(login)
    pool = McpPool(tokens, resource_keys=("crm", "billing", "kb", "actions"))
    await pool.connect()
    # The pool holds a token for each server by now, and every token carries
    # the same role and scope, so the default one will do.
    claims = await tokens.claims()
    allowlist = permissions.allowlist_from_claims(claims)

    session = Session(
        login=login, user=user, tokens=tokens, mcp=pool, allowlist=allowlist
    )
    db.execute(
        "insert into sessions(session_id, login, sub) values (%s, %s, %s)",
        (session.session_id, login, user.sub),
    )
    SESSIONS[session.session_id] = session
    # The rights this session got: what its models will be able to see.
    logger.info(
        "session_opened",
        session_id=session.session_id,
        role=claims.get("role", ""),
        visible_tools=session.list_visible_tools(),
    )
    return session


def get(session_id: str) -> Session | None:
    return SESSIONS.get(session_id)


def pep_of(session: Session) -> Pep:
    """Return the session's PEP, created on first use."""
    if session.pep is None:
        session.pep = Pep(session.session_id, session.tokens)
    return session.pep
