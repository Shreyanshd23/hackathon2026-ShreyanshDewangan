"""Pytest bootstrap: force deterministic offline mode BEFORE any app import.

Must run before core.config is imported, so we set env here at collection time
and disable random tool-failure injection for reproducible tests.
"""

import os
import sys

os.environ.setdefault("OFFLINE_MODE", "1")
os.environ.setdefault("FAILURE_INJECTION_RATE", "0.0")
os.environ.setdefault("LOG_LEVEL", "ERROR")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
