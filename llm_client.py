"""Асинхронный OpenAI-совместимый клиент для LLM с фолбэком между провайдерами.

Ключевые свойства:
- Раздельные таймауты: соединение / простой между чанками стрима / весь запрос.
  Стрим может идти долго — это нормально; зависшим считается поток, который
  молчит дольше idle_read_timeout_s.
- Классификация ошибок: фатальные для провайдера (401/402/403/400/404/422)
  сразу переключают на следующий, ретраятся только сетевые сбои/429/5xx.
- Событие {"type": "reset"} при перезапуске после частично отданного стрима —
  потребители (агент, бот) обязаны сбросить накопленный текст.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


class LLMProviderFatal(LLMError):
    """Ошибка, при которой этого провайдера не имеет смысла ретраить."""


# HTTP-статусы, после которых не ретраим текущего провайдера,
# а сразу переходим к следующему в цепочке.
_FATAL_STATUSES = {400, 401, 402, 403, 404, 422}

# --- DSML: внутренняя разметка tool-calls DeepSeek, протекающая в content ---
# Выглядит как: <｜｜DSML｜｜invoke name="present_products">…</｜｜DSML｜｜invoke>
# Закрывающий тег может быть в видах </｜｜DSML｜｜invoke> и <｜｜DSML｜｜/invoke>,
# поэтому матчим «любой тег, содержащий DSML и имя секции».

_DSML_INVOKE_RE = re.compile(
    r"<[^>]*DSML[^>]*invoke\s+name=\"([^\"]*)\"[^>]*>(.*?)<[^>]*DSML[^>]*invoke[^>]*>",
    re.S,
)
_DSML_PARAM_RE = re.compile(
    r"<[^>]*DSML[^>]*parameter[^>]*?name=\"([^\"]*)\"(?:\s+string=\"(true|false)\")?[^>]*>(.*?)<[^>]*DSML[^>]*parameter[^>]*>",
    re.S,
)
_DSML_TAG_RE = re.compile(r"<[^>]*DSML[^>]*>")
# Подозрительное в стриме: '<' рядом с баром или одиночный полный бар.
_DSML_SUSPECT_RE = re.compile(r"<[｜|]|[｜|]")


def _extract_dsml_tool_calls(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Вычленяет DSML-блоки из текста ответа.

    Возвращает (очищенный текст, список tool_calls в OpenAI-формате).
    """
    calls: list[dict[str, Any]] = []

    def _invoke_to_call(match: re.Match[str]) -> str:
        name = (match.group(1) or "").strip()
        body = match.group(2) or ""
        arguments: dict[str, Any] = {}
        for pm in _DSML_PARAM_RE.finditer(body):
            pname = (pm.group(1) or "").strip()
            is_string = (pm.group(2) or "true") != "false"
            raw = pm.group(3) or ""
            if is_string:
                arguments[pname] = raw
            else:
                try:
                    arguments[pname] = json.loads(raw)
                except Exception:
                    arguments[pname] = raw
        calls.append(
            {
                "id": f"dsml_{len(calls) + 1}_{random.randint(1000, 9999)}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
            }
        )
        return ""

    clean = _DSML_INVOKE_RE.sub(_invoke_to_call, text)
    clean = _DSML_TAG_RE.sub("", clean)
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    return clean, calls


@dataclass(frozen=True)
class LLMProvider:
    api_url: str
    api_key: str
    model: str


