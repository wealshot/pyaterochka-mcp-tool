"""Запуск Telegram-бота + HTTP MCP-сервера одним файлом с живым выводом в консоль.

Показывает в реальном времени: сообщения пользователя, мысли модели,
вызовы инструментов, найденные товары и итоговые ответы.
Попутно поднимает HTTP MCP-сервер, чтобы к инструментам могла
подключиться внешняя нейросеть (Claude Desktop, Cursor и т.п.).

Запуск:  python run.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from typing import Any

from bot import TelegramBot
from config import Config
from live_timer import LiveTimer
from mcp.mcp_server import TOOLS
from mcp_http_server import run_http_mcp_server
from providers import selection_display as llm_selection_display

CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
GREY = "\033[90m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"

TOOL_LABELS: dict[str, str] = {
    "find_store": "🔍 ищу магазин по адресу",
    "find_nearest_stores": "📍 ищу ближайшие магазины",
    "get_store_info": "🏪 открываю карточку магазина",
    "get_store_hours": "🕐 смотрю часы работы",
    "search_products": "🛒 ищу товары в магазине",
    "list_category_products": "📦 открываю категорию",
    "find_products": "🛒 ищу товары",
    "get_product_promotion": "🏷️ проверяю акцию",
    "get_product_info": "📋 открываю карточку товара",
    "list_store_categories": "🗂️ открываю категории",
    "list_stores_in_area": "🗺️ ищу магазины на карте",
    "refresh_session": "🔄 обновляю сессию",
    "remember_preference": "🧠 запоминаю предпочтение",
    "present_products": "📸 формирую фото товаров",
}


def _setup_logging() -> None:
    class ClearLineHandler(logging.StreamHandler):
        """Стирает хвост таймера (строка с \r), чтобы логи не прилипали к нему."""

        def emit(self, record: logging.LogRecord) -> None:  # noqa: A003
            try:
                msg = self.format(record)
                stream = self.stream
                stream.write("\r\x1b[2K")
                stream.write(msg + self.terminator)
                self.flush()
            except Exception:  # noqa: BLE001
                self.handleError(record)

    handler = ClearLineHandler(sys.stdout)

    class _Fmt(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:  # noqa: A003
            msg = super().format(record)
            return f"{GREY}[{record.levelname}] {msg}{RESET}"

    handler.setFormatter(_Fmt("%(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)


def _print_tool_start(name: str, arguments: dict[str, Any]) -> None:
    label = TOOL_LABELS.get(name, name)
    args = ""
    if arguments:
        parts = []
        for key, value in arguments.items():
            text = str(value)
            if len(text) > 60:
                text = text[:60] + "…"
            parts.append(f"{key}={text}")
        args = " " + DIM + "(" + ", ".join(parts) + ")" + RESET
    print(f"{CYAN}  ⚙️ {label}{args}{RESET}")


def _print_products(products: list[dict[str, Any]]) -> None:
    print(f"{YELLOW}  📸 Товары для фото ({len(products)}):{RESET}")
    for product in products:
        name = product.get("name", "")
        price = product.get("price")
        price_txt = f"{price:.2f} ₽" if price is not None else "—"
        print(f"    • {name} — {price_txt}")


def make_listener() -> Any:
    """Возвращает колбэк, который печатает события агента в консоль.

    Печать жёстко ограничена по объёму: фазы («думает», таймер) печатаются
    один раз при смене состояния, а не на каждый токен — иначе скроллбэк
    терминала разрастается и съедает память.
    """
    state = {"text_open": False, "thinking_shown": False}
    timer = LiveTimer()

    def _close_text() -> None:
        if state["text_open"]:
            print(RESET)
            state["text_open"] = False

    def _end_thinking() -> None:
        if state["thinking_shown"]:
            state["thinking_shown"] = False
            timer.stop()

    def on_event(event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "user_message":
            _close_text()
            _end_thinking()
            print(f"\n{CYAN}  👤 Вы: {event['text']}{RESET}")
        elif event_type == "reset":
            _close_text()
            _end_thinking()
            print(f"{YELLOW}  ↻ модель перезапускается после сбоя…{RESET}")
        elif event_type == "thinking":
            # Один раз на фазу рассуждений, а не на каждый токен.
            if not state["thinking_shown"]:
                _close_text()
                print(f"{GREY}  … модель думает…{RESET}")
                timer.start()
                state["thinking_shown"] = True
        elif event_type == "text":
            _end_thinking()
            # NB: _close_text() здесь НЕ вызываем — он печатает перенос строки,
            # и тогда каждый токен уезжает на свою строку (та самая «каша»).
            if not state["text_open"]:
                print(f"{GREEN}  ", end="", flush=True)
                state["text_open"] = True
            print(event["text"], end="", flush=True)
        elif event_type == "llm_done":
            _close_text()
            _end_thinking()
            print(f"{GREY}  ⏱ генерация: {event['seconds']:.1f}с{RESET}")
        elif event_type == "tool_start":
            _close_text()
            _end_thinking()
            _print_tool_start(event.get("name", ""), event.get("arguments", {}))
            timer.start()
        elif event_type == "tool_done":
            _close_text()
            _end_thinking()
            secs = event.get("seconds")
            time_txt = f" ({secs:.1f}с)" if isinstance(secs, (int, float)) else ""
            if event.get("ok"):
                timer.stop(f"{GREEN}    ✅{time_txt}{RESET}")
            else:
                timer.stop(f"{RED}    ⚠️ ошибка{time_txt}: {event.get('error', '')[:150]}{RESET}")
        elif event_type == "cancelled":
            _close_text()
            _end_thinking()
            print(f"{YELLOW}  ⏹ Остановлено.{RESET}")
        elif event_type == "products":
            _close_text()
            _end_thinking()
            _print_products(event.get("products", []))
        elif event_type == "answer":
            _close_text()
            _end_thinking()
            print(f"\n{BOLD}  🤖 Ответ:{RESET}")
            print("  " + _md_to_ansi(event["text"]).replace("\n", "\n  "))
            print()

    return on_event


_ANSI = {
    "bold": "\033[1m",
    "italic": "\033[3m",
    "dim": "\033[2m",
    "cyan": "\033[96m",
    "reset": "\033[0m",
}


def _md_to_ansi(text: str) -> str:
    """Маркдаун → ANSI-коды для консоли (жирный, курсив, код, заголовки, списки)."""
    r = _ANSI["reset"]
    text = re.sub(r"```(?:\w+)?\n?(.*?)```", lambda m: f"{_ANSI['cyan']}{m.group(1)}{r}", text, flags=re.S)
    text = re.sub(r"`([^`\n]+)`", lambda m: f"{_ANSI['cyan']}{m.group(1)}{r}", text)
    text = re.sub(r"\*\*([^*]+)\*\*", lambda m: f"{_ANSI['bold']}{m.group(1)}{r}", text)
    text = re.sub(r"__(.+?)__", lambda m: f"{_ANSI['bold']}{m.group(1)}{r}", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", lambda m: f"{_ANSI['italic']}{m.group(1)}{r}", text)
    text = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", lambda m: f"{_ANSI['italic']}{m.group(1)}{r}", text)
    text = re.sub(r"^#{1,6}\s+(.+)$", lambda m: f"{_ANSI['bold']}{m.group(1)}{r}", text, flags=re.M)
    text = re.sub(r"^\s*[-*]\s+", "  • ", text, flags=re.M)
    return text


def _enable_ansi_colors() -> None:
    """Включает поддержку ANSI-кодов в консоли Windows (иначе цвета «пропадают»)."""
    if sys.platform != "win32":
        return
    os.system("")  # активирует обработку VT-последовательностей в conhost
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:  # noqa: BLE001
        pass


def _mcp_host_port() -> tuple[str, int]:
    host = os.getenv("MCP_HOST", "127.0.0.1")
    try:
        port = int(os.getenv("MCP_PORT", "8765"))
    except ValueError:
        port = 8765
    return host, port


async def main() -> None:
    _setup_logging()
    _enable_ansi_colors()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    config = Config.from_env()
    config.validate()

    app = TelegramBot(config)
    app.on_agent_event = make_listener()

    chain = config.llm_providers()
    current = llm_selection_display() or f"{config.llm_primary_model} @ {config.llm_primary_url}"
    print(f"{BOLD}🤖 Telegram-бот «Пятёрочка» + HTTP MCP{RESET}\n")
    print(f"  Модель: {GREEN}{current}{RESET}")
    fallbacks = [f"{p.model}" for p in chain[1:]]
    if fallbacks:
        print(f"  Фолбэк: {GREEN}{' → '.join(fallbacks)}{RESET}")
    print(f"  API:    {config.telegram_api_base_url}\n")

    mcp_host, mcp_port = _mcp_host_port()
    mcp_task = asyncio.create_task(run_http_mcp_server(mcp_host, mcp_port))
    await asyncio.sleep(0.5)
    print(f"  MCP:    {GREEN}http://{mcp_host}:{mcp_port}/mcp{RESET} — HTTP MCP для внешних нейросетей")
    print(f"  Tools:  {GREY}{len(TOOLS)} MCP-инструментов каталога Пятёрочки{RESET}")
    print(f"{GREY}Ctrl+C — остановить.{RESET}\n")

    async with app.start():
        print(f"{GREEN}✅ Бот запущен и слушает сообщения.{RESET}\n")
        try:
            await app.dp.start_polling(app.bot)
        except KeyboardInterrupt:
            print(f"\n{GREY}Останавливаю…{RESET}")
        finally:
            mcp_task.cancel()
            try:
                await mcp_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


if __name__ == "__main__":
    asyncio.run(main())
