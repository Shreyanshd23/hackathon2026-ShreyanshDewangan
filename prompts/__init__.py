"""Versioned system prompts, loaded from Markdown files.

Keeping prompts as files (not Python string literals) makes prompt changes
diffable in code review and lets the eval suite gate on them like any other
source change.
"""
import os
from functools import cache

_DIR = os.path.dirname(os.path.abspath(__file__))


@cache
def load(name: str) -> str:
    with open(os.path.join(_DIR, f"{name}.md"), encoding="utf-8") as f:
        return f.read()
