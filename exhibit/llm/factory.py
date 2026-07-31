"""Select an LLM backend.

Preference resolution (from --llm flag or ANACLI_LLM env var):
  - "mock"      → deterministic MockLLM (offline, no key)
  - "anthropic" → Claude adapter (errors if the SDK/credentials are missing)
  - "auto"      → Claude if the anthropic SDK is importable AND ANTHROPIC_API_KEY
                  is set; otherwise MockLLM. (Users relying on an `ant` profile
                  instead of the env var should pass --llm anthropic explicitly.)
"""

from __future__ import annotations

import importlib.util
import os

from .base import LLMClient
from .mock import MockLLM


def make_client(preference: str = "auto") -> LLMClient:
    pref = (preference or "auto").lower()

    if pref == "mock":
        return MockLLM()

    if pref == "anthropic":
        from .anthropic_client import AnthropicLLM

        return AnthropicLLM()

    if pref == "auto":
        has_sdk = importlib.util.find_spec("anthropic") is not None
        if has_sdk and os.environ.get("ANTHROPIC_API_KEY"):
            from .anthropic_client import AnthropicLLM

            return AnthropicLLM()
        return MockLLM()

    raise ValueError(f"unknown LLM preference {preference!r} (use mock|anthropic|auto)")
