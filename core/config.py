"""
Typed application settings
──────────────────────────
One place, validated at import. Anything tunable in the system is here, read
from the environment with sane defaults. No secrets are hardcoded; keys come
from the environment / .env only.

OFFLINE_MODE lets the entire pipeline run with a deterministic mock LLM and no
API keys — used by tests, CI, and local demos. This is why the eval suite can
gate CI without burning tokens.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _flag(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # ── LLM ───────────────────────────────────────────────
    groq_api_key: str | None = field(default_factory=lambda: os.getenv("GROQ_API_KEY") or os.getenv("GROQ_API_KEY_1"))
    gemini_api_key: str | None = field(default_factory=lambda: os.getenv("GEMINI_API_KEY"))
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "llama-3.3-70b-versatile"))
    llm_temperature: float = 0.15
    llm_max_tokens: int = 2048

    # Offline mock LLM (auto-on when no Groq key is present, unless forced).
    offline_mode: bool = field(default_factory=lambda: _flag("OFFLINE_MODE", not bool(os.getenv("GROQ_API_KEY") or os.getenv("GROQ_API_KEY_1"))))

    # ── Rate limiting (real concurrency, not sleep hacks) ──
    # Groq free tier ≈ 30 req/min and 12k tokens/min on this model.
    llm_rpm: int = field(default_factory=lambda: int(os.getenv("LLM_RPM", "28")))
    llm_tpm: int = field(default_factory=lambda: int(os.getenv("LLM_TPM", "11000")))
    max_concurrent_tickets: int = field(default_factory=lambda: int(os.getenv("MAX_CONCURRENT_TICKETS", "8")))

    # ── Resolver loop ─────────────────────────────────────
    max_tool_iterations: int = 12
    # ── Tool resilience ───────────────────────────────────
    tool_max_retries: int = 2
    tool_backoff_base: float = 0.4
    failure_injection_rate: float = field(default_factory=lambda: float(os.getenv("FAILURE_INJECTION_RATE", "0.05")))

    # ── Simulation / paths ────────────────────────────────
    simulated_date: str = "2024-03-15"
    data_dir: str = field(default_factory=lambda: os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shopwave.db")))

    # ── Server ────────────────────────────────────────────
    flask_debug: bool = field(default_factory=lambda: _flag("FLASK_DEBUG", False))
    allowed_origins: str = field(default_factory=lambda: os.getenv("ALLOWED_ORIGINS", "http://127.0.0.1:5000,http://localhost:5000"))


settings = Settings()
