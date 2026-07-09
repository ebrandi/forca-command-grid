"""Provider-agnostic LLM client (design doc 11 §1, §3, §5).

``LLMClient`` resolves the configured adapter and wraps it with the disciplined
outbound-HTTP shape the codebase already trusts for ESI: a circuit breaker + bounded
retry/backoff on transient failure, and a special retry that bumps ``max_output_tokens``
when a reasoning model truncates its answer (``finish_reason == "length"`` — observed
live on MiniMax M2.7/M3, doc 11 §10). All calls happen in a Celery worker.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger("forca.command_intel")

_BREAKER_KEY = "command_intel:llm:down"
_BREAKER_TTL = 60           # seconds the breaker stays open after tripping
_MAX_TRUNCATION_RETRIES = 2
_TRUNCATION_GROWTH = 1.6    # multiply max_output_tokens on a length-truncation retry
_MAX_OUTPUT_CEILING = 32768


# --- neutral request/response ------------------------------------------------
@dataclass(frozen=True)
class LLMRequest:
    system: str
    user_blocks: list[str]          # ordered content blocks (instructions + fenced data)
    schema: dict = field(default_factory=dict)
    max_output_tokens: int = 8192
    temperature: float = 0.3
    model: str = ""


@dataclass(frozen=True)
class LLMResult:
    obj: dict | None                # parsed structured object (None if unparseable)
    text: str                       # the answer text (think-block stripped)
    usage: dict                     # {input, output, reasoning?, cached?}
    model: str
    latency_ms: int
    finish_reason: str = ""


# --- errors ------------------------------------------------------------------
class LLMError(Exception):
    """Base for all LLM client errors."""


class LLMUnavailable(LLMError):
    """Provider is down / rate-limited / breaker open — caller degrades gracefully."""


class LLMTruncated(LLMError):
    """The model hit the output-token limit before finishing (``finish_reason==length``)."""

    def __init__(self, message: str, *, partial_text: str = "", usage: dict | None = None):
        super().__init__(message)
        self.partial_text = partial_text
        self.usage = usage or {}


class LLMConfigError(LLMError):
    """The subsystem is not configured (no key / unknown provider)."""


# --- adapter resolution ------------------------------------------------------
def _resolve_adapter(provider: str, *, base_url=None, api_key=None, model=None):
    provider = (provider or settings.LLM_PROVIDER or "").lower()
    # The MiniMax adapter speaks OpenAI-compatible chat completions and tolerates a plain
    # OpenAI endpoint, so it also serves an "openai"-style fallback provider (4.14).
    if provider in ("minimax", "openai", "openai_compatible"):
        from .adapters.minimax import MiniMaxAdapter

        return MiniMaxAdapter(base_url=base_url, api_key=api_key, model=model)
    raise LLMConfigError(f"Unknown LLM provider: {provider!r}")


def _resolve_fallback_adapter():
    """The configured second provider, or None when no fallback is set (4.14)."""
    if not getattr(settings, "LLM_FALLBACK_ENABLED", False):
        return None
    return _resolve_adapter(
        getattr(settings, "LLM_FALLBACK_PROVIDER", "") or "minimax",
        base_url=settings.LLM_FALLBACK_BASE_URL,
        api_key=settings.LLM_FALLBACK_API_KEY,
        model=getattr(settings, "LLM_FALLBACK_MODEL", "") or None,
    )


# --- the client --------------------------------------------------------------
class LLMClient:
    """Resolve the adapter once; guard → call → record on every request."""

    def __init__(self, provider: str | None = None):
        if not settings.COMMAND_INTEL_ENABLED:
            raise LLMConfigError("COMMAND_INTEL_ENABLED is False (no LLM_API_KEY)")
        self.adapter = _resolve_adapter(provider or settings.LLM_PROVIDER)
        # 4.14: an optional second provider, tried when the primary is unavailable,
        # behind the SAME breaker (the breaker trips only if BOTH are down).
        self.fallback = _resolve_fallback_adapter()

    # -- breaker ------------------------------------------------------------
    @staticmethod
    def breaker_open() -> bool:
        return bool(cache.get(_BREAKER_KEY))

    @staticmethod
    def _trip_breaker() -> None:
        cache.set(_BREAKER_KEY, True, _BREAKER_TTL)

    # -- public -------------------------------------------------------------
    def generate(self, req: LLMRequest, *, retries: int | None = None) -> LLMResult:
        """Call the primary adapter (with breaker + bounded retry/backoff + truncation
        growth); on :class:`LLMUnavailable`, fall back to the second provider if one is
        configured (4.14) before tripping the shared breaker.

        Raises :class:`LLMUnavailable` only if the provider(s) can't be reached (the
        caller then writes a ``ready_degraded`` report). Raises :class:`LLMTruncated`
        only if the answer keeps truncating at the ceiling — truncation is not a
        provider-down condition, so it never triggers the fallback or the breaker.
        """
        if self.breaker_open():
            raise LLMUnavailable("LLM circuit breaker is open")
        retries = settings_int("LLM client retries", default=2) if retries is None else retries

        try:
            return self._generate_with_adapter(self.adapter, req, retries)
        except LLMUnavailable as primary_exc:
            if self.fallback is not None:
                logger.warning(
                    "command_intel primary LLM unavailable (%s); trying fallback provider",
                    primary_exc,
                )
                try:
                    return self._generate_with_adapter(self.fallback, req, retries)
                except LLMUnavailable as fb_exc:
                    self._trip_breaker()
                    raise LLMUnavailable(
                        f"primary and fallback LLM both unavailable ({fb_exc})"
                    ) from fb_exc
            self._trip_breaker()
            raise

    def _generate_with_adapter(self, adapter, req: LLMRequest, retries: int) -> LLMResult:
        """One adapter's call loop: bounded network retry/backoff + truncation growth.
        Raises :class:`LLMUnavailable` on exhaustion (WITHOUT tripping the breaker — the
        caller decides that after the fallback) or :class:`LLMTruncated` on repeated
        length-truncation at the ceiling."""
        max_tokens = req.max_output_tokens
        last_exc: Exception | None = None

        for _trunc in range(_MAX_TRUNCATION_RETRIES + 1):
            attempt_req = req if max_tokens == req.max_output_tokens else _with_tokens(req, max_tokens)
            for net_attempt in range(retries + 1):
                try:
                    return adapter.generate(attempt_req)
                except LLMTruncated as exc:
                    last_exc = exc
                    break  # grow tokens, don't burn network retries on a clean truncation
                except LLMUnavailable as exc:
                    last_exc = exc
                    if net_attempt < retries:
                        time.sleep(min(2 ** net_attempt + random.uniform(0, 0.5), 8))  # noqa: S311 - jitter, not crypto
                        continue
                    raise  # exhausted this adapter's retries — caller handles fallback/breaker
            # we only reach here via the truncation break — grow and retry
            if max_tokens >= _MAX_OUTPUT_CEILING:
                break
            max_tokens = min(int(max_tokens * _TRUNCATION_GROWTH), _MAX_OUTPUT_CEILING)
            logger.info("command_intel LLM truncated; retrying with max_output_tokens=%d", max_tokens)

        assert last_exc is not None
        raise last_exc


def _with_tokens(req: LLMRequest, max_tokens: int) -> LLMRequest:
    return LLMRequest(
        system=req.system, user_blocks=req.user_blocks, schema=req.schema,
        max_output_tokens=max_tokens, temperature=req.temperature, model=req.model,
    )


def settings_int(_label: str, *, default: int) -> int:
    # Reads the per-report transport retry count from provider config if present.
    try:
        from apps.command_intel import config

        return int(config.get("provider").get("retry_attempts", default))
    except Exception:  # noqa: BLE001 - config is best-effort here
        return default
