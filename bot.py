"""Telegram-бот: aiogram + агент с инструментами Пятёрочки.

LLM — любой OpenAI-совместимый провайдер из .env (с автофолбэком),
инструменты — встроенный MCP-сервер Пятёрочки. Работает как с официальным
api.telegram.org, так и с локальным Telegram Bot API сервером
(в последнем случае доступен ещё и стриминг ответа через sendMessageDraft).
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import math
import random
import re
import sys
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

import aiohttp

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Location,
    Message,
)

from agent import AgentSession, PyaterochkaAgent
from config import OFFICIAL_TELEGRAM_API, Config
from llm_client import LLMClient, LLMError
from mcp.mcp_server import MCPServer
from providers import PROVIDERS, api_key_for, by_id, selection_display, selection_reset, selection_save
from stats import StatsLogger

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("bot")

TELEGRAM_MAX_MESSAGE = 4096

# Жёсткий потолок накопленного черновика стриминга в памяти: старые куски
# выбрасываются, чтобы длинные генерации не разрастались без предела.
MAX_DRAFT_SEGMENTS_CHARS = 3500

WELCOME_TEXT = """<b>Привет! Я схожу за тебя в «Пятёрочку»!</b>

Я умею:
• найти ближайший магазин и запомнить его как избранный;
• искать товары и показывать цены, акции и фото;
• учитывать цену по карте и скидки «при покупке N штук».

<b>Как начать:</b>
1️⃣ Отправь мне <b>геолокацию</b> (скрепка → 📍 Location) — я покажу ближайшие магазины с расстоянием, и ты выберешь избранный кнопками.
2️⃣ Или просто напиши адрес, например: <i>Москва, Кировоградская улица, 17</i>.

Потом спрашивай: «найди молоко», «что со скидкой на кофе?», «часы работы магазина».

Команда /reset — сбросить избранный магазин и историю.
Команда /stop — остановить текущее выполнение.
Команда /model — сменить модель и провайдера (инлайн)."""


def _chunk_text(text: str) -> list[str]:
    """Режет длинный HTML-текст на части ≤4096 без разрыва тегов и сущностей,
    закрывая открытые теги в конце каждой части."""
    chunks: list[str] = []
    start = 0
    total = len(text)
    while start < total:
        end = min(start + TELEGRAM_MAX_MESSAGE, total)
        if end < total:
            seg = text[start:end]
            last_lt = seg.rfind("<")
            last_gt = seg.rfind(">")
            # Если хвост сегмента — незакрытый тег, резать можно только ДО него.
            ceiling = last_lt if last_lt > last_gt else len(seg)
            floor = last_gt + 1
            cut = max(seg.rfind("\n", 0, ceiling), seg.rfind(" ", 0, ceiling))
            if cut >= max(floor, TELEGRAM_MAX_MESSAGE // 3):
                end = start + cut + 1
            elif last_gt >= 0:
                end = start + last_gt + 1  # хотя бы сразу после закрытого тега
        part = _close_open_tags(text[start:end])
        if part.strip():
            chunks.append(part)
        start = end
    return chunks or [text]


def _close_open_tags(html_text: str) -> str:
    """Закрывает незакрытые простые теги (b/i/s/u/code/a) в конце фрагмента —
    иначе Telegram отклоняет сообщение с «Can't find end tag»."""
    # сначала откусываем оборванный тег без закрытия ">" (например, хвост "<a hre")
    last_open = html_text.rfind("<")
    if last_open > html_text.rfind(">"):
        html_text = html_text[:last_open].rstrip()
    stack: list[str] = []
    for m in re.finditer(r"<(/?)(b|i|s|u|code|a|pre)(?=[\s>/])[^>]*?>", html_text):
        closing, name = m.group(1) == "/", m.group(2)
        if closing:
            if stack and stack[-1] == name:
                stack.pop()
        else:
            stack.append(name)
    return html_text + "".join(f"</{name}>" for name in reversed(stack))


def _safe_html_cut(html_text: str, limit: int) -> str:
    """Безопасно укорачивает HTML до limit символов: не рвёт теги и &сущности,
    закрывает оставшиеся открытыми теги."""
    if len(html_text) <= limit:
        return html_text
    cut = html_text[:limit]
    last_open = cut.rfind("<")
    if last_open > cut.rfind(">"):
        cut = cut[:last_open]
    amp = cut.rfind("&")
    if amp != -1 and ";" not in cut[amp:] and len(cut) - amp <= 10:
        cut = cut[:amp]
    return _close_open_tags(cut.rstrip())


class _PendingJob:
    """Отложенный запрос в очереди обработки."""

    __slots__ = ("chat_id", "coro", "cancelled")

    def __init__(self, chat_id: int, coro: Any) -> None:
        self.chat_id = chat_id
        self.coro = coro
        self.cancelled = False


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


