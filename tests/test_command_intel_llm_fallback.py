"""4.14 — Multi-provider LLM fallback (second adapter behind the same breaker).

Acceptance: when the primary provider is unavailable, a configured fallback provider is
tried before the shared circuit breaker trips; the breaker opens only if BOTH are down.
Truncation (not a provider-down condition) never triggers the fallback or the breaker.
Backward-compatible: with no fallback configured, a primary failure trips the breaker as
before.
"""
from __future__ import annotations

import pytest
from django.core.cache import cache
from django.test import override_settings

from apps.command_intel.llm import client as llm_client
from apps.command_intel.llm.client import (
    LLMClient,
    LLMRequest,
    LLMResult,
    LLMTruncated,
    LLMUnavailable,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_breaker():
    cache.delete(llm_client._BREAKER_KEY)
    yield
    cache.delete(llm_client._BREAKER_KEY)


class _FakeAdapter:
    def __init__(self, *, result=None, exc=None):
        self.result, self.exc, self.calls = result, exc, 0

    def generate(self, req):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.result


def _result(model="fake"):
    return LLMResult(obj={"ok": True}, text="{}", usage={}, model=model, latency_ms=1)


def _client(primary, fallback=None):
    c = LLMClient.__new__(LLMClient)  # bypass config-gated __init__
    c.adapter = primary
    c.fallback = fallback
    return c


REQ = LLMRequest(system="s", user_blocks=["u"])


def test_primary_success_skips_fallback():
    prim = _FakeAdapter(result=_result("primary"))
    fb = _FakeAdapter(result=_result("fallback"))
    res = _client(prim, fb).generate(REQ, retries=0)
    assert res.model == "primary" and prim.calls == 1 and fb.calls == 0


def test_falls_back_when_primary_unavailable():
    prim = _FakeAdapter(exc=LLMUnavailable("down"))
    fb = _FakeAdapter(result=_result("fallback"))
    res = _client(prim, fb).generate(REQ, retries=0)
    assert res.model == "fallback" and fb.calls == 1
    assert not LLMClient.breaker_open()  # a working fallback must NOT trip the breaker


def test_breaker_trips_only_when_both_fail():
    prim = _FakeAdapter(exc=LLMUnavailable("down"))
    fb = _FakeAdapter(exc=LLMUnavailable("also down"))
    with pytest.raises(LLMUnavailable):
        _client(prim, fb).generate(REQ, retries=0)
    assert LLMClient.breaker_open() and fb.calls == 1


def test_no_fallback_trips_breaker_as_before():
    prim = _FakeAdapter(exc=LLMUnavailable("down"))
    with pytest.raises(LLMUnavailable):
        _client(prim, None).generate(REQ, retries=0)
    assert LLMClient.breaker_open()


def test_truncation_does_not_fallback_or_trip_breaker():
    prim = _FakeAdapter(exc=LLMTruncated("length"))
    fb = _FakeAdapter(result=_result("fallback"))
    with pytest.raises(LLMTruncated):
        _client(prim, fb).generate(REQ, retries=0)
    assert fb.calls == 0 and not LLMClient.breaker_open()


def test_open_breaker_short_circuits():
    cache.set(llm_client._BREAKER_KEY, True, 60)
    prim = _FakeAdapter(result=_result("primary"))
    with pytest.raises(LLMUnavailable):
        _client(prim, None).generate(REQ, retries=0)
    assert prim.calls == 0  # breaker open → no adapter call at all


def test_redact_scrubs_any_key_format():
    from apps.command_intel.llm.adapters.minimax import _redact
    # Wide sk- prefix, not just MiniMax sk-cp- (OpenAI/Anthropic/… fallback keys).
    assert "sk-proj-SECRET" not in _redact("boom sk-proj-SECRETvalue123 here")
    # The literal configured key is scrubbed regardless of format (e.g. a bare token).
    assert "bare-token-xyz9" not in _redact("auth failed: bare-token-xyz9", "bare-token-xyz9")
    assert _redact("nothing to hide", "k") == "nothing to hide"  # short key never touched


@override_settings(LLM_FALLBACK_ENABLED=False)
def test_resolve_fallback_none_when_unconfigured():
    assert llm_client._resolve_fallback_adapter() is None


@override_settings(
    LLM_FALLBACK_ENABLED=True, LLM_FALLBACK_PROVIDER="minimax",
    LLM_FALLBACK_BASE_URL="https://api.example.com/v1",
    LLM_FALLBACK_API_KEY="fb-key", LLM_FALLBACK_MODEL="fallback-model",
)
def test_resolve_fallback_adapter_configured():
    adapter = llm_client._resolve_fallback_adapter()
    assert adapter is not None
    assert adapter.base_url == "https://api.example.com/v1"
    assert adapter.api_key == "fb-key" and adapter.model == "fallback-model"
