"""Write an MCP Inspector catalog: every MCP server with a fresh token.

    python -m scripts.write_inspector_catalog anna .mcp-inspector.json

Each server gets its own token because the audience differs per server.
The stand must be running. The file holds tokens, so it stays git-ignored.
"""

import asyncio
import json
import sys
from pathlib import Path

from core.config import RESOURCES
from core.tokens import Tokens


async def build_catalog(login: str) -> dict:
    tokens = Tokens(login)
    return {
        "mcpServers": {
            key: {
                "type": "streamable-http",
                "url": f"{resource}/mcp",
                "headers": {"Authorization": f"Bearer {await tokens.get(key)}"},
            }
            for key, resource in RESOURCES.items()
        }
    }


def main() -> None:
    login, catalog_path = sys.argv[1:]
    catalog = asyncio.run(build_catalog(login))
    Path(catalog_path).write_text(json.dumps(catalog, indent=2))


if __name__ == "__main__":
    main()