def _md_to_html(text: str) -> str:
    """Безопасно конвертирует markdown от LLM в HTML для Telegram."""
    text = html.escape(text)

    def _code(match: re.Match[str]) -> str:
        return f"<code>{match.group(1)}</code>"

    def _bold(match: re.Match[str]) -> str:
        return f"<b>{match.group(1)}</b>"

    def _italic(match: re.Match[str]) -> str:
        return f"<i>{match.group(1)}</i>"

    text = re.sub(r"```(?:\w+)?\n(.*?)```", _code, text, flags=re.S)
    text = re.sub(r"`([^`\n]+)`", _code, text)
    text = re.sub(r"\*\*([^*]+)\*\*", _bold, text)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", _italic, text)
    text = re.sub(r"^###?\s+(.+)$", lambda m: f"<b>{m.group(1)}</b>", text, flags=re.M)
    text = re.sub(r"^\s*[-*]\s+", "• ", text, flags=re.M)
    return text


class TelegramBot:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.sessions: dict[int, AgentSession] = defaultdict(AgentSession)
        self.agent: PyaterochkaAgent | None = None
        self.llm: LLMClient | None = None
        self.on_agent_event: Callable[[dict[str, Any]], None] | None = None
        self._sessions_file = Path(__file__).resolve().parent / "sessions.json"
        self._http: aiohttp.ClientSession | None = None
        self._run_tasks: dict[int, asyncio.Task] = {}
        self._stop_flags: set[int] = set()
        self._draft_cooldown: dict[int, float] = {}
        # Последовательная обработка: один worker, FIFO. Никаких параллельных
        # генераций — они задваивают черновики и душат Telegram rate-limit'ами.
        self._queue: asyncio.Queue[_PendingJob] = asyncio.Queue()
        self._queued: dict[int, list[_PendingJob]] = defaultdict(list)
        self._worker_task: asyncio.Task | None = None
        self._warmup_task: asyncio.Task | None = None
        self.stats = StatsLogger()
        self._load_sessions()

        session_kwargs: dict = {}
        if config.telegram_proxy:
            session_kwargs["proxy"] = config.telegram_proxy
        if config.telegram_api_base_url != OFFICIAL_TELEGRAM_API:
            session_kwargs["api"] = TelegramAPIServer.from_base(config.telegram_api_base_url)
        # sendMessageDraft (стриминг-черновик) есть только в локальных
        # Telegram Bot API серверах; официальный api.telegram.org его не знает.
        self._drafts_supported = config.telegram_api_base_url != OFFICIAL_TELEGRAM_API

        self.bot = Bot(
            token=config.telegram_token,
            session=AiohttpSession(**session_kwargs),
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self.dp = Dispatcher()
        self._register_handlers()

    def _load_sessions(self) -> None:
        """Загружает избранные магазины и предпочтения из файла."""
        try:
            if not self._sessions_file.is_file():
                return
            data = json.loads(self._sessions_file.read_text(encoding="utf-8"))
            for chat_id, payload in (data or {}).items():
                try:
                    self.sessions[int(chat_id)] = AgentSession.from_dict(payload)
                except (KeyError, TypeError, ValueError):
                    logger.warning("Пропускаю повреждённую сессию chat=%s", chat_id)
            logger.info("Загружено сессий из памяти: %d", len(self.sessions))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось загрузить память сессий: %s", exc)

    def _save_sessions(self) -> None:
        """Сохраняет память (магазин + предпочтения) на диск."""
        try:
            data = {str(chat_id): session.to_dict() for chat_id, session in self.sessions.items()}
            self._sessions_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось сохранить память сессий: %s", exc)

    def _register_handlers(self) -> None:
        @self.dp.message(Command("start", "help"))
        async def on_start(message: Message) -> None:
            logger.info("[chat=%d] /start от %s", message.chat.id, message.from_user.username or message.from_user.id)
            await message.answer(WELCOME_TEXT)

        @self.dp.message(Command("reset"))
        async def on_reset(message: Message) -> None:
            self.sessions.pop(message.chat.id, None)
            self._save_sessions()
            logger.info("[chat=%d] /reset — сброс истории и избранного", message.chat.id)
            await message.answer("История и избранный магазин сброшены. Напиши адрес или пришли геолокацию.")

        @self.dp.message(Command("stop"))
        async def on_stop(message: Message) -> None:
            chat_id = message.chat.id
            self._stop_flags.add(chat_id)
            cancelled_running = False
            task = self._run_tasks.get(chat_id)
            if task and not task.done():
                task.cancel()
                cancelled_running = True
            queued = 0
            for job in list(self._queued.get(chat_id, [])):
                job.cancelled = True
                queued += 1
            if cancelled_running and queued:
                await message.answer(f"⏹ Останавливаю… (и отменяю {queued} из очереди)")
            elif cancelled_running:
                logger.info("[chat=%d] /stop — останавливаю выполнение", chat_id)
                await message.answer("⏹ Останавливаю…")
            elif queued:
                await message.answer(f"⏹ Отменил {queued} запрос(ов) из очереди.")
            else:
                self._stop_flags.discard(chat_id)
                await message.answer("Сейчас ничего не выполняется.")

        @self.dp.message(Command("model"))
        async def on_model(message: Message) -> None:
            current = selection_display() or f"{self.config.llm_primary_model} @ {self.config.llm_primary_url}"
            logger.info("[chat=%d] /model — открыт выбор модели", message.chat.id)
            text = (
                "⚙️ <b>Управление моделью</b>\n\n"
                f"Текущая: <b>{current}</b>\n\n"
                "Выбери провайдера, затем модель. Смена применяется сразу, без перезапуска."
            )
            await message.answer(text, reply_markup=self._model_providers_keyboard())

        @self.dp.callback_query(F.data == "model:prov")
        async def on_model_providers(call: CallbackQuery) -> None:
            await call.answer()
            await call.message.edit_text("⚙️ Выбери провайдера:", reply_markup=self._model_providers_keyboard())

        @self.dp.callback_query(F.data.startswith("model:list:"))
        async def on_model_list(call: CallbackQuery) -> None:
            _, _, provider_id, page = call.data.split(":")
            provider = by_id(provider_id)
            if provider is None:
                await call.answer("Провайдер не найден", show_alert=True)
                return
            keyboard, pages = self._model_models_keyboard(provider_id, int(page))
            await call.answer()
            await call.message.edit_text(
                f"⚙️ <b>{provider['name']}</b>\nВыбери модель ({int(page) + 1}/{pages}):",
                reply_markup=keyboard,
            )

        @self.dp.callback_query(F.data.startswith("model:pick:"))
        async def on_model_pick(call: CallbackQuery) -> None:
            _, _, provider_id, index = call.data.split(":")
            provider = by_id(provider_id)
            if provider is None:
                await call.answer("Провайдер не найден", show_alert=True)
                return
            try:
                model = provider["models"][int(index)]
            except (IndexError, ValueError):
                await call.answer("Модель не найдена", show_alert=True)
                return
            mid, label = model["id"], model["label"]
            note = model.get("note")
            if note:
                await call.answer(f"⚠️ {label}: {note}", show_alert=True)
                return
            key = api_key_for(provider)
            if not key:
                await call.answer("Для этого провайдера нет API-ключа", show_alert=True)
                return
            selection_save(
                {
                    "api_url": provider["api_url"],
                    "api_key": key,
                    "model": mid,
                    "provider": provider_id,
                    "label": label,
                }
            )
            await self._swap_llm()
            await call.answer()
            text = (
                f"✅ Модель установлена: <b>{label}</b>\n"
                f"<code>{mid}</code>\n"
                f"Провайдер: {provider['name']}\n\n"
                "Действует сразу, без перезапуска."
            )
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🔙 Провайдеры", callback_data="model:prov")],
                    [InlineKeyboardButton(text="✖️ Закрыть", callback_data="model:close")],
                ]
            )
            await call.message.edit_text(text, reply_markup=keyboard)
            logger.info("[chat=%d] модель установлена: %s @ %s", call.message.chat.id, mid, provider_id)

        @self.dp.callback_query(F.data == "model:reset")
        async def on_model_reset(call: CallbackQuery) -> None:
            selection_reset()
            await self._swap_llm()
            await call.answer()
            text = (
                "↩️ Сброшено на модель из <code>.env</code>:\n"
                f"<b>{self.config.llm_primary_model}</b> @ {self.config.llm_primary_url}"
            )
            await call.message.edit_text(text, reply_markup=self._model_providers_keyboard())

        @self.dp.callback_query(F.data == "model:close")
        async def on_model_close(call: CallbackQuery) -> None:
            await call.answer()
            try:
                await call.message.delete()
            except Exception:  # noqa: BLE001
                await call.message.edit_text("Закрыто.")

        @self.dp.callback_query(F.data == "fav:manual")
        async def on_manual_address(call: CallbackQuery) -> None:
            await call.answer()
            logger.info("[chat=%d] callback fav:manual — просим адрес вручную", call.message.chat.id)
            await call.message.answer("Напиши адрес, например: <i>Москва, Кировоградская улица, 17</i>")

        @self.dp.callback_query(F.data.startswith("fav:"))
        async def on_favorite_callback(call: CallbackQuery) -> None:
            store_id = call.data.removeprefix("fav:")
            await call.answer()
            assert self.agent is not None
            logger.info("[chat=%d] callback fav:%s — выбор избранного магазина", call.message.chat.id, store_id)
            try:
                result = await self.agent.mcp.call_tool("get_store_info", {"store_id": store_id})
                store = result["store"]
            except Exception as exc:  # noqa: BLE001
                logger.exception("[chat=%d] get_store_info(%s) failed", call.message.chat.id, store_id)
                await call.message.answer(f"Не удалось получить карточку магазина: {exc}")
                return
            session = self.sessions[call.message.chat.id]
            session.set_favorite(store)
            self._save_sessions()
            logger.info("[chat=%d] избранный магазин: %s (%s)", call.message.chat.id, store.get("store_id"), store.get("address"))
            hours = "круглосуточно" if store.get("is_24h") else (
                f"{store.get('work_start_time')}–{store.get('work_end_time')}"
            )
            line = f"🏪 <b>{store.get('address')}</b>"
            parts = []
            if store.get("metro"):
                parts.append(f"метро: {store['metro']}")
            parts.append(f"часы: {hours}")
            if parts:
                line += "\n" + " • ".join(parts)
            line += "\n\nМагазин сохранён как <b>избранный</b>. Что ищем?"
            await call.message.answer(line)

        @self.dp.message(F.location)
        async def on_location(message: Message) -> None:
            location: Location = message.location
            assert self.agent is not None
            logger.info("[chat=%d] геолокация: %.6f, %.6f", message.chat.id, location.latitude, location.longitude)
            try:
                result = await self.agent.mcp.call_tool(
                    "find_nearest_stores",
                    {"latitude": location.latitude, "longitude": location.longitude, "limit": 6},
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("nearest stores failed")
                await message.answer(f"Не удалось найти магазины рядом: {exc}")
                return
            stores = result.get("stores", [])
            logger.info("[chat=%d] найдено магазинов рядом: %d", message.chat.id, len(stores))
            if not stores:
                await message.answer("Рядом не нашлось магазинов. Попробуй написать адрес вручную.")
                return
            buttons = []
            for store in stores:
                distance = store.get("distance_m")
                suffix = f" — {int(distance)} м" if distance is not None else ""
                label = f"{store['address']}{suffix}"
                if len(label) > 60:
                    label = label[:59] + "…"
                buttons.append([InlineKeyboardButton(text=label, callback_data=f"fav:{store['store_id']}")])
            buttons.append([InlineKeyboardButton(text="✍️ Указать адрес вручную", callback_data="fav:manual")])
            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
            await message.answer(
                "📍 Вот ближайшие магазины. Выбери избранный кнопкой — или напиши адрес вручную:",
                reply_markup=keyboard,
            )

        @self.dp.message(F.text)
        async def on_message(message: Message) -> None:
            chat_id = message.chat.id
            user_text = (message.text or "").strip()
            if user_text.startswith("/"):
                return  # команды обрабатываются отдельными хендлерами
            job = _PendingJob(chat_id, self._handle_user_message(message))
            ahead = len(self._queued.get(chat_id, [])) + (1 if chat_id in self._run_tasks else 0)
            self._queued[chat_id].append(job)
            await self._queue.put(job)
            logger.info(
                "[chat=%d] сообщение в очереди (позиция %d): %s",
                chat_id, ahead + 1, user_text[:120],
            )
            if ahead:
                try:
                    await message.answer(f"⏳ Запрос в очереди — перед ним ещё {ahead}. Выполняю по порядку.")
                except Exception:  # noqa: BLE001
                    pass

    async def _queue_worker(self) -> None:
        """Единственный worker: берёт запросы строго по одному."""
        while True:
            job = await self._queue.get()
            try:
                jobs = self._queued.get(job.chat_id)
                if jobs and job in jobs:
                    jobs.remove(job)
                if not self._queued.get(job.chat_id):
                    self._queued.pop(job.chat_id, None)
                if job.cancelled:
                    job.coro.close()
                    continue
                await self._await_warmup()
                task = asyncio.create_task(job.coro)
                self._run_tasks[job.chat_id] = task
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("Ошибка обработки сообщения из очереди")
            finally:
                # Очередь последовательная: запись этого чата больше не нужна.
                self._run_tasks.pop(job.chat_id, None)
                self._queue.task_done()

    async def _await_warmup(self) -> None:
        """Не даём пользовательскому запросу стартовать посреди прогрева
        5ka-сессии — иначе оба конкурируют за одну инициализацию."""
        warm = self._warmup_task
        if warm is None or warm.done():
            return
        logger.info("Жду завершения прогрева 5ka-сессии перед обработкой запроса…")
        try:
            await asyncio.wait_for(asyncio.shield(warm), timeout=240)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    def start_queue_worker(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._queue_worker(), name="message-queue-worker")

    async def stop_queue_worker(self) -> None:
        """Останавливает worker и закрывает незапущенные корутины очереди."""
        if self._worker_task is not None and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._worker_task = None
        while not self._queue.empty():
            job = self._queue.get_nowait()
            self._queue.task_done()
            if not job.cancelled:
                job.cancelled = True
                job.coro.close()
        self._queued.clear()

    async def _handle_user_message(self, message: Message) -> None:
            chat_id = message.chat.id
            session = self.sessions[chat_id]
            assert self.agent is not None
            user_text = (message.text or "").strip()
            if user_text.startswith("/"):
                return  # команды обрабатываются отдельными хендлерами
            logger.info("[chat=%d] сообщение: %s", chat_id, user_text)
            self.stats.log("message_start", chat=chat_id, text=user_text)
            if self.on_agent_event:
                try:
                    self.on_agent_event({"type": "user_message", "text": user_text})
                except Exception:  # noqa: BLE001
                    logger.exception("on_agent_event failed")
            run_started = time.monotonic()
            typing = asyncio.create_task(self._keep_typing(message))
            self._run_tasks[chat_id] = asyncio.current_task()
            self._stop_flags.discard(chat_id)

            draft_id = random.randint(10000, 999999)
            segments: list[str] = []
            last_send = 0.0
            last_len = 0
            series_name: str | None = None
            series_count = 0
            # phase: "gen" — генерация ИИ, "tool" — выполнение инструмента, None — завершено.
            phase: str | None = "gen"
            phase_start = time.monotonic()
            stopped = False

            def timer_line() -> str:
                if phase is None:
                    return ""
                return f"\n⏱️ {time.monotonic() - phase_start:.1f}с"

            def _trim_segments() -> None:
                total = sum(len(s) for s in segments)
                while len(segments) > 1 and total > MAX_DRAFT_SEGMENTS_CHARS:
                    total -= len(segments.pop(0))

            def draft_text() -> str:
                base = "".join(segments) or ("🤔 Думаю" if phase == "gen" else "")
                return base + timer_line()

            async def push_draft(force: bool = False) -> None:
                nonlocal last_send, last_len
                now = time.monotonic()
                if now < self._draft_cooldown.get(chat_id, 0.0):
                    return
                text = draft_text()
                if not force:
                    if now - last_send < 0.8:
                        return
                    if len(text) - last_len < 20:
                        return
                ok, retry_after = await self._send_draft(chat_id, draft_id, text)
                if ok:
                    last_send = now
                    last_len = len(text)
                elif retry_after:
                    self._draft_cooldown[chat_id] = time.monotonic() + max(retry_after, 5.0)

            async def ticker() -> None:
                try:
                    while True:
                        await asyncio.sleep(2.5)
                        await push_draft(force=True)
                except asyncio.CancelledError:
                    pass

            ticker_task = asyncio.create_task(ticker())
            await push_draft(force=True)

            answer = ""
            products: list[dict] = []
            sent_images: set[str] = set()
            pending_products: list[dict] = []
            try:
                async for event in self.agent.stream_run(
                    session,
                    user_text,
                    should_stop=lambda: chat_id in self._stop_flags,
                ):
                    if self.on_agent_event:
                        try:
                            self.on_agent_event(event)
                        except Exception:  # noqa: BLE001
                            logger.exception("on_agent_event failed")
                    event_type = event["type"]
                    if event_type == "reset":
                        # Провайдер упал посреди стрима и идёт перезапуск:
                        # накопленный черновик невалиден, начинаем заново.
                        segments.clear()
                        series_name = None
                        series_count = 0
                        phase = "gen"
                        phase_start = time.monotonic()
                        await push_draft(force=True)
                    elif event_type == "thinking":
                        if phase is None:
                            phase = "gen"
                            phase_start = time.monotonic()
                    elif event_type == "text":
                        if phase != "gen":
                            phase = "gen"
                            phase_start = time.monotonic()
                        segments.append(event["text"])
                        _trim_segments()
                        series_name = None
                        await push_draft()
                    elif event_type == "tool_start":
                        phase = "tool"
                        phase_start = time.monotonic()
                        name = event["name"]
                        self.stats.log("tool_start", chat=chat_id, name=name)
                        label = TOOL_LABELS.get(name, name)
                        if name == series_name:
                            series_count += 1
                            segments[-1] = f"⚙️ {label}… (x{series_count})"
                        else:
                            segments.append(f"⚙️ {label}…")
                            series_name = name
                            series_count = 1
                        await push_draft(force=True)
                    elif event_type == "tool_done":
                        phase = "gen"
                        phase_start = time.monotonic()
                        name = event["name"]
                        self.stats.log(
                            "tool_done", chat=chat_id, name=name,
                            ok=bool(event.get("ok")), seconds=event.get("seconds"),
                            error=str(event.get("error", ""))[:200],
                        )
                        label = TOOL_LABELS.get(name, name)
                        secs = event.get("seconds")
                        time_txt = f" · {secs:.1f}с" if isinstance(secs, (int, float)) else ""
                        if event.get("ok"):
                            suffix = " ✅" if series_count <= 1 else f" ✅ (x{series_count})"
                        else:
                            suffix = f" ⚠️ ошибка: {event.get('error', '')[:120]}"
                        segments[-1] = f"⚙️ {label}…{suffix}{time_txt}\n\n"
                        await push_draft(force=True)
                    elif event_type == "answer":
                        answer = event["text"]
                        phase = None
                        self.stats.log("answer", chat=chat_id, chars=len(answer), text=answer)
                    elif event_type == "cancelled":
                        stopped = True
                        phase = None
                        self.stats.log("cancelled", chat=chat_id)
                    elif event_type == "products":
                        products = event["products"]
                        fresh = [p for p in products if p.get("image") and p["image"] not in sent_images]
                        sent_images.update(p["image"] for p in fresh)
                        pending_products.extend(fresh)
            except asyncio.CancelledError:
                stopped = True
                phase = None
                if not answer:
                    answer = "⏹ Остановлено."
            except LLMError as exc:
                logger.exception("agent run failed")
                msg = str(exc)
                self.stats.log("error", chat=chat_id, kind="llm", error=msg[:300])
                if "402" in msg or "недостаточно средств" in msg or "insufficient_balance" in msg:
                    answer = "💰 У выбранного провайдера закончился баланс. Пополни счёт или смени модель через /model."
                elif "Invalid 'tools" in msg or "does not match pattern" in msg:
                    answer = "⚠️ Эта модель не поддерживает русские имена инструментов. Смени модель через /model."
                else:
                    answer = f"Произошла ошибка: {msg}"
            except Exception as exc:  # noqa: BLE001
                logger.exception("agent run failed")
                self.stats.log("error", chat=chat_id, kind="agent", error=str(exc)[:300])
                answer = f"Произошла ошибка: {exc}"
            finally:
                typing.cancel()
                ticker_task.cancel()
                self._run_tasks.pop(chat_id, None)
                self._stop_flags.discard(chat_id)

            if stopped and not answer:
                answer = "⏹ Остановлено."
            await self._finalize_draft(chat_id, draft_id, answer)
            if pending_products and not stopped:
                try:
                    await self._send_products(chat_id, pending_products)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[chat=%d] отправка фото: %s", chat_id, exc)
            self.stats.log(
                "message_end", chat=chat_id,
                seconds=round(time.monotonic() - run_started, 1),
                answer_chars=len(answer), photos=len(pending_products),
                stopped=stopped,
            )
            self._save_sessions()

    async def _swap_llm(self) -> None:
        """Пересоздаёт LLMClient из текущего выбора (llm_selection.json).
        Старый клиент закрываем с задержкой, чтобы не оборвать in-flight запросы."""
        if self.agent is None:
            return
        old = self.agent.llm
        new = LLMClient(providers=self.config.llm_providers())
        new.stats_hook = self.stats.hook()
        self.agent.llm = new
        self.llm = new
        if old is not None:

            async def _close_later(client: LLMClient) -> None:
                await asyncio.sleep(30)
                await client.close()

            asyncio.create_task(_close_later(old))

    def _model_providers_keyboard(self) -> InlineKeyboardMarkup:
        buttons = []
        for provider in PROVIDERS:
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=provider["name"],
                        callback_data=f"model:list:{provider['id']}:0",
                    )
                ]
            )
        buttons.append([InlineKeyboardButton(text="↩️ Сброс на .env", callback_data="model:reset")])
        buttons.append([InlineKeyboardButton(text="✖️ Закрыть", callback_data="model:close")])
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    def _model_models_keyboard(self, provider_id: str, page: int = 0) -> tuple[InlineKeyboardMarkup, int]:
        provider = by_id(provider_id)
        if provider is None:
            raise ValueError(f"Неизвестный провайдер: {provider_id}")
        models = provider["models"]
        per_page = 10
        pages = max(1, math.ceil(len(models) / per_page))
        page = min(max(page, 0), pages - 1)
        chunk = models[page * per_page : (page + 1) * per_page]
        buttons = []
        for offset, model in enumerate(chunk):
            real_index = page * per_page + offset
            text = ("⚠️ " if model.get("note") else "") + model["label"]
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=text,
                        callback_data=f"model:pick:{provider_id}:{real_index}",
                    )
                ]
            )
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"model:list:{provider_id}:{page - 1}"))
        nav.append(InlineKeyboardButton(text="🔙 Провайдеры", callback_data="model:prov"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(text="➡️", callback_data=f"model:list:{provider_id}:{page + 1}"))
        buttons.append(nav)
        buttons.append([InlineKeyboardButton(text="✖️ Закрыть", callback_data="model:close")])
        return InlineKeyboardMarkup(inline_keyboard=buttons), pages

    async def _get_http(self) -> aiohttp.ClientSession:
        """Общая aiohttp-сессия для Telegram API (не плодим соединения на каждый запрос)."""
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession()
        return self._http

    async def _send_draft(self, chat_id: int, draft_id: int, text: str) -> tuple[bool, float]:
        """Отправляет обновление streaming-черновика через sendMessageDraft.

        Возвращает (успех, retry_after). При 429 retry_after > 0 — вызывающий
        код берёт паузу (cooldown) и перестаёт спамить Telegram.
        """
        if not self._drafts_supported:
            return False, 0.0  # официальный api.telegram.org не умеет sendMessageDraft
        url = f"{self.config.telegram_api_base_url}/bot{self.config.telegram_token}/sendMessageDraft"
        payload = {"chat_id": chat_id, "draft_id": draft_id, "text": text[:4096]}
        try:
            session = await self._get_http()
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    return True, 0.0
                body = await resp.text()
                if resp.status == 429:
                    retry_after = 5.0
                    try:
                        retry_after = float(json.loads(body).get("parameters", {}).get("retry_after") or retry_after)
                    except Exception:  # noqa: BLE001
                        pass
                    logger.warning("sendMessageDraft 429: retry_after=%s (беру паузу)", retry_after)
                    return False, retry_after
                logger.warning("sendMessageDraft %s: %s", resp.status, body[:200])
                return False, 0.0
        except Exception as exc:  # noqa: BLE001
            logger.warning("sendMessageDraft failed: %s", exc)
            return False, 0.0

    async def _finalize_draft(self, chat_id: int, draft_id: int, final_text: str) -> None:
        """Завершает streaming: отправляет финальное сообщение (фото уходят по ходу).
        Полный ответ уже показан в консоли блоком «🤖 Ответ» — в лог не дублируем."""
        rendered = _md_to_html(final_text or "…")
        logger.debug("[chat=%d] финальный ответ отправлен (%d симв.)", chat_id, len(rendered))
        for part in _chunk_text(rendered):
            await self._send_message(chat_id, part)

    async def _send_message(self, chat_id: int, text: str) -> None:
        url = f"{self.config.telegram_api_base_url}/bot{self.config.telegram_token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        try:
            session = await self._get_http()
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    logger.warning("sendMessage %s: %s", resp.status, (await resp.text())[:200])
        except Exception as exc:  # noqa: BLE001
            logger.warning("sendMessage failed: %s", exc)

    async def _download_image(self, url: str) -> bytes | None:
        """Скачивает изображение товара (с реферером) — Telegram не может сам curl'ить эти URL."""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": "https://5ka.ru/",
            "Accept": "image/*",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status != 200:
                        logger.warning("[фото] %s -> HTTP %s", url[:80], resp.status)
                        return None
                    data = await resp.read()
                    if not data:
                        return None
                    logger.info("[фото] скачано %d б: %s", len(data), url[:80])
                    return data
        except Exception as exc:  # noqa: BLE001
            logger.warning("[фото] ошибка скачивания %s: %s", url[:80], exc)
            return None

    async def _send_products(self, chat_id: int, products: list[dict]) -> None:
        """Отправляет альбомы фотографий товаров. Каждое фото — свой товар,
        подпись у каждого (название + цена + ссылка). Альбом Telegram вмещает
        максимум 10 фото, поэтому при большем списке шлём несколько альбомов.
        """
        if not products:
            return
        for start in range(0, len(products), 10):
            chunk = products[start : start + 10]
            best = chunk
            while best:
                media = await self._build_media(best)
                if not media:
                    best = best[:-1]
                    continue
                try:
                    await self.bot.send_media_group(chat_id=chat_id, media=media)
                    logger.info("[chat=%d] отправлен альбом: %d фото", chat_id, len(media))
                    break
                except Exception as exc:  # noqa: BLE001
                    msg = str(exc)
                    logger.warning("[chat=%d] альбом (%d фото): %s", chat_id, len(media), msg)
                    if "WEBPAGE_CURL_FAILED" in msg or "MEDIA_EMPTY" in msg or "not found" in msg.lower() or "wrong file" in msg.lower():
                        best = best[:-1]
                        continue
                    break
            else:
                for product in chunk:
                    data = await self._download_image(product.get("image", ""))
                    if not data:
                        continue
                    try:
                        await self.bot.send_photo(
                            chat_id=chat_id,
                            photo=BufferedInputFile(data, filename=f"{product.get('plu', 'photo')}.jpg"),
                            caption=self._product_caption(product),
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("[chat=%d] фото поштучно: %s", chat_id, exc)

    async def _build_media(self, products: list[dict]) -> list[InputMediaPhoto]:
        """Скачивает картинки и строит список InputMediaPhoto: первый элемент — весь список товаров."""
        media: list[InputMediaPhoto] = []
        for index, product in enumerate(products):
            data = await self._download_image(product.get("image", ""))
            if not data:
                continue
            item = InputMediaPhoto(
                media=BufferedInputFile(data, filename=f"{product.get('plu', f'photo{index}')}.jpg"),
                caption=self._product_caption(product),
            )
            if len(media) == 0:
                item.caption = self._products_caption(products)
            media.append(item)
        return media

    def _products_caption(self, products: list[dict]) -> str:
        """Сводная подпись первого фото альбома: влезаем в лимит Telegram,
        не рвя HTML-теги (иначе весь альбом отклоняется)."""
        lines: list[str] = []
        total = 0
        for product in products:
            line = self._product_line(product)
            if total + len(line) + 2 > 900:
                break
            lines.append(line)
            total += len(line) + 2
        return "\n\n".join(lines)

    @staticmethod
    def _product_line(product: dict) -> str:
        name = html.escape(product.get("name", ""))
        price = product.get("price")
        price_txt = f"{price:.2f} ₽" if price is not None else "—"
        old = product.get("old_price")
        if old is not None and old != price:
            price_txt += f" <s>{old:.2f} ₽</s>"
        url = product.get("url")
        link = f"\n🔗 <a href=\"{html.escape(url)}\">Смотреть товар</a>" if url else ""
        return f"<b>{name}</b>\n💰 {price_txt}{link}"

    def _product_caption(self, product: dict) -> str:
        name = html.escape(product.get("name", ""))
        price = product.get("price")
        if price is not None:
            name += f"\n💰 {price:.2f} ₽"
            old_price = product.get("old_price")
            if old_price is not None and old_price != price:
                name += f" <s>{old_price:.2f} ₽</s>"
        promo = product.get("promo_label")
        if promo:
            name += f"\n🔥 {html.escape(promo)}"
        url = product.get("url")
        if url:
            name += f"\n🔗 <a href=\"{html.escape(url)}\">Смотреть товар</a>"
        return _safe_html_cut(name, 1000)

    async def _keep_typing(self, message: Message) -> None:
        try:
            while True:
                await self.bot.send_chat_action(message.chat.id, "typing")
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass

    @asynccontextmanager
    async def start(self):
        mcp = MCPServer()
        await mcp.start()
        llm = LLMClient(providers=self.config.llm_providers())
        llm.stats_hook = self.stats.hook()
        self.agent = PyaterochkaAgent(llm, mcp)
        self.llm = llm
        logger.info(
            "Бот готов: model=%s api=%s (фолбэк: %s)",
            self.config.llm_primary_model,
            self.config.llm_primary_url,
            " → ".join(f"{p.model}@{p.api_url.split('//')[-1].split('/')[0]}" for p in self.config.llm_providers()[1:])
            or "выкл.",
        )
        self.start_queue_worker()
        self._warmup_task = asyncio.create_task(self._warmup_mcp(), name="mcp-warmup")
        try:
            yield
        finally:
            if self._warmup_task is not None and not self._warmup_task.done():
                self._warmup_task.cancel()
            await self.stop_queue_worker()
            await self.llm.close()
            await mcp.close()
            if self._http is not None and not self._http.closed:
                await self._http.close()

    async def _warmup_mcp(self) -> None:
        """Прогревает 5ka-сессию сразу при старте, чтобы первый запрос
        пользователя не ждал минуту. Отсутствие интернета не роняет бот:
        пара попыток с паузой, дальше сессия прогреется лениво."""
        assert self.agent is not None
        for attempt in range(1, 4):
            try:
                await self.agent.mcp.call_tool("refresh_session", {})
                logger.info("🔥 5ka-сессия прогрета заранее — первый поиск будет быстрым")
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Прогрев 5ka-сессии (попытка %d/3): %s", attempt, str(exc)[:160],
                )
                if attempt < 3:
                    await asyncio.sleep(15)
        logger.warning("Прогрев не удался — сессия прогреется при первом запросе (медленнее)")


async def main() -> None:
    config = Config.from_env()
    config.validate()
    app = TelegramBot(config)
    async with app.start():
        logger.info("Старт polling через %s", config.telegram_api_base_url)
        await app.dp.start_polling(app.bot)


if __name__ == "__main__":
    asyncio.run(main())
