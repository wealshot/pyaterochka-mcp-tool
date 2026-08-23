"""HTTP (Streamable HTTP) транспорт для MCP-сервера Пятёрочки.

Даёт внешним нейросетям и AI-инструментам (Claude Desktop, opencode, Cursor,
MCP Inspector и т.п.) возможность подключиться по адресу:

    http://127.0.0.1:8765/mcp

и использовать те же инструменты, что и Telegram-бот: поиск магазинов,
товаров, акций, категорий и карточек.

Чтобы открыть сервер в интернет (например, для Claude/ChatGPT с remote MCP),
поднимите туннель — ngrok, cloudflared или любой аналог:

    ngrok http 8765          # → https://<случайный>.ngrok-free.app/mcp
    cloudflared tunnel --url http://localhost:8765

Реализация опирается на существующий ``MCPServer.handle()`` (JSON-RPC поверх
stdio) и оборачивает его в Streamable HTTP транспорт: POST с JSON-RPC телом
возвращает JSON-ответ (как это делает официальный SDK клиент), GET держит
SSE-соединение, уведомления получают 202 Accepted. Внешних зависимостей не
добавляет — использует уже установленный aiohttp.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from aiohttp import web

from mcp.mcp_server import MCPServer, TOOLS, SERVER_NAME, SERVER_VERSION

logger = logging.getLogger(__name__)

CONTENT_TYPE_JSON = "application/json"
CONTENT_TYPE_SSE = "text/event-stream"
# NB: без charset внутри content_type — aiohttp добавляет его сам через charset=
# (иначе Response(text=...) кидает ошибку и эндпоинт отдаёт HTTP 500).
CONTENT_TYPE_TEXT = "text/plain"


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
    )


def _cors_headers(allow_sse: bool = False) -> dict[str, str]:
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization, "
        "mcp-session-id, mcp-protocol-version, last-event-id",
    }
    if allow_sse:
        headers["Cache-Control"] = "no-cache, no-transform"
        headers["Connection"] = "keep-alive"
    return headers


class McpHttpHandler:
    """Обёртка над MCPServer для HTTP-транспорта MCP."""

    def __init__(self, server: MCPServer, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.server = server
        self.connect_url = f"http://{host}:{port}/mcp"

    async def _process_message(self, body: dict[str, Any]) -> web.Response:
        if not isinstance(body, dict):
            return _json_response(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "Invalid Request"},
                },
                status=400,
            )
        # Уведомления (без id) не должны получать тело ответа — 202 Accepted.
        response = await self.server.handle(body)
        if response is None:
            return web.Response(status=202, headers=_cors_headers())
        return _json_response(response, status=200)

    async def on_post(self, request: web.Request) -> web.Response:
        content_type = (request.content_type or "").split(";")[0].strip().lower()
        if content_type != CONTENT_TYPE_JSON:
            return _json_response(
                {"error": "Unsupported Media Type: Content-Type must be application/json"},
                status=415,
            )
        try:
            body = await request.json()
        except Exception:
            return _json_response(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
                status=400,
            )
        return await self._process_message(body)

    async def on_options(self, request: web.Request) -> web.Response:
        return web.Response(status=204, headers=_cors_headers())

    async def on_get(self, request: web.Request) -> web.StreamResponse:
        """SSE-поток для сообщений сервера (не используется, держим живым)."""
        response = web.StreamResponse(headers=_cors_headers(allow_sse=True))
        response.headers["Content-Type"] = CONTENT_TYPE_SSE
        await response.prepare(request)
        try:
            while True:
                await response.write(b": keep-alive\n\n")
                await asyncio.sleep(15)
        except (asyncio.CancelledError, ConnectionResetError, RuntimeError):
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
                "tools": [t["name"] for t in TOOLS],
            }
        )

    async def on_health(self, request: web.Request) -> web.Response:
        return web.Response(
            text="ok",
            content_type=CONTENT_TYPE_TEXT,
            charset="utf-8",
            status=200,
            headers=_cors_headers(),
        )


async def run_http_mcp_server(host: str = "127.0.0.1", port: int = 8765) -> None:
    """Поднимает HTTP MCP-сервер и держит его, пока не отменят задачу."""
    server = MCPServer()
    await server.start()
    handler = McpHttpHandler(server, host, port)
    app = web.Application()
    app.router.add_get("/", handler.on_index)
    app.router.add_get("/health", handler.on_health)
    app.router.add_get("/mcp", handler.on_get)
    app.router.add_options("/mcp", handler.on_options)
    app.router.add_post("/mcp", handler.on_post)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("HTTP MCP server запущен: http://%s:%d/mcp", host, port)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await server.close()


def main() -> None:
    """Автономный запуск только HTTP MCP-сервера (без Telegram-бота)."""
    host = os.getenv("MCP_HOST", "127.0.0.1")
    try:
        port = int(os.getenv("MCP_PORT", "8765"))
    except ValueError:
        port = 8765
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(run_http_mcp_server(host, port))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
