"""Logging that is safe to use under an MCP stdio transport.

Under stdio, file descriptor 1 *is* the JSON-RPC wire. `logging.basicConfig()`
defaults to a `StreamHandler` on `sys.stderr`, which is correct, but any library
that calls `basicConfig` first wins, and the default could be anything. This
module makes the choice explicit and idempotent.
"""

from __future__ import annotations

import logging
import sys

_CONFIGURED = False

DEFAULT_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def configure_logging(level: int = logging.INFO, *, force: bool = False) -> None:
    """Attach a single stderr handler to the root logger.

    Idempotent: repeated calls are no-ops unless `force` is set. Existing handlers
    are removed, so a library that already called `basicConfig` cannot leave a
    stdout handler attached behind our back.
    """
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter(DEFAULT_FORMAT))
    root.addHandler(handler)
    root.setLevel(level)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """A logger that is guaranteed to have a stderr destination configured."""
    configure_logging()
    return logging.getLogger(name)
