"""
Central configuration for the ShopWave AI Support Agent.
All tuneable parameters in one place.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── LLM ──────────────────────────────────────────────
GROQ_API_KEY = os.getenv("GROQ_API_KEY") or os.getenv("GROQ_API_KEY_1")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
LLM_MODEL = os.getenv("LLM_MODEL", "llama-3.3-70b-versatile")
LLM_TEMPERATURE = 0.15
LLM_MAX_TOKENS = 2048
MAX_TOOL_ITERATIONS = 12          # max tool-call rounds per ticket

# ── Concurrency ──────────────────────────────────────
CONCURRENT_TICKETS = 5            # tickets processed in parallel

# ── Failure injection (for demo / hackathon) ─────────
FAILURE_INJECTION_RATE = 0.05     # 5 % random tool failure

# ── Simulation ───────────────────────────────────────
SIMULATED_DATE = "2024-03-15"     # "today" in the demo timeline

# ── Paths ────────────────────────────────────────────
DATA_DIR = os.path.dirname(os.path.abspath(__file__))
