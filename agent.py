"""Агентский цикл: LLM + инструменты Пятёрочки + «избранный магазин» чата."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Callable

from llm_client import LLMClient
from mcp.mcp_server import MCPServer, TOOLS

logger = logging.getLogger(__name__)

MAX_STEPS = 60
MAX_HISTORY_MESSAGES = 400
MAX_TOOL_RESULT_CHARS = 80000
MAX_RESPONSE_TOKENS = 16000
MAX_CONTEXT_TOKENS = 700000
COMPACT_THRESHOLD_TOKENS = 600000
TOOL_RESULT_KEEP_CHARS = 12000
MAX_COLLECTED_PRODUCTS = 300

# Максимум вызовов инструментов за один запрос — защита от зацикливания модели.
MAX_TOOL_CALLS = 30

# После стольких подряд упавших инструментов считаем сервис недоступным
# и заставляем модель завершить ответ, а не долбить дальше.
MAX_CONSECUTIVE_TOOL_FAILURES = 4

TOOL_FAILURE_FINAL_MESSAGE = (
    "Стоп. Инструменты Пятёрочки сейчас недоступны (сервис отвечает ошибками). "
    "Немедленно сформируй итоговый ответ из того, что уже найдено, и обязательно "
    "сообщи пользователю, что сервис магазина временно недоступен — новые поиски не выполнять."
)

FORCE_FINAL_MESSAGE = (
    "Стоп. Поиски завершены — их уже достаточно. Сейчас же сформируй ИТОГОВЫЙ ответ пользователю: "
    "перечисли найденные товары с ценами и акциями. Больше НЕ вызывай инструменты и не начинай новых поисков."
)

# Реальные store_id выглядят как S105, 3AHS, 1234. Плейсхолдеры вроде
# found_store_id или store_id отсюда — невалидны, подменяем избранным магазином.
STORE_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{1,10}$")

SYSTEM_PROMPT_TEMPLATE = """Ты — агент-помощник по каталогу сети магазинов «Пятёрочка». Твоя цель — находить магазины и товары по запросу пользователя, используя доступные инструменты.

Доступные инструменты (имена и аргументы всегда на английском):
- find_store(address): найти физический магазин по полному адресу.
- find_nearest_stores(latitude, longitude, limit): найти ближайшие магазины по координатам.
- get_store_info(store_id) / get_store_hours(store_id): карточка и часы работы магазина.
- search_products(store_id, query, ...): искать товары в магазине с фильтрами.
- list_category_products(store_id, category_id, ...): товары категории магазина.
- find_products(query, store_id или address, ...): универсальный поиск товаров.
- get_product_promotion(store_id, plu): условия акции на товар.
- get_product_info(store_id, plu): состав, калории, БЖУ и ссылка товара.
- list_store_categories(store_id): категории магазина.
- refresh_session(): обновить web-сессию 5ka.ru при ошибках авторизации/403.
- remember_preference(preference): запомнить предпочтение пользователя надолго.
- present_products(plu): показать фотографии финально выбранных товаров. Вызывай ОДИН раз в самом конце.

Избранный магазин чата: {favorite_store}
Известные предпочтения пользователя: {preferences}

ВАЖНЫЕ ПРАВИЛА:
1. Работай строго ПОСЛЕДОВАТЕЛЬНО: сначала найди магазин (если нет готового store_id), дождись результата, извлеки store_id, и только потом ищи товары. НЕ вызывай поиск товаров одновременно с поиском магазина и НЕ выдумывай store_id.
2. Если у чата есть избранный магазин, а пользователь не называет новый адрес/гео — ищи товары сразу в нём.
3. Если пользователь называет новый адрес — сначала find_store (этот магазин станет новым избранным), затем поиск в нём.
4. ИЩИ ПОКА НЕ НАЙДЁШЬ ВСЁ. Для списка продуктов перебирай каждую позицию отдельным search_products. Если поиск пуст — пробуй синонимы, фильтры и категории.
5. НЕ ЗАЦИКЛИВАЙСЯ: не повторяй одинаковые поиски и не начинай новые поиски, когда всё необходимое уже найдено. Как только собрано достаточно товаров (примерно 10–15) или поиски перестают давать новые результаты — СРАЗУ переходи к итоговому ответу одним сообщением.

