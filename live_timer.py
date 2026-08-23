"""Живой таймер фаз для консоли: тикающие секунды на одной строке.

Счётчик «⏱ Nс» печатается сразу после старта (без задержки) и
перерисовывается раз в секунду до остановки фазы.
"""

from __future__ import annotations

import asyncio
import time

RESET = "\033[0m"
GREY = "\033[90m"


class LiveTimer:
    """Фоновый тикер: перерисовывает одну строку в консоли через \\r."""

    def __init__(self, prefix: str = "⏱ ") -> None:
        self.prefix = prefix
        self._started: float | None = None
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        """Запускает отсчёт. Первый кадр печатается сразу (0с)."""
        self._cancel()
        self._started = time.monotonic()
        self._print_tick(0.0)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = loop.create_task(self._tick_loop())

    def stop(self, final: str = "") -> None:
        """Стирает строку таймера. final — строка, печатаемая вместо неё."""
        self._cancel()
        clear = "\r" + " " * 60 + "\r"
        if final:
            print(f"{clear}{final}", flush=True)
        else:
            print(clear, end="", flush=True)

    def _cancel(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    def _print_tick(self, seconds: float) -> None:
        print(f"\r{GREY}{self.prefix}{seconds:.0f}с{RESET}", end="", flush=True)

    async def _tick_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(1.0)
                if self._started is not None:
                    self._print_tick(time.monotonic() - self._started)
        except asyncio.CancelledError:
            pass
