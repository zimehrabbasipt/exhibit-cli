"""LLM abstraction. The engine depends only on the ``LLMClient`` protocol; the
mock (deterministic) and, later, real (Anthropic/OpenAI) adapters implement it."""
