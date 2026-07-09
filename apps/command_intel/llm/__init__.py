"""LLM integration for Command Intelligence (design doc 11).

A provider-agnostic ``LLMClient`` over per-vendor adapters. The reference adapter is
MiniMax (OpenAI-compatible). Everything here is called ONLY from Celery workers
(never a web request). The secret (``LLM_API_KEY``) is read from settings, never
logged. With no key, ``settings.COMMAND_INTEL_ENABLED`` is False and callers skip
the LLM entirely (the deterministic ``ready_degraded`` path).
"""
