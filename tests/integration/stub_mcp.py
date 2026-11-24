"""A stub MCP pool and a session that uses it and a stub PEP."""

from app import permissions
from app.sessions import Session
from core import config
from core.logging import make_trace_id
from tests.integration.stub_pep import StubPep

# What anna's token allows: the claims the issuer puts in it.
ANNA_ALLOWLIST = permissions.allowlist_from_claims(
    {"role": "client", "scope": "tariffs:read tariffs:write charges:read"}
)


class StubMcpPool:
    """Answers like an MCP pool and remembers every call."""

    def __init__(self, result_by_tool: dict[str, dict | Exception]) -> None:
        # What each tool returns. A tool whose result is an exception fails
        # with it, like a server that is down. Calling a tool missing here
        # fails the test with a KeyError.
        self.result_by_tool = result_by_tool
        # Every call in order: the tool's name and its arguments.
        self.tool_calls: list[tuple[str, dict]] = []

    def resource_of(self, _tool_name: str) -> str:
        # Every tool these tests call lives on the action server.
        return "actions"

    def tool_schema(self, _tool_name: str) -> dict:
        # Only the model reads the schema, and these tests have no model.
        return {"type": "object", "properties": {}}

    def tool_description(self, tool_name: str) -> str:
        return tool_name

    async def call(
        self, _resource_key: str, tool_name: str, arguments: dict
    ) -> dict:
        self.tool_calls.append((tool_name, arguments))
        tool_result = self.result_by_tool[tool_name]
        if isinstance(tool_result, Exception):
            raise tool_result
        return tool_result


def make_session(mcp_pool: StubMcpPool) -> Session:
    """Return a new session of anna backed by the stub MCP pool.

    Its PEP allows every stage; a test may put another stub in `pep`.
    """
    return Session(
        login="anna",
        user=config.USERS["anna"],
        # No tokens: the stub MCP pool doesn't check them.
        tokens=None,  # pyright: ignore[reportArgumentType]
        mcp=mcp_pool,  # pyright: ignore[reportArgumentType]
        allowlist=ANNA_ALLOWLIST,
        # A trace of its own, so a test finds the rows its session wrote.
        trace_id=make_trace_id(),
        pep=StubPep(),
    )
