"""Конфигурация бота из переменных окружения."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

from llm_client import LLMProvider
from providers import selection_load

load_dotenv()

OFFICIAL_TELEGRAM_API = "https://api.telegram.org"


def _enabled(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).strip().lower() not in {"0", "false", "no", "off"}


@dataclass(frozen=True)
class Config:
    telegram_token: str
    telegram_api_base_url: str
    telegram_proxy: str | None

    llm_primary_url: str
    llm_primary_key: str
    llm_primary_model: str

    llm_reserve_url: str
    llm_reserve_key: str
    llm_reserve_model: str
    llm_reserve_enabled: bool

    llm_fallback_url: str
    llm_fallback_key: str
    llm_fallback_model: str
    llm_fallback_enabled: bool

    pyaterochka_cookies_file: str | None
    pyaterochka_proxy: str | None

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_api_base_url=os.getenv("TELEGRAM_API_BASE_URL", OFFICIAL_TELEGRAM_API).rstrip("/"),
            telegram_proxy=os.getenv("TELEGRAM_PROXY_SOCKS5") or None,
            llm_primary_url=(os.getenv("LLM_API_URL", "https://api.openai.com/v1") or "").rstrip("/"),
            llm_primary_key=(os.getenv("LLM_API_KEY", "") or "").strip(),
            llm_primary_model=os.getenv("LLM_MODEL", "gpt-4o-mini").strip(),
            llm_reserve_url=(os.getenv("LLM_RESERVE_URL", "") or "").rstrip("/"),
            llm_reserve_key=(os.getenv("LLM_RESERVE_KEY", "") or "").strip(),
            llm_reserve_model=os.getenv("LLM_RESERVE_MODEL", "").strip(),
            llm_reserve_enabled=_enabled("LLM_RESERVE_ENABLED"),
            llm_fallback_url=(os.getenv("LLM_FALLBACK_URL", "") or "").rstrip("/"),
            llm_fallback_key=(os.getenv("LLM_FALLBACK_KEY", "") or "").strip(),
            llm_fallback_model=os.getenv("LLM_FALLBACK_MODEL", "").strip(),
            llm_fallback_enabled=_enabled("LLM_FALLBACK_ENABLED"),
            pyaterochka_cookies_file=os.getenv("PYATEROCHKA_COOKIES_FILE") or None,
            pyaterochka_proxy=os.getenv("PYATEROCHKA_PROXY") or None,
        )

    def _selection_provider(self) -> LLMProvider | None:
        """Модель, выбранная вручную через /model (если есть)."""
        selection = selection_load()
        if not selection:
            return None
        return LLMProvider(
            api_url=selection["api_url"],
            api_key=selection["api_key"],
            model=selection["model"],
        )

    def llm_providers(self) -> list[LLMProvider]:
        """Цепочка провайдеров для автоматического фолбэка.

        Порядок: выбор из /model (если есть) → основной (.env) →
        резерв 1 → резерв 2. Дубликаты (url+model) убираются.
        """
        chain: list[LLMProvider] = []
        seen: set[tuple[str, str]] = set()

        def add(provider: LLMProvider | None) -> None:
            if provider is None or not provider.api_key:
                return
            key = (provider.api_url.rstrip("/"), provider.model)
            if key in seen:
                return
            seen.add(key)
            chain.append(provider)

        add(self._selection_provider())
        add(LLMProvider(api_url=self.llm_primary_url, api_key=self.llm_primary_key, model=self.llm_primary_model))
        if self.llm_reserve_enabled and self.llm_reserve_key and self.llm_reserve_url and self.llm_reserve_model:
            add(LLMProvider(api_url=self.llm_reserve_url, api_key=self.llm_reserve_key, model=self.llm_reserve_model))
        if self.llm_fallback_enabled and self.llm_fallback_key and self.llm_fallback_url and self.llm_fallback_model:
            add(LLMProvider(api_url=self.llm_fallback_url, api_key=self.llm_fallback_key, model=self.llm_fallback_model))
        return chain

    def validate(self) -> None:
        missing = []
        if not self.telegram_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.llm_providers():
            missing.append("LLM_API_KEY")
        if missing:
            raise RuntimeError(f"Не заданы переменные окружения: {', '.join(missing)}")
