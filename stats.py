"""JSONL-статистика работы бота — для последующего анализа и оптимизации.

Пишет построчные JSON-записи в logs/stats-YYYYMMDD.jsonl:
- message_start / message_end — запросы пользователей и их результаты;
- tool_start / tool_done — вызовы инструментов с длительностью;
- llm_attempt / llm_fallback / llm_success / llm_fatal — работа провайдеров;
- error / reset — сбои.

Формат устойчивый к чтению «с наскока»: одна JSON-строка = одно событие.
Файлы не попадают в git (.gitignore). Ничего не падает, если диск недоступен.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_MAX_TEXT_FIELD = 400


class StatsLogger:
    def __init__(self, directory: str | None = None) -> None:
        base = directory or os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
        self.directory = base
        try:
            os.makedirs(self.directory, exist_ok=True)
        except Exception:  # noqa: BLE001
            pass
        self._lock = threading.Lock()
        self.enabled = os.getenv("STATS_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}

    def log(self, event_type: str, **fields: Any) -> None:
        if not self.enabled:
            return
        record: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "type": event_type,
        }
        for key, value in fields.items():
            if isinstance(value, str) and len(value) > _MAX_TEXT_FIELD:
                value = value[:_MAX_TEXT_FIELD] + "…"
            record[key] = value
        try:
            filename = os.path.join(self.directory, f"stats-{datetime.now():%Y%m%d}.jsonl")
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            with self._lock:
                with open(filename, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except Exception:  # noqa: BLE001 — статистика не должна ронять бот
            logger.debug("stats write failed", exc_info=True)

    def hook(self) -> Any:
        """Колбэк для LLMClient.stats_hook: превращает события в записи."""

        def _on_llm_event(**fields: Any) -> None:
            self.log("llm", **fields)

        return _on_llm_event
