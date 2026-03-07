from __future__ import annotations

from .server import McpServer


def main() -> int:
    server = McpServer()
    server.serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
