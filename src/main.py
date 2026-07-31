"""Entry point: start the approvals web page, then serve MCP.

Transports: stdio (default, for local agents / quick review) or
streamable HTTP (`--transport http`, for running as an in-network service).
All logging goes to stderr — stdout belongs to the MCP protocol.
"""

import argparse
import os
import sys

from .approvals_ui import start_approvals_server
from .diagnostics import log_server_start
from .eventlog import audit_log, diagnostic_log
from .server import client, ledger, mcp


def main() -> None:
    parser = argparse.ArgumentParser(description="StoreLink MCP server")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    args = parser.parse_args()

    host = os.getenv("APPROVALS_HOST", "127.0.0.1")
    port = int(os.getenv("APPROVALS_PORT", "8765"))
    start_approvals_server(ledger, host, port)
    print(f"Approvals page: http://{host}:{port}", file=sys.stderr)
    print(f"Store keys:     {client.key_source()}", file=sys.stderr)
    print(f"Audit trail:    http://{host}:{port}/audit", file=sys.stderr)
    print(f"Debug log:      {diagnostic_log.path}  (python -m src.logquery sessions)", file=sys.stderr)
    print(f"Audit log:      {audit_log.path}", file=sys.stderr)
    log_server_start(args.transport, approvals_url=f"http://{host}:{port}")

    if args.transport == "http":
        mcp.run(
            transport="streamable-http",
            host=os.getenv("MCP_HOST", "127.0.0.1"),
            port=int(os.getenv("MCP_PORT", "8000")),
        )
    else:
        mcp.run()


if __name__ == "__main__":
    main()