ПРАВИЛА ОТВЕТА ПОЛЬЗОВАТЕЛЮ:
1. НИКОГДА не показывай пользователю id_магазина, sap_code или PLU — это внутренние идентификаторы бэкенда. Запрещено писать их в ответе (например «S105», «251C…»). Всегда называй магазин по АДРЕСУ, а товары по названию.
2. Оформляй ответ в Markdown: заголовки, жирный текст, списки.
3. Указывай цену. Если цена действует по карте (cpd_price) или по акции «при покупке N штук» (units_to_activate / promo_label) — обязательно скажи об этом пользователю. Если есть старая цена и акция — покажи «было X, стало Y».
4. Отвечай коротко и по делу, по-русски.
5. Если инструмент вернул ошибку — сообщи об этом и предложи, что делать.
6. ЗАПРЕЩЕНО ВЫДУМЫВАТЬ. Цитируй ТОЛЬКО данные из search_products / list_category_products / get_product_info / find_store. При ошибке или пустом результате прямо скажи, что данные получить не удалось.
7. Суммируй цены и считай итог ТОЛЬКО по товарам, найденным инструментами. Если какой-то товар не найден — не включай его в корзину и скажи об этом.
8. ФОТО ТОВАРОВ: перед итоговым ответом вызови present_products и передай PLU выбранных товаров. Вызывай только один раз в конце.
9. Формируй итоговый ответ ОДНИМ сообщением после завершения всех поисков. Не пиши черновые промежуточные ответы между поисками и не начинай новые поиски после того, как начал формировать итог.

{favorite_note}"""

FAVORITE_PRESENT = "Избранный магазин чата: {store_id} ({address})."
FAVORITE_ABSENT = "Избранного магазина пока нет. Найди его через find_store по адресу пользователя."


def _openai_tool(mcp_tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": mcp_tool["name"],
            "description": mcp_tool["description"],
            "parameters": mcp_tool.get("inputSchema", {"type": "object", "properties": {}, "required": []}),
        },
    }


REMEMBER_TOOL = {
    "type": "function",
    "function": {
        "name": "remember_preference",
        "description": "Запомнить предпочтение пользователя для будущих поисков (сохраняется навсегда, пока не сброшено). Примеры: «не пью кофе», «люблю сыры и йогурты», «бюджет до 300 ₽». Вызывай, когда пользователь явно говорит о своих предпочтениях.",
        "parameters": {
            "type": "object",
            "properties": {
                "preference": {"type": "string", "description": "Краткое предпочтение пользователя, 1 предложение."},
            },
            "required": ["preference"],
        },
    },
}


PRESENT_TOOL = {
    "type": "function",
    "function": {
        "name": "present_products",
        "description": "Отправить пользователю фотографии выбранных товаров (альбом с ценами и ссылками). Передай список PLU товаров, которые попали в финальный ответ. Вызывай ОДИН раз в самом конце, только после того как финальный список готов. Не вызывай во время поиска.",
        "parameters": {
            "type": "object",
            "properties": {
                "plu": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Список PLU выбранных товаров (строки).",
                },
            },
            "required": ["plu"],
        },
    },
}


def build_tools() -> list[dict[str, Any]]:
    return [_openai_tool(tool) for tool in TOOLS] + [REMEMBER_TOOL, PRESENT_TOOL]


def _is_valid_store_id(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    return bool(STORE_ID_PATTERN.fullmatch(value)) and "_" not in value


def _truncate(text: str, limit: int = MAX_TOOL_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…(обрезано)"


def _estimate_tokens(text: str) -> int:
    """Грубая оценка токенов: 1 токен ≈ 4 символа (кириллица кодируется плотнее)."""
    return max(1, len(text) // 3)


def _estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    total = 0
    for message in messages:
        content = message.get("content") or ""
        total += _estimate_tokens(str(content))
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            for call in tool_calls:
                fn = call.get("function") or {}
                total += _estimate_tokens(str(fn.get("arguments") or ""))
                total += _estimate_tokens(str(fn.get("name") or ""))
        reasoning = message.get("reasoning") or message.get("reasoning_content") or ""
        if reasoning:
            total += _estimate_tokens(str(reasoning))
    return total


class AgentSession:
    """Состояние одного чата: история диалога, избранный магазин и предпочтения."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.favorite_store: dict[str, Any] | None = None
        self.preferences: list[str] = []

    def set_favorite(self, store: dict[str, Any]) -> None:
        self.favorite_store = store

    def remember(self, preference: str) -> None:
        preference = " ".join(preference.split())
        if not preference:
            return
        if preference not in self.preferences:
            self.preferences.append(preference)
        if len(self.preferences) > 20:
            self.preferences = self.preferences[-20:]

    def system_prompt(self) -> str:
        favorite_note = FAVORITE_ABSENT
        favorite_store = "нет"
        if self.favorite_store:
            favorite_store = f"{self.favorite_store.get('store_id')} ({self.favorite_store.get('address')})"
            favorite_note = FAVORITE_PRESENT.format(
                store_id=self.favorite_store.get("store_id"),
                address=self.favorite_store.get("address"),
            )
        preferences = ", ".join(self.preferences) if self.preferences else "нет"
        return SYSTEM_PROMPT_TEMPLATE.format(
            favorite_store=favorite_store,
            favorite_note=favorite_note,
            preferences=preferences,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "favorite_store": self.favorite_store,
            "preferences": list(self.preferences),
        }

    def compact(self, *, keep_tokens: int = COMPACT_THRESHOLD_TOKENS, keep_chars: int = TOOL_RESULT_KEEP_CHARS) -> int:
        """Сжимает историю, когда она разрослась: старые результаты инструментов
        сильно укорачиваются. Возвращает оценку сэкономленных токенов.

        Логика: идём с начала и ужимаем tool-сообщения до keep_chars символов,
        пока суммарный размер истории не опустится ниже keep_tokens.
        """
        if not self.messages:
            return 0
        total = _estimate_messages_tokens(self.messages)
        if total <= keep_tokens:
            return 0
        saved = 0
        compacted = 0
        for index, message in enumerate(self.messages):
            if message.get("role") != "tool":
                continue
            content = message.get("content") or ""
            if len(content) <= keep_chars:
                continue
            new_content = _truncate(content, keep_chars)
            old_tokens = _estimate_tokens(content)
            new_tokens = _estimate_tokens(new_content)
            saved += old_tokens - new_tokens
            message["content"] = new_content
            compacted += 1
            total = _estimate_messages_tokens(self.messages)
            if total <= keep_tokens:
                break
        if compacted:
            logger.info("Компактинг истории: %d tool-сообщений ужато, сэкономлено ~%d токенов", compacted, saved)
        return saved

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentSession":
        session = cls()
        session.favorite_store = data.get("favorite_store")
        session.preferences = list(data.get("preferences") or [])
        return session


