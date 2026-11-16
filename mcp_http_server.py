
"""HTTP MCP сервер каталога Пятёрочки для Render."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from typing import Any
from pathlib import Path

from aiohttp import web
from mcp.mcp_server import MCPServer, TOOLS, SERVER_NAME, SERVER_VERSION

logger = logging.getLogger(__name__)

CONTENT_TYPE_JSON = "application/json"
CONTENT_TYPE_SSE = "text/event-stream"
CONTENT_TYPE_TEXT = "text/plain"


def _cors_headers(allow_sse: bool = False) -> dict[str, str]:
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": (
            "Content-Type, Authorization, mcp-session-id, "
            "mcp-protocol-version, last-event-id"
        ),
    }
    if allow_sse:
        headers["Cache-Control"] = "no-cache, no-transform"
        headers["Connection"] = "keep-alive"
    return headers


def _json_response(
    data: dict[str, Any] | list[dict[str, Any]],
    *,
    status: int = 200,
) -> web.Response:
    return web.Response(
        text=json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        status=status,
        content_type=CONTENT_TYPE_JSON,
        charset="utf-8",
        headers=_cors_headers(),
    )


@web.middleware
async def auth_middleware(request: web.Request, handler):
    token = os.getenv("MCP_AUTH_TOKEN", "")

    if request.path != "/mcp" or not token:
        return await handler(request)

    authorization = request.headers.get("Authorization", "")
    expected = f"Bearer {token}"

    if not hmac.compare_digest(authorization, expected):
        return _json_response(
            {"error": "Unauthorized"},
            status=401,
        )

    return await handler(request)


class McpHttpHandler:

    def __init__(
        self,
        server: MCPServer,
        host: str,
        port: int,
    ) -> None:
        self.server = server
        self.connect_url = f"http://{host}:{port}/mcp"

    async def _process_message(self, body: dict[str, Any]) -> web.Response:
        if not isinstance(body, dict):
            return _json_response(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32600,
                        "message": "Invalid Request",
                    },
                },
                status=400,
            )

        response = await self.server.handle(body)

        if response is None:
            return web.Response(
                status=202,
                headers=_cors_headers(),
            )

        return _json_response(response)

    async def on_post(self, request: web.Request) -> web.Response:
        content_type = (
            request.content_type or ""
        ).split(";")[0].strip().lower()

        if content_type != CONTENT_TYPE_JSON:
            return _json_response(
                {"error": "Content-Type must be application/json"},
                status=415,
            )

        try:
            body = await request.json()
        except Exception:
            return _json_response(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32700,
                        "message": "Parse error",
                    },
                },
                status=400,
            )

        return await self._process_message(body)

    async def on_options(self, request: web.Request) -> web.Response:
        return web.Response(
            status=204,
            headers=_cors_headers(),
        )

    async def on_get(self, request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(
            headers=_cors_headers(allow_sse=True)
        )
        response.headers["Content-Type"] = CONTENT_TYPE_SSE

        await response.prepare(request)

        try:
            while True:
                await response.write(b": keep-alive\n\n")
                await asyncio.sleep(15)
        except (
            asyncio.CancelledError,
            ConnectionResetError,
            RuntimeError,
        ):
            pass

        return response

    async def on_index(self, request: web.Request) -> web.Response:
        return _json_response(
            {
                "server": SERVER_NAME,
                "version": SERVER_VERSION,
                "endpoint": "/mcp",
                "connect": self.connect_url,
                "tools_count": len(TOOLS),
                "tools": [tool["name"] for tool in TOOLS],
            }
        )


    async def on_app_api(self, request: web.Request) -> web.Response:
        """Ограниченный публичный API мобильной страницы без выдачи MCP-токена."""
        content_type = (request.content_type or "").split(";")[0].strip().lower()
        if content_type != CONTENT_TYPE_JSON:
            return _json_response({"error": "Content-Type must be application/json"}, status=415)
        try:
            body = await request.json()
        except Exception:
            return _json_response({"error": "Invalid JSON"}, status=400)

        if not isinstance(body, dict) or body.get("method") != "tools/call":
            return _json_response({"error": "Only product search is available"}, status=400)

        params = body.get("params") or {}
        if params.get("name") != "find_products":
            return _json_response({"error": "Only find_products is available"}, status=403)

        supplied = params.get("arguments") or {}
        query = str(supplied.get("query", "")).strip()
        if not query or len(query) > 100:
            return _json_response({"error": "Укажи название товара (до 100 символов)"}, status=400)

        # Запросы через публичную страницу всегда ограничены одним выбранным магазином.
        body = {
            "jsonrpc": "2.0",
            "id": body.get("id"),
            "method": "tools/call",
            "params": {
                "name": "find_products",
                "arguments": {
                    "address": "Биробиджан, ул. Советская, д. 68 к. 1",
                    "query": query,
                    "limit": 20,
                    "sort": "price_asc",
                },
            },
        }
        return await self._process_message(body)

    async def on_health(self, request: web.Request) -> web.Response:
        return web.Response(
            text="ok",
            content_type=CONTENT_TYPE_TEXT,
            charset="utf-8",
            headers=_cors_headers(),
        )

    async def on_app(self, request: web.Request) -> web.Response:
        app_file = Path(__file__).with_name("app.html")
        if not app_file.is_file():
            return web.Response(text="Mobile app file not found", status=404)
        return web.FileResponse(app_file)


async def run_http_mcp_server(
    host: str = "0.0.0.0",
    port: int = 8765,
) -> None:
    server = MCPServer()
    await server.start()

    handler = McpHttpHandler(server, host, port)

    app = web.Application(
        middlewares=[auth_middleware]
    )

    app.router.add_get("/", handler.on_index)
    app.router.add_get("/app", handler.on_app)
    app.router.add_post("/app-api", handler.on_app_api)
    app.router.add_options("/app-api", handler.on_options)
    app.router.add_get("/health", handler.on_health)
    app.router.add_get("/mcp", handler.on_get)
    app.router.add_options("/mcp", handler.on_options)
    app.router.add_post("/mcp", handler.on_post)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, host, port)
    await site.start()

    logger.info(
        "HTTP MCP server started on %s:%s",
        host,
        port,
    )

    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await server.close()


def main() -> None:
    host = os.getenv("MCP_HOST", "0.0.0.0")

    try:
        port = int(os.getenv("PORT", os.getenv("MCP_PORT", "8765")))
    except ValueError:
        port = 8765

    logging.basicConfig(level=logging.INFO)

    try:
        asyncio.run(run_http_mcp_server(host, port))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
