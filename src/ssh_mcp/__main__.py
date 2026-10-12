from __future__ import annotations

import os

from .server import McpServer


def main() -> int:
    server = McpServer(
        max_workers=int(os.environ.get("SSH_MCP_MAX_WORKERS", "4")),
        max_pending=int(os.environ.get("SSH_MCP_MAX_PENDING", "16")),
    )
    server.serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