class LLMClient:
    def __init__(
        self,
        providers: list[LLMProvider],
        *,
        connect_timeout_s: float = 20.0,
        idle_read_timeout_s: float = 90.0,
        request_timeout_s: float = 240.0,
        stream_total_timeout_s: float = 900.0,
        max_retries: int = 2,
    ) -> None:
        if not providers:
            raise ValueError("Нужен хотя бы один LLM-провайдер")
        self.providers = providers
        self.connect_timeout_s = connect_timeout_s
        self.idle_read_timeout_s = idle_read_timeout_s
        self.request_timeout_s = request_timeout_s
        self.stream_total_timeout_s = stream_total_timeout_s
        self.max_retries = max_retries
        # total=None: долгий стрим — это норма. Лимитируем отдельные фазы.
        self._timeout = aiohttp.ClientTimeout(
            total=None,
            connect=connect_timeout_s,
            sock_connect=connect_timeout_s,
            sock_read=idle_read_timeout_s,
        )
        self._session: aiohttp.ClientSession | None = None
        # Опциональный колбэк для статистики: self.stats_hook(**поля)
        self.stats_hook: Any = None

    def _emit(self, **fields: Any) -> None:
        if self.stats_hook is None:
            return
        try:
            self.stats_hook(**fields)
        except Exception:  # noqa: BLE001
            pass

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    @staticmethod
    def _headers(provider: LLMProvider) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {provider.api_key}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _raise_for_status(provider: LLMProvider, status: int, body: str) -> None:
        detail = f"{provider.model} HTTP {status}: {body[:500]}"
        if status in _FATAL_STATUSES:
            raise LLMProviderFatal(detail)
        raise LLMError(detail)

    async def _call_provider(
        self,
        provider: LLMProvider,
        payload: dict[str, Any],
        session: aiohttp.ClientSession,
    ) -> dict[str, Any]:
        url = f"{provider.api_url}/chat/completions"
        async with session.post(url, headers=self._headers(provider), json=payload) as resp:
            body = await resp.text()
            if resp.status != 200:
                self._raise_for_status(provider, resp.status, body)
            try:
                return json.loads(body)
            except Exception as exc:
                raise LLMError(f"{provider.model}: некорректный JSON ответа") from exc

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        max_tokens: int = 2048,
        temperature: float = 0.3,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice

        session = await self._get_session()
        errors: list[str] = []

        for provider_index, provider in enumerate(self.providers):
            provider_payload = {**payload, "model": provider.model}
            for attempt in range(1, self.max_retries + 1):
                try:
                    data = await asyncio.wait_for(
                        self._call_provider(provider, provider_payload, session),
                        timeout=self.request_timeout_s,
                    )
                    self._emit(event="llm_success", model=provider.model, attempt=attempt, mode="nonstream")
                    return data
                except LLMProviderFatal as exc:
                    errors.append(str(exc))
                    self._emit(event="llm_fatal", model=provider.model, attempt=attempt, error=str(exc)[:300])
                    logger.warning(
                        "LLM %s фатальная ошибка (без ретраев), провайдер %d/%d: %s",
                        provider.model, provider_index + 1, len(self.providers), exc,
                    )
                    break
                except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError, LLMError) as exc:
                    errors.append(f"{provider.model}: {exc}")
                    self._emit(event="llm_attempt", model=provider.model, attempt=attempt, mode="nonstream", error=str(exc)[:300])
                    logger.warning(
                        "LLM %s попытка %d/%d: %s (провайдер %d/%d)",
                        provider.model, attempt, self.max_retries, exc or type(exc).__name__,
                        provider_index + 1, len(self.providers),
                    )
                    if attempt < self.max_retries:
                        await _sleep(_backoff(attempt))
            if provider_index + 1 < len(self.providers):
                logger.info("Переключаюсь на резервный LLM-провайдер: %s", self.providers[provider_index + 1].model)
                self._emit(event="llm_fallback", from_model=provider.model, to_model=self.providers[provider_index + 1].model)

        raise LLMError(f"LLM недоступен после {len(self.providers)} провайдеров: {' | '.join(errors)}")

    async def _stream_provider(
        self,
        provider: LLMProvider,
        payload: dict[str, Any],
        session: aiohttp.ClientSession,
    ):
        """Итерирует SSE-чанки стримингового ответа одного провайдера."""
        url = f"{provider.api_url}/chat/completions"
        got_any_data = False
        done_seen = False
        async with session.post(url, headers=self._headers(provider), json=payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                self._raise_for_status(provider, resp.status, body)
            async for line in resp.content:
                if not line:
                    continue
                text = line.decode("utf-8", errors="replace").strip()
                if not text.startswith("data:"):
                    continue
                data = text[len("data:"):].strip()
                if data == "[DONE]":
                    done_seen = True
                    return
                try:
                    chunk = json.loads(data)
                except Exception:
                    continue
                got_any_data = True
                yield chunk
        # Соединение закрылось до [DONE]: без единого чанка это сбой (нужен
        # ретрай/фолбэк); с чанками помечаем поток как обрезанный.
        if not got_any_data:
            raise LLMError(f"{provider.model}: пустой ответ потока")
        if not done_seen:
            yield {"__stream_truncated__": True}

    async def _consume_stream(self, provider: LLMProvider, payload: dict[str, Any], session: aiohttp.ClientSession):
        """Аккумулирует SSE-чанки в события и финальное сообщение.

        События:
        - {"type": "reasoning", "text": ...}  — рассуждения модели (не показывать)
        - {"type": "content",  "text": ...}   — видимый текст
        - {"type": "tool_call","index": N, "name": ...} — появился вызов инструмента
        - {"type": "message",  "message": {...}} — итоговое assistant-сообщение
        """
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_acc: dict[int, dict[str, str]] = {}
        dsml_parts: list[str] = []
        dsml_mode = False
        pending = ""  # удержанный хвост: возможно, начало DSML-разметки

        def _emit_text(fragment: str) -> Any:
            content_parts.append(fragment)
            return {"type": "content", "text": fragment}

        def _split_holdback(buffer: str) -> tuple[str, str, bool]:
            """Отделяет безопасный текст от возможного начала DSML.

            Возвращает (можно_показать, удержать, это_dsml).
            """
            match = _DSML_SUSPECT_RE.search(buffer)
            if not match:
                return buffer, "", False
            idx = match.start()
            tail = buffer[idx:]
            if "DSML" in tail[:80]:
                return buffer[:idx], tail, True
            if len(tail) > 200:
                # Длинный хвост без «DSML» — обычный текст (например, код со '<').
                return buffer, "", False
            return buffer[:idx], tail, False

        async for chunk in self._stream_provider(provider, payload, session):
            if chunk.get("__stream_truncated__"):
                break
            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}

            reasoning = delta.get("reasoning_content")
            if reasoning:
                reasoning_parts.append(reasoning)
                yield {"type": "reasoning", "text": reasoning}

            content = delta.get("content")
            if content:
                if dsml_mode:
                    dsml_parts.append(content)
                else:
                    pending += content
                    emit, keep, is_dsml = _split_holdback(pending)
                    if emit:
                        yield _emit_text(emit)
                    if is_dsml:
                        dsml_mode = True
                        dsml_parts.append(keep)
                        pending = ""
                    else:
                        pending = keep

            for tc in delta.get("tool_calls") or []:
                index = tc.get("index", 0)
                acc = tool_acc.setdefault(index, {"id": "", "name": "", "arguments": ""})
                if tc.get("id"):
                    acc["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    acc["name"] += fn["name"]
                if fn.get("arguments"):
                    acc["arguments"] += fn["arguments"]

        if not dsml_mode and pending:
            yield _emit_text(pending)
            pending = ""

        if tool_acc and not all(a["name"] and a["id"] and a["arguments"] for a in tool_acc.values()):
            # Оборванный вызов инструмента (delta-формат): невалидный JSON
            # сломает агента — лучше упасть и перезапустить стрим.
            raise LLMError(f"{provider.model}: поток оборван посреди вызова инструмента")

        clean_text = "".join(content_parts)
        dsml_calls: list[dict[str, Any]] = []
        if dsml_parts:
            clean_text, dsml_calls = _extract_dsml_tool_calls(clean_text)
            joined_dsml = "".join(dsml_parts)
            _, extra_calls = _extract_dsml_tool_calls(joined_dsml)
            dsml_calls = extra_calls or dsml_calls

        message: dict[str, Any] = {"role": "assistant", "content": clean_text or None}
        if reasoning_parts:
            message["reasoning_content"] = "".join(reasoning_parts)
        tool_calls: list[dict[str, Any]] = []
        for index in sorted(tool_acc):
            acc = tool_acc[index]
            if acc["name"] and acc["id"]:
                tool_calls.append(
                    {
                        "id": acc["id"],
                        "type": "function",
                        "function": {"name": acc["name"], "arguments": acc["arguments"] or "{}"},
                    }
                )
        if dsml_calls:
            # id у DSML-вызовов уже заполнен — не требуем acc-проверок.
            tool_calls.extend(dsml_calls)
        if tool_calls:
            message["tool_calls"] = tool_calls
        yield {"type": "message", "message": message}

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        max_tokens: int = 2048,
        temperature: float = 0.3,
    ):
        """Асинхронный генератор стриминговых событий с фолбэком между провайдерами.

        Если стрим оборвался ПОСЛЕ того, как часть текста уже отдана потребителю,
        перед повторной попыткой выдаётся {"type": "reset"} — потребитель обязан
        сбросить накопленное, чтобы не задвоить ответ.
        """
        payload: dict[str, Any] = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice

        session = await self._get_session()
        errors: list[str] = []

        for provider_index, provider in enumerate(self.providers):
            provider_payload = {**payload, "model": provider.model}
            for attempt in range(1, self.max_retries + 1):
                emitted = False
                try:
                    async with asyncio.timeout(self.stream_total_timeout_s):
                        async for event in self._consume_stream(provider, provider_payload, session):
                            if event["type"] != "message":
                                emitted = True
                            yield event
                    self._emit(event="llm_success", model=provider.model, attempt=attempt, mode="stream")
                    return
                except LLMProviderFatal as exc:
                    errors.append(str(exc))
                    self._emit(event="llm_fatal", model=provider.model, attempt=attempt, error=str(exc)[:300])
                    logger.warning(
                        "LLM %s фатальная ошибка (без ретраев): %s",
                        provider.model, exc,
                    )
                    break
                except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError, LLMError) as exc:
                    text = f"{provider.model}: {exc or type(exc).__name__}"
                    errors.append(text)
                    self._emit(event="llm_attempt", model=provider.model, attempt=attempt, mode="stream", error=str(exc)[:300])
                    kind = "завис/таймаут" if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) else "сбой"
                    logger.warning(
                        "LLM %s попытка %d/%d (stream, %s): %s",
                        provider.model, attempt, self.max_retries, kind, exc or type(exc).__name__,
                    )
                    if attempt >= self.max_retries:
                        break
                    if emitted:
                        yield {"type": "reset"}
                    await _sleep(_backoff(attempt))
            if provider_index + 1 < len(self.providers):
                logger.info("Переключаюсь на резервный LLM-провайдер: %s", self.providers[provider_index + 1].model)
                self._emit(event="llm_fallback", from_model=provider.model, to_model=self.providers[provider_index + 1].model)

        raise LLMError(f"LLM недоступен: {' | '.join(errors)}")

    @staticmethod
    def parse_message(data: dict[str, Any]) -> tuple[str | None, list[dict[str, Any]]]:
        """Возвращает (content, tool_calls). tool_calls — нормализованные вызовы."""
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"Неожиданный формат ответа LLM: {str(data)[:300]}") from exc

        content = message.get("content")
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
            content = "".join(parts) or None

        dsml_calls: list[dict[str, Any]] = []
        if isinstance(content, str) and "DSML" in content:
            content, dsml_calls = _extract_dsml_tool_calls(content)

        calls: list[dict[str, Any]] = []
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            arguments_raw = function.get("arguments") or "{}"
            try:
                arguments = json.loads(arguments_raw) if isinstance(arguments_raw, str) else arguments_raw
            except Exception:
                arguments = {"_raw": arguments_raw}
            calls.append(
                {
                    "id": call.get("id") or "",
                    "name": function.get("name") or "",
                    "arguments": arguments if isinstance(arguments, dict) else {"_raw": arguments_raw},
                    "raw": call,
                }
            )
        if dsml_calls:
            for dc in dsml_calls:
                fn = dc.get("function") or {}
                try:
                    arguments = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    arguments = {"_raw": fn.get("arguments")}
                calls.append({"id": dc["id"], "name": fn.get("name", ""), "arguments": arguments, "raw": dc})
        return content, calls


def _backoff(attempt: int) -> float:
    """Пауза между ретраями: растёт с попыткой + джиттер, максимум 8с."""
    return min(1.2 * attempt + random.random() * 0.8, 8.0)


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)
