"""A server with a tool whose body prints, to show where those bytes actually go.

Under MCP SDK v2's stdio transport, fd 1 has already been repointed at stderr by
the time any handler runs, so this print is loud but harmless. Contrast with
`noisy_server.py`, which prints before the claim and does corrupt the wire.
"""

from __future__ import annotations

from common.logging import configure_logging
from task1_mcp_server.server import build_server

BANNER = "SIDE EFFECT FROM INSIDE THE HANDLER"


def main() -> None:
    configure_logging()
    server = build_server()

    @server.tool(name="shout", description="Writes to stdout from inside a handler.")
    def shout() -> str:
        print(BANNER)
        return "done"

    server.run(transport="stdio")


if __name__ == "__main__":
    main()
