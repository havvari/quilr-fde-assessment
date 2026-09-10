"""Entry point: `python -m task1_mcp_server`.

Under stdio transport, file descriptor 1 *is* the JSON-RPC wire, and one stray
byte on it desynchronises the client's parser for the rest of the session.

What the SDK already does: `mcp/server/stdio.py` claims fd 1 the moment the
session starts -- it dup2s the real wire onto a private descriptor and points
fd 1 at stderr for the session's lifetime. So a `print()` inside a tool handler
lands on stderr and is harmless. That is a genuinely good default and it means
"we were careful not to print" is not the interesting part of this milestone.

What it cannot cover is the window *before* the claim: anything written at import
time, by our modules or by a dependency announcing itself, goes to the real fd 1
and corrupts the very first frame. That window is what `assert_stdout_unclaimed`
watches, and what `tests/test_task1_stdout.py` actually exercises.
"""

from __future__ import annotations

import io
import sys

from common.logging import configure_logging, get_logger
from task1_mcp_server.server import build_server

logger = get_logger(__name__)


def assert_stdout_unclaimed() -> None:
    """Warn to stderr if anything has already written to, or rebound, stdout.

    Called before the transport starts, which is the only moment this check can
    still tell you something: afterwards fd 1 points at stderr and a write proves
    nothing. It warns rather than exits -- refusing to start would turn a cosmetic
    dependency banner into an outage -- but the warning names the culprit's
    `repr`, which is usually enough to find it.
    """
    stream = sys.stdout
    if stream is None:  # pragma: no cover - only under pythonw-style hosts
        return

    if not isinstance(stream, io.TextIOWrapper):
        logger.warning(
            "sys.stdout has been rebound to %r before the stdio transport started. "
            "Anything already written to it went onto the JSON-RPC wire.",
            stream,
        )
        return

    # A buffered writer holding bytes at this point means someone printed during
    # import and the bytes have not been flushed yet -- they will land on the wire.
    buffered = getattr(stream, "buffer", None)
    pending = getattr(buffered, "_write_buf", None)
    if pending:
        logger.warning("stdout has %d unflushed byte(s) queued before transport start", len(pending))


def main() -> None:
    configure_logging()
    assert_stdout_unclaimed()
    logger.info("starting %s over stdio", "refund-desk")
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