class PyaterochkaAgent:
    def __init__(self, llm: LLMClient, mcp: MCPServer) -> None:
        self.llm = llm
        self.mcp = mcp
        self.tools = build_tools()

    async def _run_tool(self, name: str, arguments: dict[str, Any], session: AgentSession) -> dict[str, Any]:
        """Выполняет инструмент, разрешая id_магазина через избранный магазин."""
        resolved = dict(arguments)
        store_id = resolved.get("store_id")
        if name in {"search_products", "list_category_products", "list_store_categories", "get_store_info", "get_store_hours", "get_product_promotion", "get_product_info"}:
            if session.favorite_store and (not _is_valid_store_id(store_id) or store_id is None):
                fallback = session.favorite_store.get("store_id")
                logger.info("store_id=%r подменён избранным магазином %s", store_id, fallback)
                resolved["store_id"] = fallback
        elif name == "find_products":
            if resolved.get("store_id") is None and session.favorite_store:
                resolved["store_id"] = session.favorite_store.get("store_id")

        if name == "remember_preference":
            # Виртуальный инструмент: сохраняется в сессии, MCP-сервер о нём не знает.
            session.remember(str(resolved.get("preference") or ""))
            return {"saved": True, "preference": str(resolved.get("preference") or "")}

        result = await self.mcp.call_tool(name, resolved)

        if name == "find_store" and isinstance(result, dict) and result.get("store"):
            session.set_favorite(result["store"])
            result = {**result, "saved_as_favorite": True}
        elif name == "find_products" and isinstance(result, dict) and result.get("store"):
            # найти_товары по уже известному id_магазина возвращает пустой Store
            # (без адреса) — не затираем избранный магазин такой карточкой.
            store = result["store"]
            if store.get("address"):
                session.set_favorite(store)
                result = {**result, "saved_as_favorite": True}
        return result

    @staticmethod
    def _collect_products(result: dict[str, Any], collector: dict[str, dict[str, Any]]) -> int:
        """Добавляет найденные товары в collector (ключ — PLU). Возвращает число добавленных."""
        products = result.get("products") if isinstance(result, dict) else None
        if not isinstance(products, list):
            return 0
        added = 0
        for product in products:
            if not isinstance(product, dict):
                continue
            plu = str(product.get("plu") or "")
            if not plu:
                continue
            if plu in collector or len(collector) >= MAX_COLLECTED_PRODUCTS:
                continue
            collector[plu] = {
                "plu": plu,
                "name": product.get("name", ""),
                "price": product.get("price"),
                "old_price": product.get("old_price"),
                "is_promo": product.get("is_promo", False),
                "promo_label": product.get("promo_label", ""),
                "image": product.get("image", ""),
                "url": product.get("url", ""),
            }
            added += 1
        return added

    async def stream_run(
        self,
        session: AgentSession,
        user_text: str,
        *,
        should_stop: Callable[[], bool] | None = None,
    ):
        """Стриминговый агентский цикл. Генерирует события:

        - {"type": "thinking"}                     — модель рассуждает
        - {"type": "text", "text": str}            — порция ответа
        - {"type": "llm_done", "seconds": float}   — генерация ИИ завершена (сколько заняла)
        - {"type": "tool_start", "name", "arguments"} — начался вызов инструмента
        - {"type": "tool_done", "name", "ok", "seconds", "error"?} — инструмент выполнен (сколько занял)
        - {"type": "answer", "text": str}          — финальный ответ
        - {"type": "products", "products": [...]}  — фото выбранных товаров (по показать_товары)
        - {"type": "cancelled"}                    — выполнение остановлено (команда /stop)
        """
        session.messages.append({"role": "user", "content": user_text})
        products_by_plu: dict[str, dict[str, Any]] = {}
        total_tool_calls = 0
        consecutive_failures = 0

        for step in range(MAX_STEPS):
            if should_stop and should_stop():
                yield {"type": "cancelled"}
                return
            if _estimate_messages_tokens(session.messages) > COMPACT_THRESHOLD_TOKENS:
                session.compact()
            service_down = consecutive_failures >= MAX_CONSECUTIVE_TOOL_FAILURES
            force_final = total_tool_calls >= MAX_TOOL_CALLS or service_down
            if force_final:
                session.messages.append({
                    "role": "system",
                    "content": TOOL_FAILURE_FINAL_MESSAGE if service_down else FORCE_FINAL_MESSAGE,
                })
            messages = [{"role": "system", "content": session.system_prompt()}] + session.messages[-MAX_HISTORY_MESSAGES:]
            content_parts: list[str] = []
            assistant_message: dict[str, Any] | None = None
            generation_started = time.monotonic()

            async for event in self.llm.chat_stream(
                messages,
                tools=None if force_final else self.tools,
                max_tokens=MAX_RESPONSE_TOKENS,
            ):
                if should_stop and should_stop():
                    yield {"type": "cancelled"}
                    return
                event_type = event["type"]
                if event_type == "reset":
                    # Стрим перезапускается после сбоя провайдера — часть
                    # текста, которую мы уже отдали, невалидна.
                    logger.warning("Стрим LLM перезапущен (reset), накопленный ответ сброшен")
                    content_parts.clear()
                    assistant_message = None
                    yield {"type": "reset"}
                elif event_type == "reasoning":
                    yield {"type": "thinking"}
                elif event_type == "content":
                    content_parts.append(event["text"])
                    yield {"type": "text", "text": event["text"]}
                elif event_type == "tool_call":
                    yield {"type": "tool_start", "name": event["name"], "arguments": {}}
                elif event_type == "message":
                    assistant_message = event["message"]

            yield {"type": "llm_done", "seconds": round(time.monotonic() - generation_started, 1)}

            if assistant_message is None:
                raise RuntimeError("LLM не вернул сообщение")

            tool_calls = assistant_message.get("tool_calls") or []
            if not tool_calls or force_final:
                final = (
                    "".join(content_parts).strip()
                    or (assistant_message.get("content") or "").strip()
                    or self._fallback_summary(products_by_plu)
                )
                session.messages.append({"role": "assistant", "content": final})
                self._trim(session)
                yield {"type": "answer", "text": final}
                return

            echo: dict[str, Any] = {
                "role": "assistant",
                "content": assistant_message.get("content"),
                "tool_calls": assistant_message.get("tool_calls"),
            }
            # Nemotron возвращает reasoning, DeepSeek — reasoning_content.
            # Их нужно сохранять между шагами, иначе некоторые API отклоняют запрос.
            if assistant_message.get("reasoning"):
                echo["reasoning"] = assistant_message["reasoning"]
            if assistant_message.get("reasoning_content"):
                echo["reasoning_content"] = assistant_message["reasoning_content"]
            session.messages.append(echo)

            for call in tool_calls:
                if should_stop and should_stop():
                    yield {"type": "cancelled"}
                    return
                fn = call.get("function") or {}
                call_id = call.get("id") or ""
                name = fn.get("name") or ""
                arguments_raw = fn.get("arguments") or "{}"
                try:
                    arguments = json.loads(arguments_raw) if isinstance(arguments_raw, str) else arguments_raw
                except Exception:
                    arguments = {"_raw": arguments_raw}
                if not isinstance(arguments, dict):
                    arguments = {"_raw": arguments_raw}

                yield {"type": "tool_start", "name": name, "arguments": arguments}
                logger.info("step %d: tool=%s args=%s", step, name, json.dumps(arguments, ensure_ascii=False)[:400])
                tool_started = time.monotonic()
                try:
                    if name == "present_products":
                        # Виртуальный инструмент: выбираем товары из уже найденных.
                        raw_plu = arguments.get("plu") or []
                        if isinstance(raw_plu, str):
                            raw_plu = [raw_plu]
                        selected = [products_by_plu[p] for p in raw_plu if str(p) in products_by_plu]
                        result = {"ok": True, "shown": len(selected)}
                        if selected:
                            yield {"type": "products", "products": list(selected)}
                    else:
                        result = await self._run_tool(name, arguments, session)
                        self._collect_products(result, products_by_plu)
                        consecutive_failures = 0
                        result_text = json.dumps(result, ensure_ascii=False)
                    yield {
                        "type": "tool_done",
                        "name": name,
                        "ok": True,
                        "seconds": round(time.monotonic() - tool_started, 1),
                    }
                except Exception as exc:  # noqa: BLE001 — ошибки уходят модели
                    logger.exception("tool %s failed", name)
                    consecutive_failures += 1
                    error_text = str(exc) or f"{type(exc).__name__}"
                    result_text = json.dumps(
                        {
                            "error": error_text,
                            "hint": "Инструмент временно недоступен или сервис Пятёрочки ответил ошибкой. "
                            "Можно повторить тот же вызов ещё раз, либо использовать другой инструмент "
                            "(например, find_products вместо search_products). Сообщи пользователю понятным языком.",
                        },
                        ensure_ascii=False,
                    )
                    yield {
                        "type": "tool_done",
                        "name": name,
                        "ok": False,
                        "error": error_text,
                        "seconds": round(time.monotonic() - tool_started, 1),
                    }
                session.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": name,
                        "content": _truncate(result_text),
                    }
                )
                total_tool_calls += 1

        yield {"type": "answer", "text": "Не удалось завершить задачу за допустимое число шагов. Уточни запрос или попробуй ещё раз."}

    @staticmethod
    def _fallback_summary(products_by_plu: dict[str, dict[str, Any]]) -> str:
        """Итог из собранных товаров, если модель не сформировала ответ сама."""
        items = list(products_by_plu.values())
        if not items:
            return "Поиск завершён. Товаров, к сожалению, найти не удалось — попробуй уточнить запрос."
        lines = ["Вот что удалось найти:"]
        for product in items[:15]:
            name = product.get("name", "")
            price = product.get("price")
            price_txt = f" — {price:.2f} ₽" if price is not None else ""
            old = product.get("old_price")
            if old is not None and old != price:
                price_txt += f" (было {old:.2f} ₽)"
            lines.append(f"• {name}{price_txt}")
        if len(items) > 15:
            lines.append(f"…и ещё {len(items) - 15} шт.")
        return "\n".join(lines)

    async def run(self, session: AgentSession, user_text: str) -> tuple[str, list[dict[str, Any]]]:
        """Обёртка над stream_run для синхронного использования (тесты)."""
        answer = "Готово."
        products: list[dict[str, Any]] = []
        async for event in self.stream_run(session, user_text):
            if event["type"] == "answer":
                answer = event["text"]
            elif event["type"] == "products":
                products = event["products"]
        return answer, products

    def _trim(self, session: AgentSession) -> None:
        if len(session.messages) > MAX_HISTORY_MESSAGES:
            session.messages = session.messages[-MAX_HISTORY_MESSAGES:]
