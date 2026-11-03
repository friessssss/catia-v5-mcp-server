"""stdio <-> HTTP bridge to the background service (``catia-mcp proxy``).

Claude Desktop only spawns local stdio servers. This bridge reads
newline-delimited JSON-RPC from stdin, POSTs each message to the service's
stateless /mcp endpoint and writes the JSON reply to stdout. Requests run
concurrently (a ping is answered while a long CATIA call is in flight);
replies are written whole, one per line.

If the service is not running it is started. If it restarts between two
calls nothing is lost: the endpoint is stateless, so the next request just
goes to the new process.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
from typing import Any

import httpx

from catia_mcp.service import ensure_running, mcp_url

logger = logging.getLogger("catia_mcp.proxy")

_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def _error_reply(message: dict[str, Any], text: str) -> dict[str, Any] | None:
    if "id" not in message or "method" not in message:
        return None  # notifications and responses get no reply
    return {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32603, "message": text}}


class Proxy:
    def __init__(self, port: int, token: str | None = None) -> None:
        self.port = port
        self.url = mcp_url(port)
        self.headers = dict(_HEADERS)
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
        self._write_lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(None, connect=5.0),
            # 127.0.0.1 must never go through a corporate HTTP proxy.
            trust_env=False,
        )

    async def _write(self, payload: Any) -> None:
        # Bytes, always UTF-8: on Windows a piped sys.stdout uses the ANSI
        # code page, which cannot encode e.g. the "∩" in a tool description.
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._write_lock:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()

    async def _post(self, body: bytes) -> httpx.Response:
        try:
            return await self._client.post(self.url, content=body, headers=self.headers)
        except httpx.ConnectError:
            # Service not up (first call after logon, crash, upgrade): start it.
            await asyncio.to_thread(ensure_running, self.port)
            return await self._client.post(self.url, content=body, headers=self.headers)

    async def handle(self, raw: str) -> None:
        try:
            message = json.loads(raw)
        except ValueError:
            logger.warning("Dropping non-JSON line from client: %r", raw[:200])
            return
        try:
            await self._relay(message, raw)
        except Exception as error:
            logger.exception("Proxy failed on %s", message.get("method"))
            reply = _error_reply(message, f"CATIA MCP proxy error: {error}")
            if reply:
                await self._write(reply)

    async def _relay(self, message: dict[str, Any], raw: str) -> None:
        try:
            response = await self._post(raw.encode("utf-8"))
        except Exception as error:  # noqa: BLE001 -- reported to the client
            reply = _error_reply(message, f"CATIA MCP service unreachable: {error}")
            if reply:
                await self._write(reply)
            return
        if response.status_code == 202 or not response.content:
            return
        if response.status_code >= 400:
            reply = _error_reply(
                message, f"CATIA MCP service HTTP {response.status_code}: {response.text[:500]}"
            )
            if reply:
                await self._write(reply)
            return
        for payload in _payloads(response):
            await self._write(payload)

    async def run(self) -> None:
        # Reading stdin on a thread works on every platform (Windows pipes do
        # not support the event loop's connect_read_pipe).
        await asyncio.to_thread(ensure_running, self.port)
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()

        def pump() -> None:
            for line in sys.stdin.buffer:
                loop.call_soon_threadsafe(queue.put_nowait, line)
            loop.call_soon_threadsafe(queue.put_nowait, None)

        threading.Thread(target=pump, daemon=True, name="stdin").start()
        tasks: set[asyncio.Task] = set()
        while (line := await queue.get()) is not None:
            text = line.decode("utf-8").strip()
            if not text:
                continue
            task = asyncio.create_task(self.handle(text))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._client.aclose()


def _payloads(response: httpx.Response) -> list[Any]:
    """JSON body, or the data lines of an SSE body."""
    kind = response.headers.get("content-type", "")
    if "text/event-stream" in kind:
        out = []
        for line in response.text.splitlines():
            if line.startswith("data:"):
                data = line[5:].strip()
                if data:
                    out.append(json.loads(data))
        return out
    return [response.json()]


def run_proxy(port: int, token: str | None = None) -> None:
    asyncio.run(Proxy(port, token).run())
