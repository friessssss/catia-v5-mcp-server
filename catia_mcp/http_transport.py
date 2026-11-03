"""Remote access: serve the MCP tools over Streamable HTTP.

The default transport stays stdio (the agent and CATIA on the same Windows
machine). This module lets the server run on the Windows/CATIA machine while
the agent runs elsewhere: a Mac, a laptop, or a client reaching a cloud
Windows workstation.

Security model, deliberately conservative because every tool drives a live
CAD session:
- Binds to 127.0.0.1 by default. Reaching it from another machine requires
  an explicit --host and a bearer token; the server refuses to start on a
  non-loopback address without one.
- Every request must carry "Authorization: Bearer <token>" when a token is
  set, compared in constant time.
- On loopback, DNS-rebinding protection only accepts localhost Host headers.
- No TLS here: expose it through a private network (Tailscale, WireGuard,
  an SSH tunnel) or a TLS reverse proxy, never directly on the internet.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import secrets
from collections.abc import Callable
from typing import Any

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings

logger = logging.getLogger("catia_mcp.http")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MCP_PATH = "/mcp"
HEALTH_PATH = "/health"


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_bind(host: str, token: str | None) -> None:
    """Refuse to expose the server beyond this machine without a token."""
    if not is_loopback(host) and not token:
        raise SystemExit(
            f"Refusing to listen on {host} without a token: anyone on the network "
            "could drive CATIA. Set CATIA_MCP_TOKEN or pass --token (generate one "
            "with: catia-mcp --new-token)."
        )
    if token is not None and len(token) < 16:
        raise SystemExit("The token must be at least 16 characters long.")


def security_settings(host: str, port: int) -> TransportSecuritySettings:
    if is_loopback(host):
        local = [f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"]
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=local, allowed_origins=[]
        )
    # Remote clients reach the machine under a name we cannot know in advance
    # (LAN IP, Tailscale name, proxy). The bearer token is the gate there.
    return TransportSecuritySettings(enable_dns_rebinding_protection=False)


async def _send_json(send: Any, status: int, payload: dict[str, Any]) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": json.dumps(payload).encode()})


async def _send_status(send: Any, status: int, message: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"www-authenticate", b"Bearer"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": message})


def build_app(
    mcp_server: Any,
    host: str,
    port: int,
    token: str | None,
    status: Callable[[], dict[str, Any]] | None = None,
) -> Any:
    """Return an ASGI app exposing the MCP server at /mcp and a health probe
    at /health.

    Sessions are stateless with plain JSON responses: every request stands
    alone, so restarting the server (or CATIA) never strands a client on an
    expired session id, and simple bridges (catia-mcp proxy) can relay
    requests one POST at a time. The tools need no server-to-client
    requests, which is all stateless mode gives up."""
    manager = StreamableHTTPSessionManager(
        app=mcp_server,
        json_response=True,
        stateless=True,
        security_settings=security_settings(host, port),
    )
    expected = f"Bearer {token}".encode() if token else None

    async def app(scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            # The session manager must run for the whole server lifetime.
            message = await receive()
            if message["type"] != "lifespan.startup":
                return
            async with manager.run():
                await send({"type": "lifespan.startup.complete"})
                while (await receive())["type"] != "lifespan.shutdown":
                    pass
            await send({"type": "lifespan.shutdown.complete"})
            return
        if scope["type"] != "http":
            return
        path = scope.get("path", "").rstrip("/")
        if path not in (MCP_PATH, HEALTH_PATH):
            await _send_status(send, 404, b"Not found. The MCP endpoint is /mcp.")
            return
        if expected is not None:
            headers = dict(scope.get("headers") or [])
            supplied = headers.get(b"authorization", b"")
            if not hmac.compare_digest(supplied, expected):
                logger.warning("Rejected unauthenticated request from %s", scope.get("client"))
                await _send_status(send, 401, b"Missing or invalid bearer token.")
                return
        if path == HEALTH_PATH:
            await _send_json(send, 200, status() if status else {"status": "ok"})
            return
        await manager.handle_request(scope, receive, send)

    return app


async def serve(
    mcp_server: Any,
    host: str,
    port: int,
    token: str | None,
    status: Callable[[], dict[str, Any]] | None = None,
) -> None:
    import uvicorn

    check_bind(host, token)
    app = build_app(mcp_server, host, port, token, status)
    shown = "set" if token else "none (loopback only)"
    logger.info("Serving MCP over HTTP at http://%s:%d%s (token: %s)", host, port, MCP_PATH, shown)
    # log_config=None keeps uvicorn on the root logger (our rotating file);
    # its default config writes to a console that pythonw.exe does not have.
    # /health is polled by clients and `catia-mcp status`; keep it out of the log.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    config = uvicorn.Config(
        app, host=host, port=port, log_level="info", lifespan="on", log_config=None
    )
    await uvicorn.Server(config).serve()


def new_token() -> str:
    return secrets.token_urlsafe(32)
