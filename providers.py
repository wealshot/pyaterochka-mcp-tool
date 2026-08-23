"""Каталог LLM-провайдеров и моделей для команды /model.

Все провайдеры используют OpenAI-совместимый API (/chat/completions).
Ключи читаются из переменных окружения (.env) — в коде и в git их нет.

Каждая модель: {"id": str, "label": str, "note": str | None}.
note — предупреждение, которое показывается в /model (например,
reasoning-модели отвечают медленнее).

Список можно расширить под себя: добавьте провайдера в PROVIDERS,
укажите key_env — и он появится в инлайн-меню /model.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

SELECTION_FILE = Path(__file__).resolve().parent / "llm_selection.json"

PROVIDERS: list[dict[str, Any]] = [
    {
        "id": "openai",
        "name": "🟢 OpenAI",
        "api_url": "https://api.openai.com/v1",
        "key_env": "OPENAI_API_KEY",
        "models": [
            {"id": "gpt-4o-mini", "label": "GPT-4o mini"},
            {"id": "gpt-4o", "label": "GPT-4o"},
            {"id": "gpt-4.1-mini", "label": "GPT-4.1 mini"},
            {"id": "gpt-4.1", "label": "GPT-4.1"},
            {"id": "o4-mini", "label": "o4-mini (reasoning)", "note": "reasoning-модель: медленный старт"},
        ],
    },
    {
        "id": "openrouter",
        "name": "🔵 OpenRouter",
        "api_url": "https://openrouter.ai/api/v1",
        "key_env": "OPENROUTER_API_KEY",
        "models": [
            {"id": "openai/gpt-4o-mini", "label": "GPT-4o mini"},
            {"id": "anthropic/claude-3.5-sonnet", "label": "Claude 3.5 Sonnet"},
            {"id": "google/gemini-2.0-flash-001", "label": "Gemini 2.0 Flash"},
            {"id": "deepseek/deepseek-chat", "label": "DeepSeek V3"},
            {"id": "meta-llama/llama-3.3-70b-instruct", "label": "Llama 3.3 70B"},
            {"id": "qwen/qwen-2.5-72b-instruct", "label": "Qwen 2.5 72B"},
            {"id": "mistralai/mistral-small-24b-instruct-2501", "label": "Mistral Small 24B"},
        ],
    },
    {
        "id": "groq",
        "name": "🟠 Groq",
        "api_url": "https://api.groq.com/openai/v1",
        "key_env": "GROQ_API_KEY",
        "models": [
            {"id": "llama-3.3-70b-versatile", "label": "Llama 3.3 70B (быстрая)"},
            {"id": "llama-3.1-8b-instant", "label": "Llama 3.1 8B (мгновенная)"},
            {"id": "qwen-2.5-32b", "label": "Qwen 2.5 32B"},
            {"id": "deepseek-r1-distill-llama-70b", "label": "DeepSeek R1 Distill 70B", "note": "reasoning-модель: медленнее"},
        ],
    },
    {
        "id": "nvidia",
        "name": "🟢 NVIDIA NIM",
        "api_url": "https://integrate.api.nvidia.com/v1",
        "key_env": "NVIDIA_API_KEY",
        "models": [
            {"id": "meta/llama-3.3-70b-instruct", "label": "Llama 3.3 70B"},
            {"id": "deepseek-ai/deepseek-r1", "label": "DeepSeek R1", "note": "reasoning-модель: медленнее"},
            {"id": "qwen/qwen2.5-7b-instruct", "label": "Qwen 2.5 7B"},
            {"id": "nv-mistralai/mistral-nemo-12b-instruct", "label": "Mistral NeMo 12B"},
            {"id": "openai/gpt-oss-120b", "label": "GPT-OSS 120B"},
        ],
    },
    {
        "id": "deepseek",
        "name": "🔴 DeepSeek (официальный)",
        "api_url": "https://api.deepseek.com/v1",
        "key_env": "DEEPSEEK_API_KEY",
        "models": [
            {"id": "deepseek-chat", "label": "DeepSeek Chat"},
            {"id": "deepseek-reasoner", "label": "DeepSeek Reasoner", "note": "reasoning-модель: медленнее"},
        ],
    },
    {
        "id": "together",
        "name": "🟡 Together AI",
        "api_url": "https://api.together.xyz/v1",
        "key_env": "TOGETHER_API_KEY",
        "models": [
            {"id": "meta-llama/Llama-3.3-70B-Instruct-Turbo", "label": "Llama 3.3 70B Turbo"},
            {"id": "Qwen/Qwen2.5-72B-Instruct-Turbo", "label": "Qwen 2.5 72B Turbo"},
            {"id": "deepseek-ai/DeepSeek-V3", "label": "DeepSeek V3"},
            {"id": "mistralai/Mixtral-8x7B-Instruct-v0.1", "label": "Mixtral 8x7B"},
        ],
    },
]


def by_id(provider_id: str) -> dict[str, Any] | None:
    for provider in PROVIDERS:
        if provider["id"] == provider_id:
            return provider
    return None


def api_key_for(provider: dict[str, Any]) -> str:
    return (os.getenv(provider["key_env"], "") or "").strip()


def selection_load() -> dict[str, Any] | None:
    """Текущий выбор модели (llm_selection.json) или None, если не задан."""
    try:
        if not SELECTION_FILE.is_file():
            return None
        data = json.loads(SELECTION_FILE.read_text(encoding="utf-8"))
        if not data.get("api_url") or not data.get("api_key") or not data.get("model"):
            return None
        return data
    except Exception:  # noqa: BLE001
        return None


def selection_save(data: dict[str, Any]) -> None:
    SELECTION_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def selection_reset() -> None:
    try:
        SELECTION_FILE.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def selection_display() -> str | None:
    """Красивая строка текущего выбора: «Label (model) @ Provider». """
    selection = selection_load()
    if not selection:
        return None
    provider = by_id(selection.get("provider", ""))
    provider_name = provider["name"] if provider else selection["api_url"]
    label = selection.get("label") or selection["model"]
    return f"{label} ({selection['model']}) @ {provider_name}"
