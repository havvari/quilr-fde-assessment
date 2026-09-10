"""A copy of the Task 1 entry point sabotaged with an import-time write.

This is the failure mode the SDK's fd-1 claim cannot defend against: the print
below runs while fd 1 is still the JSON-RPC wire, so its bytes land in front of
the first response frame. Used by `test_task1_stdout.py` to show that the
purity test is capable of failing.
"""

from __future__ import annotations

import sys

print("starting refund desk...")
sys.stdout.flush()

from task1_mcp_server.__main__ import main  # noqa: E402

if __name__ == "__main__":
    main()
